"""The YouTube tool surface.

TWO THINGS THAT WOULD BREAK THIS SERVER IF DONE THE OBVIOUS WAY
---------------------------------------------------------------
1. **stdout is the MCP transport.** `yt_write.py` is a CLI and prints
   constantly -- progress lines, reminders, banners. A single stray print on
   stdout corrupts the JSON-RPC stream and the client drops the connection.
   Every call into that module therefore runs inside `_quiet()`, which
   redirects stdout to a buffer we can return as `cli_output`.

2. **Uploads outlive a tool call.** 134 MB took real minutes at 8 MB chunks;
   400 MB episodes take longer. A synchronous upload tool reports "timed out"
   while the transfer is still running, and the natural response -- retry --
   burns another 1,600 quota units against a 10,000/day cap. So
   `youtube_upload_video` hands back a job_id immediately and the transfer
   continues on a worker thread.
"""

import contextlib
import io
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .coordinator import (
    COST_CAPTIONS_INSERT,
    COST_READ,
    COST_VIDEOS_INSERT,
    COST_WRITE,
    KNOWN_CHANNELS,
    WRONG_CHANNELS,
    NoToken,
    WrongChannel,
    assert_safe_channel,
    bound_channels,
    mcp,
    mirror_status,
    quota_charge,
    quota_guard,
    quota_read,
    service,
    txt2srt_module,
    yt_write,
)

logger = logging.getLogger(__name__)

CAPTION_EXTS = (".srt", ".sbv", ".vtt")
MAX_TAGS = 15  # SOP Step 5 house rule. Refuse the 16th, never truncate.


