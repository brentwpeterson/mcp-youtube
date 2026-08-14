# youtube MCP server

Authenticated YouTube writes for Brent's channels: uploads, captions, playlists,
metadata. Built 2026-08-14 from Claude-Dirac's proven CLI.

## Why this is its own server

A 134 MB video (Kate Farms; longer episodes run 400 MB+) lives on Brent's Mac,
and so does the OAuth token. Routing the file through `app.requestdesk.ai` to
reach Google would be a double transfer into storage the API does not have, and
would require moving a YouTube refresh token into AWS secrets for no gain.

This does **not** violate "the requestdesk MCP never touches Mongo" — this is not
the requestdesk MCP, and it touches no database at all. It is the same shape as
the slack / trello / transistor / quickbooks servers: one MCP fronting one vendor
API.

    youtube MCP      owns the UPLOAD
    requestdesk MCP  owns the RECORD   (episode_youtube_upload -> punch_set)

## It wraps `yt_write.py`, it does not reimplement it

    /Users/brent/scripts/CB-Workspace/claude-shared/skills/youtube/yt_write.py

Every guard in that file was written after a real failure on 2026-08-13. The
allowlist, denylist and resumable-transfer helper are imported from it so there
is one definition rather than two that drift.

That file is **mirrored** to `.claude/skills/youtube/yt_write.py` as a separate
inode. Editing one and not the other is a live trap, so `youtube_auth_status`
reports a sha comparison of the two under `yt_write_mirror`.

## The guard that matters most

A token bound to Brent's personal channel (`UCJmVHG4ZzHM-B4MuSbtpEDA`,
@AgenticCommerceGuy, empty) returns **HTTP 200 on every call** and uploads land
there with no error whatsoever. Dirac hit this three times.

`assert_safe_channel` runs before *every* write, not just at startup — a token
can be replaced underneath a running server. It costs 1 quota unit to protect a
1,600-unit write.

    ALLOWED   UC5blM_aXiUhP5QCChKcjTDw   Talk Commerce
              UCwJYSup2J2ZnmDGA0Yx_GSA   Content Cucumber
    REFUSED   UCJmVHG4ZzHM-B4MuSbtpEDA   personal, empty

## Uploads are asynchronous on purpose

134 MB takes minutes at 8 MB chunks. A synchronous tool call reports a timeout
while the transfer is still running, and the natural response — retry — burns a
second 1,600 quota units.

    job = youtube_upload_video(file=..., title=...)   # returns immediately
    youtube_upload_status(job["job_id"])              # queued|uploading|verifying|done|failed

`verified: true` appears only after a read-back confirms the video exists. **Do
not close a punchlist row on anything less than that.**

Jobs are in-memory and do not survive a server restart. If you lose one, check
the channel before re-uploading.

## Enforced server-side

- `privacy` defaults to **private**. Never public by default.
- `publish_at` requires `privacy=private` (YouTube rejects it otherwise) — caught
  locally so you get a sentence instead of an opaque 400.
- **Max 15 tags** (SOP Step 5). The 16th is refused, never silently trimmed.

## Quota

`videos.insert` costs 1,600 of 10,000/day, so the ceiling is **4–5 episodes per
day**. 26 unpublished episodes is a five-day burn.

The ledger at `~/.config/brent-youtube/quota.json` resets at midnight US Pacific
and refuses an upload *before* spending when the day is gone. It counts only
what this server spent, so treat it as a floor — CLI usage is invisible to it.

## What this server deliberately does NOT do

**End screens and info cards.** They are not in the YouTube Data API at all —
Studio-only, permanently. The SOP requires both on every episode and every short,
so that step always ends with a manual pass. A tool claiming to do it would be
lying, so there isn't one.

## Auth

Credentials live at `~/.config/brent-youtube/{client_secret.json,token.json}`,
chmod 600. Scopes: `youtube`, `youtube.force-ssl`, `youtube.upload`.

**`auth` cannot run from a tool call** — it opens a browser and blocks on a
localhost callback, which would hang the MCP client. When the token is missing,
tools return an actionable error instead. A human runs this in a terminal:

    /Users/brent/scripts/CB-Workspace/.claude/local/youtube-venv/bin/python \
      /Users/brent/scripts/CB-Workspace/claude-shared/skills/youtube/yt_write.py auth

At the chooser, pick the **BRAND ACCOUNT** row (`Content Basis` → Talk Commerce),
never the personal `Brent Peterson` row.

## Tools

| Tool | Notes |
|---|---|
| `youtube_auth_status` | **Call first.** Bound channel, allowlist verdict, quota, mirror drift |
| `youtube_quota_status` | Spent / remaining / uploads still affordable |
| `youtube_upload_video` | Async; returns a job_id |
| `youtube_upload_status` | Poll a job; `verified` gates the punch row |
| `youtube_upload_captions` | `.srt`/`.sbv`/`.vtt`; replaces a same-named track |
| `youtube_transcript_to_srt` | Riverside `.txt` → `.srt` via `txt2srt.py` |
| `youtube_get_video` | Read metadata |
| `youtube_update_video` | Partial edit; unpassed fields keep their values |
| `youtube_playlist_list` | |
| `youtube_playlist_create` | Idempotent by title |
| `youtube_playlist_add` | Skips a video already present |

## Playlists worth knowing

    eTail Boston 2026   PLH3zTxT_1WVM
    Videos (SOP Step 5) PLFSxOzvLTxf9omUNTgse-3HL99Tvv-IYO
    Shorts              PLFSxOzvLTxf8LnR96lllwsPIHjRjS9VBY
    Talk Commerce       PLFSxOzvLTxf-zJhT4K7HAz-LiVFmDJ_Ak

Shorts need no special handling — YouTube classifies by vertical aspect ratio
plus runtime under three minutes. Point `playlist_id` at Shorts.

## Run

    uv run --directory /Users/brent/scripts/CB-Workspace/mcp-servers/youtube \
      python -m youtube_mcp

Registered in `.mcp.json` as `youtube`. Requires a Claude Code restart to appear.
