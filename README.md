# youtube-mcp

An MCP server for **authenticated YouTube writes** — uploading videos, attaching
real caption tracks, managing playlists, editing metadata.

It is a thin, guarded bridge over a working `yt_write.py` CLI rather than a
reimplementation of one, and most of what is interesting here is the guards.
Each exists because the obvious implementation fails in a way that produces **no
error at all**.

## The failure this server exists to prevent

YouTube channels frequently live under **Brand Accounts**. At the OAuth consent
screen, Google lists brand accounts as *peers* of your email address, not
underneath it — so the row bearing your own name is your **personal** channel,
which usually owns nothing.

Pick that row by mistake and everything still works. The token is valid. Every
API call returns HTTP 200. Uploads succeed. They just land on the wrong channel,
and you find out days later.

So `assert_safe_channel` runs before **every write**, not once at startup — a
token can be replaced underneath a running process. It costs 1 quota unit to
protect a 1,600-unit upload.

```
youtube_auth_status()
→ { ok: true, channels: [{ title: "…", allowed: true }], quota: {…} }
```

Call it first in any session that will write.

## Uploads are asynchronous, and that is not a style choice

A 134 MB file takes minutes at 8 MB chunks; hour-long recordings run past
400 MB. A synchronous tool call reports a timeout while the transfer is still
running — and the natural response, retrying, burns a second **1,600 quota
units** against a 10,000/day cap.

```
job = youtube_upload_video(file="/abs/path.mp4", title="…")
      → { job_id: "a1b2c3", status: "queued" }

youtube_upload_status(job_id="a1b2c3")
      → { status: "uploading", progress: 45 }
      → { status: "done", verified: true, url: "https://youtu.be/…" }
```

`verified: true` appears only after a **read-back** confirms the video exists and
is owned by this token. If you are closing a task or a checklist row on the
strength of an upload, gate it on `verified`, not on an id coming back from the
insert call.

Jobs are in-memory and do not survive a restart. If you lose one, check the
channel before re-uploading.

## Quota is the real ceiling

| Operation | Units |
|---|---|
| `videos.insert` | **1,600** |
| `captions.insert` | ~400 |
| metadata write | ~50 |
| read / list | 1 |

Against a default 10,000/day project quota, that is **4–5 uploads per day**. A
backfill of any size is a multi-day plan, not an afternoon.

The ledger resets at midnight **US Pacific** and refuses an upload *before*
spending when the day is gone — a `videos.insert` that dies mid-transfer still
costs the full 1,600, and so does the retry. It counts only what this server
spent, so treat it as a floor: usage from the CLI or another client is invisible
to it.

## Enforced locally, not left to the API

- **`privacy` defaults to `private`.** Never public by default. Schedule with
  `publish_at` instead of publishing straight out.
- **`publish_at` requires `privacy=private`.** YouTube rejects it otherwise;
  caught locally so you get a sentence instead of an opaque 400.
- **Maximum 15 tags.** The 16th is *refused*, never silently trimmed — a quietly
  dropped tag is worse than an error.

## What it deliberately does not do

**End screens and info cards.** They are not in the YouTube Data API at all —
Studio-only, permanently. Any tool claiming to set them would be lying, so there
isn't one. `youtube_upload_status` returns a `still_manual` reminder instead.

## Tools

| Tool | Notes |
|---|---|
| `youtube_auth_status` | **Call first.** Bound channel, allowlist verdict, quota |
| `youtube_quota_status` | Spent / remaining / uploads still affordable |
| `youtube_upload_video` | Async; returns a `job_id` |
| `youtube_upload_status` | Poll a job; `verified` is the gate |
| `youtube_upload_captions` | `.srt`/`.sbv`/`.vtt`; replaces a same-named track |
| `youtube_transcript_to_srt` | Speaker-timestamped `.txt` → `.srt` |
| `youtube_get_video` | Read metadata |
| `youtube_update_video` | Partial edit; unpassed fields keep their values |
| `youtube_playlist_list` | |
| `youtube_playlist_create` | Idempotent by title |
| `youtube_playlist_add` | Skips a video already present |

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and a `yt_write.py`
CLI exposing `_service()`, `_resumable()` and channel constants.

```bash
uv sync
```

Add to `.mcp.json`:

```json
{
  "mcpServers": {
    "youtube": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/youtube-mcp", "python", "-m", "youtube_mcp"],
      "env": {
        "YT_WRITE_PATH": "/path/to/yt_write.py"
      }
    }
  }
}
```

### Configuration

| Variable | Required | Meaning |
|---|---|---|
| `YT_WRITE_PATH` | **yes** | Absolute path to the `yt_write.py` this server wraps |
| `YT_WRITE_MIRROR_PATH` | no | A second copy to sha-compare against (see below) |
| `TXT2SRT_PATH` | no | Defaults to `txt2srt.py` beside `YT_WRITE_PATH` |
| `YOUTUBE_ALLOWED_CHANNELS` | no | `UCxxx=Label,UCyyy=Label`; defaults to `_KNOWN` in the wrapped module |
| `YOUTUBE_DENIED_CHANNELS` | no | Same format; channels to hard-reject |

`YT_WRITE_PATH` has no default on purpose. A default path for the one file this
server bridges would be a silent fallback over an endpoint — the class of bug
that hides a misconfiguration until it matters.

If your setup keeps two copies of `yt_write.py` at different paths, set
`YT_WRITE_MIRROR_PATH`. They are separate inodes, so editing one and not the
other is a live trap; `youtube_auth_status` sha-compares them and reports drift
rather than picking one silently.

At least one allowed channel must resolve, or the server refuses to start —
without an allowlist it cannot distinguish a correct upload target from a
silently wrong one, which is the whole point.

## Auth

Credentials live wherever the wrapped `yt_write.py` puts them (`CONFIG_DIR`),
typically `client_secret.json` + `token.json` at chmod 600. Scopes: `youtube`,
`youtube.force-ssl`, `youtube.upload`.

**`auth` cannot run from a tool call.** It opens a browser and blocks on a
localhost callback, which would hang the MCP client with no way out. When the
token is missing, tools return an actionable error naming the command a human
should run:

```bash
python /path/to/yt_write.py auth
```

At the chooser, pick the **Brand Account** row — never the personal row bearing
your own name.

Two related traps worth knowing: Brand Accounts are not members of a Workspace
org, so an **Internal** OAuth app returns `Error 403: org_internal` — the app
must be **External**. And it must be **published to Production**; Testing mode
expires refresh tokens every 7 days.

## Implementation note: stdout is the transport

MCP stdio servers speak JSON-RPC on stdout. The wrapped CLI prints constantly —
progress lines, banners, reminders — so **one stray print corrupts the stream**
and the client drops the connection. Every call into that module runs inside a
`redirect_stdout` buffer, surfaced as `cli_output` where it is useful.

If you fork this to wrap a different CLI, that is the part to keep.

## License

MIT