@contextlib.contextmanager
def _quiet():
    """Run a CLI-shaped call without letting its prints reach the MCP transport."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def _fail(exc: Exception) -> dict[str, Any]:
    """Turn a refusal into a readable result instead of an opaque traceback.

    These are not retryable. Each one names the state a human has to change.
    """
    if isinstance(exc, NoToken):
        return {"ok": False, "error": "no_token", "message": str(exc)}
    if isinstance(exc, WrongChannel):
        return {"ok": False, "error": "wrong_channel", "message": str(exc)}
    return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


# ---------------------------------------------------------------------------
# 1. Auth status -- build and call this before anything else
# ---------------------------------------------------------------------------

@mcp.tool()
def youtube_auth_status() -> dict[str, Any]:
    """Which channel is this token bound to, and is it safe to write there?

    CALL THIS FIRST IN ANY SESSION THAT WILL WRITE.

    A token bound to a personal channel that owns nothing returns HTTP 200 on
    every call, and uploads land there with NO error of any kind. That failure
    is invisible until someone goes looking on the wrong channel. This tool is
    how it becomes visible.

    Also reports the daily quota and whether the mirrored copy of yt_write.py
    has drifted from the canonical one (they are separate inodes; editing one
    and not the other is a live trap).

    Returns ok=false rather than raising, so a caller can branch on it.
    """
    result: dict[str, Any] = {
        "allowlist": KNOWN_CHANNELS,
        "denylist": WRONG_CHANNELS,
        "token_file": str(yt_write.TOKEN_FILE),
        "yt_write_mirror": mirror_status(),
        "quota": quota_read(),
    }
    try:
        yt = service()
        with _quiet():
            chans = bound_channels(yt)
        quota_charge(COST_READ, "channels.list")
        result["channels"] = chans
        bad = [c for c in chans if c["denied"] or not c["allowed"]]
        if not chans:
            result.update(ok=False, error="no_channels",
                          message="This token owns NO channels. Re-run auth and pick the "
                                  "BRAND ACCOUNT row.")
        elif bad:
            result.update(
                ok=False, error="wrong_channel",
                message="Token is bound to a channel that is NOT on the allowlist. "
                        "Writes are refused. Uploads would land there silently. "
                        f"rm {yt_write.TOKEN_FILE} and re-run auth in a terminal.")
        else:
            result.update(ok=True,
                          message=f"Bound to {chans[0]['title']}. Safe to write.")
        return result
    except Exception as exc:
        result.update(_fail(exc))
        return result


@mcp.tool()
def youtube_quota_status() -> dict[str, Any]:
    """Daily YouTube API quota: spent, remaining, and uploads still affordable.

    videos.insert costs 1,600 units of 10,000/day, so the real ceiling is 4-5
    episodes per day. Resets at midnight US Pacific.

    This is a FLOOR, not a ledger of record: it counts only what this server
    spent. Anything spent by the CLI or another client is invisible here.
    """
    return {"ok": True, **quota_read(),
            "costs": {"videos.insert": COST_VIDEOS_INSERT,
                      "captions.insert": COST_CAPTIONS_INSERT,
                      "metadata write": COST_WRITE, "list/read": COST_READ}}


# ---------------------------------------------------------------------------
# 2. Upload -- asynchronous, because the transfer outlives the tool call
# ---------------------------------------------------------------------------

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _job_set(job_id: str, **fields) -> None:
    # The job dict carries its own id so youtube_upload_status can return a
    # self-describing record. Seeding it here rather than letting the caller
    # pass job_id=... is deliberate: doing that collides with the positional
    # parameter and raises "got multiple values for argument 'job_id'", which
    # is what happened on every single upload attempt until 2026-09-01.
    with _jobs_lock:
        _jobs.setdefault(job_id, {"job_id": job_id}).update(fields)


def _job_get(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _run_upload(job_id: str, body: dict[str, Any], file: str,
                playlist_id: str | None, record_json: str | None) -> None:
    """Worker thread: the actual transfer.

    Mirrors `yt_write._resumable` but records percent onto the job instead of
    printing it, which is what makes polling possible.
    """
    try:
        from googleapiclient.http import MediaFileUpload

        _job_set(job_id, status="uploading", progress=0)
        yt = service()
        media = MediaFileUpload(file, chunksize=8 * 1024 * 1024, resumable=True)
        req = yt.videos().insert(
            part="snippet,status,recordingDetails", body=body, media_body=media)

        resp, last = None, -1
        while resp is None:
            chunk_status, resp = req.next_chunk()
            if chunk_status:
                pct = int(chunk_status.progress() * 100)
                if pct != last:
                    _job_set(job_id, progress=pct)
                    last = pct

        vid = resp["id"]
        quota_charge(COST_VIDEOS_INSERT, f"videos.insert {vid}")
        url = f"https://youtu.be/{vid}"
        _job_set(job_id, status="verifying", progress=100, video_id=vid, url=url)

        # Read-back verification. The punch row must only close on a video that
        # demonstrably exists and is owned by this token -- not merely on an id
        # coming back from an insert call.
        check = yt.videos().list(part="snippet,status", id=vid).execute()
        quota_charge(COST_READ, "videos.list verify")
        items = check.get("items", [])
        if not items:
            _job_set(job_id, status="failed", verified=False,
                     error="Upload returned id " + vid +
                           " but a read-back found no such video. Do NOT mark the "
                           "punch row done.")
            return

        if playlist_id:
            try:
                with _quiet():
                    yt.playlistItems().insert(part="snippet", body={"snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": vid},
                    }}).execute()
                quota_charge(COST_WRITE, "playlistItems.insert")
                _job_set(job_id, playlist_added=playlist_id)
            except Exception as exc:
                # The video uploaded. Say what did not happen; do not fail the job.
                _job_set(job_id, playlist_added=False, playlist_error=str(exc))

        payload = {
            "youtube_video_id": vid,
            "youtube_url": url,
            "youtube_studio_url": f"https://studio.youtube.com/video/{vid}/edit",
            "youtube_privacy": body["status"]["privacyStatus"],
        }
        if body["status"].get("publishAt"):
            payload["youtube_publish_at"] = body["status"]["publishAt"]
        if record_json:
            Path(record_json).write_text(json.dumps(payload, indent=2))
            _job_set(job_id, record_json_written=record_json)

        _job_set(job_id, status="done", verified=True, payload=payload,
                 finished_at=datetime.now(timezone.utc).isoformat(),
                 still_manual=["End screen (Studio only, not in the Data API)",
                               "Info cards (Studio only, not in the Data API)"])
    except Exception as exc:
        logger.exception("upload job %s failed", job_id)
        _job_set(job_id, status="failed", verified=False,
                 error=f"{type(exc).__name__}: {exc}",
                 finished_at=datetime.now(timezone.utc).isoformat())


@mcp.tool()
def youtube_upload_video(
    file: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    category_id: str = "22",
    language: str = "en",
    privacy: str = "private",
    publish_at: str | None = None,
    recording_date: str | None = None,
    playlist_id: str | None = None,
    record_json: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upload a video. Returns a job_id IMMEDIATELY -- poll `youtube_upload_status`.

    This is asynchronous on purpose. A 134 MB file takes minutes at 8 MB chunks
    and a synchronous call would report a timeout while the transfer was still
    running; the obvious retry then burns a second 1,600 quota units.

    Three rules are enforced here rather than left to Google:
      - `privacy` defaults to **private**. Never public by default.
      - `publish_at` requires privacy=private (YouTube rejects it otherwise);
        caught locally so you get a sentence instead of an opaque 400.
      - Maximum 15 tags. The 16th is REFUSED, never silently trimmed.

    Args:
        file: Absolute path to the video on this machine.
        title: Video title.
        description: Video description.
        tags: Up to 15 tags.
        category_id: YouTube categoryId. 22 = People & Blogs.
        language: Default audio/text language.
        privacy: private | unlisted | public. Defaults to private.
        publish_at: ISO8601 UTC to schedule, e.g. 2026-08-20T13:00:00Z. Requires private.
        recording_date: ISO8601 when it was recorded.
        playlist_id: Add to this playlist after a verified upload.
        record_json: Absolute path to write the {youtube_url, ...} payload to.
        dry_run: Validate and show the request body without uploading.
    """
    tags = tags or []
    try:
        if not os.path.isabs(file):
            raise ValueError(f"file must be an absolute path, got {file!r}")
        if not os.path.exists(file):
            raise FileNotFoundError(f"No such file: {file}")
        if len(tags) > MAX_TAGS:
            raise ValueError(
                f"{len(tags)} tags. The house limit is {MAX_TAGS} (SOP Step 5). "
                "Refusing rather than truncating -- a silently dropped tag is worse "
                "than an error. Trim it and call again.")
        if privacy not in ("private", "unlisted", "public"):
            raise ValueError(f"privacy must be private|unlisted|public, got {privacy!r}")
        if publish_at and privacy != "private":
            raise ValueError(
                "publish_at requires privacy='private'. YouTube rejects publishAt on a "
                "non-private video. Keep it private and let publish_at flip it.")

        size_mb = os.path.getsize(file) / (1024 * 1024)
        body: dict[str, Any] = {
            "snippet": {
                "title": title,
                "description": description or "",
                "tags": tags,
                "categoryId": category_id,
                "defaultLanguage": language,
                "defaultAudioLanguage": language,
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
                "embeddable": True,
            },
        }
        if publish_at:
            body["status"]["publishAt"] = publish_at
        if recording_date:
            body["recordingDetails"] = {"recordingDate": recording_date}

        if dry_run:
            return {"ok": True, "dry_run": True, "file": file,
                    "size_mb": round(size_mb, 1), "body": body,
                    "playlist_id": playlist_id, "quota": quota_read(),
                    "message": "Validated. Nothing uploaded."}

        quota_guard(COST_VIDEOS_INSERT, "videos.insert")
        yt = service()
        with _quiet():
            assert_safe_channel(yt)   # 1 unit, guarding a 1,600-unit write
        quota_charge(COST_READ, "channels.list guard")

        job_id = uuid.uuid4().hex[:12]
        _job_set(job_id, status="queued", progress=0, file=file,
                 size_mb=round(size_mb, 1), title=title, privacy=privacy,
                 started_at=datetime.now(timezone.utc).isoformat())
        threading.Thread(target=_run_upload, daemon=True,
                         args=(job_id, body, file, playlist_id, record_json)).start()

        return {
            "ok": True, "job_id": job_id, "status": "queued",
            "file": file, "size_mb": round(size_mb, 1), "privacy": privacy,
            "message": f"Upload started ({size_mb:.1f} MB). Poll "
                       f"youtube_upload_status(job_id='{job_id}'). Do not start a "
                       "second upload of this file -- each attempt costs 1,600 units.",
        }
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def youtube_upload_status(job_id: str) -> dict[str, Any]:
    """Progress or result of an upload started by `youtube_upload_video`.

    status: queued | uploading | verifying | done | failed
    `verified` is true only when a read-back confirmed the video exists. Do NOT
    close a punchlist row on anything less.
    """
    job = _job_get(job_id)
    if job is None:
        return {"ok": False, "error": "unknown_job",
                "message": f"No job {job_id!r}. Jobs are in-memory and do not survive an "
                           "MCP server restart -- check the channel for the video before "
                           "re-uploading, since a retry costs another 1,600 units."}
    return {"ok": job.get("status") != "failed", **job}


# ---------------------------------------------------------------------------
# 3. Captions
# ---------------------------------------------------------------------------

@mcp.tool()
def youtube_upload_captions(video_id: str, file: str, language: str = "en",
                            name: str = "English",
                            dry_run: bool = False) -> dict[str, Any]:
    """Attach a real caption track, replacing any existing track of the same name.

    This is what puts CORRECT proper nouns on the video -- guest names, company
    names, product names. YouTube's automatic captions mangle exactly those, and
    they are the words most worth getting right.

    Args:
        video_id: The videoId to caption.
        file: Absolute path to a .srt / .sbv / .vtt file.
        language: Track language code.
        name: Track name. Reusing a name REPLACES that track.
        dry_run: Validate without uploading.
    """
    try:
        if not os.path.isabs(file):
            raise ValueError(f"file must be an absolute path, got {file!r}")
        if not os.path.exists(file):
            raise FileNotFoundError(f"No such caption file: {file}")
        ext = os.path.splitext(file)[1].lower()
        if ext not in CAPTION_EXTS:
            raise ValueError(
                f"{ext} is not a caption format. Use .srt, .sbv or .vtt. "
                "youtube_transcript_to_srt converts a Riverside .txt transcript.")
        if dry_run:
            return {"ok": True, "dry_run": True, "video_id": video_id, "file": file,
                    "language": language, "name": name, "message": "Validated."}

        quota_guard(COST_CAPTIONS_INSERT, "captions.insert")
        from googleapiclient.http import MediaFileUpload
        yt = service()
        with _quiet() as buf:
            assert_safe_channel(yt)
            existing = yt.captions().list(part="snippet", videoId=video_id).execute()
            replaced = None
            for it in existing.get("items", []):
                sn = it["snippet"]
                if sn.get("language") == language and sn.get("name") == name:
                    req = yt.captions().update(
                        part="snippet", body={"id": it["id"]},
                        media_body=MediaFileUpload(file, resumable=True))
                    yt_write._resumable(req, "captions")
                    replaced = it["id"]
                    break
            if replaced is None:
                req = yt.captions().insert(part="snippet", body={"snippet": {
                    "videoId": video_id, "language": language,
                    "name": name, "isDraft": False,
                }}, media_body=MediaFileUpload(file, resumable=True))
                resp = yt_write._resumable(req, "captions")
                replaced = resp["id"]
        quota_charge(COST_CAPTIONS_INSERT, f"captions {video_id}")
        return {"ok": True, "video_id": video_id, "caption_id": replaced,
                "replaced_existing": bool(existing.get("items")),
                "cli_output": buf.getvalue().strip(), "quota": quota_read()}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def youtube_transcript_to_srt(input_txt: str, output_srt: str,
                              duration_seconds: float | None = None) -> dict[str, Any]:
    """Convert a Riverside speaker-timestamped .txt transcript into a valid .srt.

    Timing derives from block starts, not phrase level, so a Riverside-exported
    .srt is always tighter. Use this when what matters is correct names and
    searchable text without waiting on a re-export.
    """
    try:
        if not os.path.isabs(input_txt) or not os.path.isabs(output_srt):
            raise ValueError("input_txt and output_srt must both be absolute paths")
        if not os.path.exists(input_txt):
            raise FileNotFoundError(f"No such transcript: {input_txt}")
        mod = txt2srt_module()
        text = Path(input_txt).read_text()
        with _quiet():
            blocks = mod.parse_blocks(text)
            srt = mod.build(blocks, total_duration=duration_seconds)
        Path(output_srt).write_text(srt)
        return {"ok": True, "input": input_txt, "output": output_srt,
                "blocks": len(blocks), "bytes": len(srt),
                "note": "Block-level timing. A Riverside-exported .srt is tighter if "
                        "you can wait for one."}
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# 4. Metadata reads and edits
# ---------------------------------------------------------------------------

@mcp.tool()
def youtube_get_video(video_id: str) -> dict[str, Any]:
    """Read a video's title, description, tags, category and privacy."""
    try:
        yt = service()
        with _quiet():
            resp = yt.videos().list(part="snippet,status", id=video_id).execute()
        quota_charge(COST_READ, "videos.list")
        items = resp.get("items", [])
        if not items:
            return {"ok": False, "error": "not_found",
                    "message": f"No video {video_id} (or this token does not own it)."}
        sn, st = items[0]["snippet"], items[0].get("status", {})
        return {"ok": True, "id": video_id, "title": sn.get("title"),
                "description": sn.get("description", ""), "tags": sn.get("tags", []),
                "category_id": sn.get("categoryId"),
                "default_language": sn.get("defaultLanguage"),
                "privacy": st.get("privacyStatus"),
                "publish_at": st.get("publishAt"),
                "url": f"https://youtu.be/{video_id}"}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def youtube_update_video(video_id: str, title: str | None = None,
                         description: str | None = None,
                         tags: list[str] | None = None,
                         privacy: str | None = None,
                         publish_at: str | None = None,
                         dry_run: bool = False) -> dict[str, Any]:
    """Edit a video's title, description, tags, visibility or publish schedule.

    videos.update replaces whole parts, so this reads the current snippet and
    status first and changes only the fields you pass. Omitted fields keep
    their values rather than being blanked.

    Title/description/tags live in part="snippet"; privacy and scheduling live
    in part="status". This tool used to send snippet only, which meant an
    already-uploaded video could not be made public or scheduled at all --
    the gap that sent a session writing throwaway scripts on 2026-08-25.

    `privacy`: private | unlisted | public.
    `publish_at`: ISO8601 UTC, e.g. 2026-09-01T13:00:00Z. YouTube REQUIRES
    privacyStatus=private for publishAt, so this forces private for you rather
    than letting the API return an opaque 400. An unlisted video therefore
    cannot be scheduled without going private first, which this handles.
    """
    try:
        if (title is None and description is None and tags is None
                and privacy is None and publish_at is None):
            raise ValueError("Nothing to change. Pass title, description, "
                             "tags, privacy or publish_at.")
        if privacy is not None and privacy not in ("private", "unlisted", "public"):
            raise ValueError(f"privacy must be private, unlisted or public; got {privacy!r}.")
        if tags is not None and len(tags) > MAX_TAGS:
            raise ValueError(f"{len(tags)} tags exceeds the house limit of {MAX_TAGS}.")
        yt = service()
        with _quiet():
            assert_safe_channel(yt)
            resp = yt.videos().list(part="snippet,status", id=video_id).execute()
        quota_charge(COST_READ * 2, "videos.list + guard")
        items = resp.get("items", [])
        if not items:
            return {"ok": False, "error": "not_found",
                    "message": f"No video {video_id} (or this token does not own it)."}
        sn = items[0]["snippet"]
        st = items[0].get("status", {})
        new = {"title": sn.get("title"), "categoryId": sn.get("categoryId"),
               "description": sn.get("description", ""), "tags": sn.get("tags", [])}
        if sn.get("defaultLanguage"):
            new["defaultLanguage"] = sn["defaultLanguage"]
        changes = {}
        if title is not None:
            changes["title"] = [new["title"], title]; new["title"] = title
        if description is not None:
            changes["description"] = ["<len %d>" % len(new["description"]),
                                      "<len %d>" % len(description)]
            new["description"] = description
        if tags is not None:
            changes["tags"] = [new["tags"], tags]; new["tags"] = tags
        # Status is a separate part. Carry the current flags forward so a
        # privacy change cannot silently reset embeddable/license/made-for-kids.
        status_body = None
        if privacy is not None or publish_at is not None:
            status_body = {
                "selfDeclaredMadeForKids": st.get("selfDeclaredMadeForKids", False),
                "license": st.get("license", "youtube"),
                "embeddable": st.get("embeddable", True),
                "publicStatsViewable": st.get("publicStatsViewable", True),
            }
            if publish_at is not None:
                # YouTube rejects publishAt on anything but private.
                status_body["privacyStatus"] = "private"
                status_body["publishAt"] = publish_at
                changes["publish_at"] = [st.get("publishAt"), publish_at]
                changes["privacy"] = [st.get("privacyStatus"), "private"]
                if privacy is not None and privacy != "private":
                    changes["privacy_ignored"] = (
                        f"{privacy} ignored; publish_at requires private")
            else:
                status_body["privacyStatus"] = privacy
                changes["privacy"] = [st.get("privacyStatus"), privacy]

        snippet_changed = title is not None or description is not None or tags is not None
        if dry_run:
            return {"ok": True, "dry_run": True, "id": video_id, "changes": changes}
        with _quiet():
            if snippet_changed:
                yt.videos().update(part="snippet",
                                   body={"id": video_id, "snippet": new}).execute()
                quota_charge(COST_WRITE, f"videos.update snippet {video_id}")
            if status_body is not None:
                yt.videos().update(part="status",
                                   body={"id": video_id, "status": status_body}).execute()
                quota_charge(COST_WRITE, f"videos.update status {video_id}")
        return {"ok": True, "id": video_id, "changes": changes, "quota": quota_read()}
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# 5. Playlists
# ---------------------------------------------------------------------------

@mcp.tool()
def youtube_playlist_list() -> dict[str, Any]:
    """Every playlist this channel owns, with video counts."""
    try:
        yt = service()
        with _quiet():
            resp = yt.playlists().list(part="snippet,contentDetails", mine=True,
                                       maxResults=50).execute()
        quota_charge(COST_READ, "playlists.list")
        return {"ok": True, "playlists": [
            {"id": it["id"], "title": it["snippet"]["title"],
             "items": it["contentDetails"]["itemCount"]}
            for it in resp.get("items", [])]}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def youtube_playlist_create(title: str, description: str = "",
                            privacy: str = "public",
                            dry_run: bool = False) -> dict[str, Any]:
    """Create a playlist. Idempotent by title: an existing match is reused, not duplicated."""
    try:
        if dry_run:
            return {"ok": True, "dry_run": True, "title": title, "privacy": privacy}
        yt = service()
        with _quiet():
            assert_safe_channel(yt)
            existing = yt.playlists().list(part="snippet", mine=True,
                                           maxResults=50).execute()
        quota_charge(COST_READ * 2, "playlists.list + guard")
        for it in existing.get("items", []):
            if it["snippet"]["title"].strip().lower() == title.strip().lower():
                return {"ok": True, "id": it["id"], "reused": True,
                        "url": "https://www.youtube.com/playlist?list=" + it["id"],
                        "message": "A playlist with this title already existed; reusing it."}
        with _quiet():
            resp = yt.playlists().insert(part="snippet,status", body={
                "snippet": {"title": title, "description": description},
                "status": {"privacyStatus": privacy}}).execute()
        quota_charge(COST_WRITE, "playlists.insert")
        return {"ok": True, "id": resp["id"], "reused": False,
                "url": "https://www.youtube.com/playlist?list=" + resp["id"]}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def youtube_playlist_add(video_id: str, playlist_id: str) -> dict[str, Any]:
    """Add a video to a playlist. A video already present is skipped, not duplicated."""
    try:
        yt = service()
        with _quiet():
            assert_safe_channel(yt)
            items = yt.playlistItems().list(part="snippet", playlistId=playlist_id,
                                            maxResults=50).execute()
        quota_charge(COST_READ * 2, "playlistItems.list + guard")
        for it in items.get("items", []):
            if it["snippet"].get("resourceId", {}).get("videoId") == video_id:
                return {"ok": True, "added": False, "video_id": video_id,
                        "playlist_id": playlist_id,
                        "message": "Already in the playlist; skipped."}
        with _quiet():
            yt.playlistItems().insert(part="snippet", body={"snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }}).execute()
        quota_charge(COST_WRITE, "playlistItems.insert")
        return {"ok": True, "added": True, "video_id": video_id,
                "playlist_id": playlist_id}
    except Exception as exc:
        return _fail(exc)
