"""FastMCP singleton, the yt_write bridge, the channel guard, and the quota ledger.

WHY THIS SERVER IS SEPARATE FROM THE REQUESTDESK MCP
----------------------------------------------------
A 134 MB video (Kate Farms; longer episodes run 400 MB+) lives on Brent's Mac.
Routing it through app.requestdesk.ai to reach Google would be a double transfer
into storage the API does not have. The OAuth token is likewise local, at
`~/.config/brent-youtube/`. So the upload runs where the files and the token
already are.

This does not violate "the requestdesk MCP never touches Mongo" -- this is not
the requestdesk MCP, and it touches no database at all. It is the same shape as
the slack / trello / transistor / quickbooks servers: an MCP that fronts one
vendor API.

Division of labour:
    youtube MCP     owns the UPLOAD          (this server)
    requestdesk MCP owns the RECORD          (episode_youtube_upload -> punch_set)

WHY IT IMPORTS yt_write.py INSTEAD OF REIMPLEMENTING IT
--------------------------------------------------------
Every guard in that file was written after a real failure on 2026-08-13. A
parallel implementation drifts and loses them one at a time. `yt_write.py`
imports google libraries lazily inside its functions, so importing the module
costs nothing but stdlib -- which is exactly what makes this bridge cheap.
"""

import hashlib
import json
import logging
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from importlib import util as _importlib_util
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(name)s - %(message)s")
logger = logging.getLogger(__name__)

WORKSPACE = Path("/Users/brent/scripts/CB-Workspace")

# The canonical copy and its mirror. Dirac's warning: these are two real files
# with separate inodes, and editing one without the other is a live trap. We
# load the canonical one and REPORT the divergence rather than picking silently
# -- a quiet `a or b` over two credential-shaped sources is the exact pattern
# the workspace bans.
YT_WRITE_CANONICAL = WORKSPACE / "claude-shared/skills/youtube/yt_write.py"
YT_WRITE_MIRROR = WORKSPACE / ".claude/skills/youtube/yt_write.py"
TXT2SRT = WORKSPACE / "claude-shared/skills/youtube/txt2srt.py"

CONFIG_DIR = Path(os.path.expanduser("~/.config/brent-youtube"))
QUOTA_FILE = CONFIG_DIR / "quota.json"

# YouTube Data API v3 unit costs. The project cap is 10,000/day, which makes
# videos.insert the binding constraint at 4-5 episodes per day.
QUOTA_DAILY_LIMIT = 10_000
COST_VIDEOS_INSERT = 1_600
COST_CAPTIONS_INSERT = 400
COST_WRITE = 50
COST_READ = 1


def _load_module(path: Path, name: str):
    """Import a module from an absolute path. Raises if it is not there.

    No fallback and no empty-module-on-failure: if the reference implementation
    is missing, every guard below is missing with it, and a server that starts
    anyway would happily upload to the wrong channel.
    """
    if not path.exists():
        raise RuntimeError(
            f"{name} not found at {path}. This server is a bridge over that file "
            "and cannot run without it."
        )
    spec = _importlib_util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not build an import spec for {path}")
    mod = _importlib_util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


yt_write = _load_module(YT_WRITE_CANONICAL, "yt_write_ref")
_txt2srt = None  # loaded on demand; only the srt tool needs it


def txt2srt_module():
    global _txt2srt
    if _txt2srt is None:
        _txt2srt = _load_module(TXT2SRT, "txt2srt_ref")
    return _txt2srt


def mirror_status() -> dict[str, Any]:
    """Whether the mirrored copy of yt_write.py has drifted from the canonical one."""
    def _digest(p: Path) -> str | None:
        if not p.exists():
            return None
        return hashlib.sha256(p.read_bytes()).hexdigest()[:12]

    canon, mirror = _digest(YT_WRITE_CANONICAL), _digest(YT_WRITE_MIRROR)
    if mirror is None:
        return {"in_sync": False, "reason": "mirror missing", "mirror": str(YT_WRITE_MIRROR)}
    if canon != mirror:
        return {
            "in_sync": False,
            "reason": "mirror has DRIFTED from canonical -- edit both, they are separate inodes",
            "canonical_sha": canon,
            "mirror_sha": mirror,
            "canonical": str(YT_WRITE_CANONICAL),
            "mirror": str(YT_WRITE_MIRROR),
        }
    return {"in_sync": True, "sha": canon}


# ---------------------------------------------------------------------------
# The channel guard -- the single most important thing in this server.
# ---------------------------------------------------------------------------
# A token bound to Brent's PERSONAL channel returns HTTP 200 on every call and
# uploads land there with no error whatsoever. Dirac hit this three times on
# 2026-08-13. Allowlist and denylist are re-exported from yt_write so there is
# one definition, not two.
KNOWN_CHANNELS: dict[str, str] = dict(yt_write._KNOWN)
WRONG_CHANNELS: dict[str, str] = dict(yt_write._WRONG)


class WrongChannel(RuntimeError):
    """The token is bound to a channel that must never receive a write."""


class NoToken(RuntimeError):
    """No OAuth token on disk. A human must run `yt_write.py auth` in a terminal."""


def token_exists() -> bool:
    return Path(yt_write.TOKEN_FILE).exists()


def require_token() -> None:
    """Refuse to touch Google when no token exists.

    `auth` opens a browser and blocks on a localhost callback, so it can never
    run inside a tool call -- it would hang the MCP client with no way out.
    The only correct response is an error a human can act on.
    """
    if not token_exists():
        raise NoToken(
            "No YouTube token at "
            f"{yt_write.TOKEN_FILE}.\n"
            "This CANNOT be fixed from a tool call: the consent flow opens a browser "
            "and blocks on a localhost callback.\n"
            "Run this in a terminal, and pick the BRAND ACCOUNT row (not 'Brent Peterson'):\n"
            f"  {WORKSPACE}/.claude/local/youtube-venv/bin/python "
            f"{YT_WRITE_CANONICAL} auth"
        )


def service():
    """An authorized YouTube client, or a loud refusal."""
    require_token()
    return yt_write._service()


def bound_channels(yt) -> list[dict[str, Any]]:
    resp = yt.channels().list(part="snippet,statistics", mine=True).execute()
    out = []
    for it in resp.get("items", []):
        st = it.get("statistics", {})
        cid = it["id"]
        out.append({
            "channel_id": cid,
            "title": it["snippet"]["title"],
            "subscribers": st.get("subscriberCount"),
            "videos": st.get("videoCount"),
            "allowed": cid in KNOWN_CHANNELS,
            "denied": cid in WRONG_CHANNELS,
            "note": WRONG_CHANNELS.get(cid) or KNOWN_CHANNELS.get(cid) or "unrecognized channel",
        })
    return out


def assert_safe_channel(yt) -> list[dict[str, Any]]:
    """Refuse to write when the token is bound anywhere it should not be.

    Called by EVERY write tool, not just auth_status. Checking once at startup
    would not help: the token can be replaced underneath a running server.
    Costs 1 quota unit, against 1,600 for the upload it protects.
    """
    chans = bound_channels(yt)
    if not chans:
        raise WrongChannel(
            "This token owns NO channels -- the identity picked at consent owns nothing. "
            f"Delete {yt_write.TOKEN_FILE} and re-run `auth`, choosing the BRAND ACCOUNT row."
        )
    bad = [c for c in chans if c["denied"] or not c["allowed"]]
    if bad:
        lines = "\n".join(f"  - {c['title']} ({c['channel_id']}): {c['note']}" for c in bad)
        raise WrongChannel(
            "REFUSING TO WRITE. This token is bound to a channel that is not on the "
            f"allowlist:\n{lines}\n"
            "Uploads would land there and Google would return 200 with no error.\n"
            f"Fix: rm {yt_write.TOKEN_FILE} && re-run `auth`, picking the BRAND ACCOUNT row.\n"
            f"Allowed: {json.dumps(KNOWN_CHANNELS, indent=2)}"
        )
    return chans


# ---------------------------------------------------------------------------
# Quota ledger
# ---------------------------------------------------------------------------
# YouTube resets quota at midnight PACIFIC, not UTC and not local. Tracked in a
# small JSON file so the count survives a server restart mid-day.
_quota_lock = threading.Lock()


def _quota_day() -> str:
    """Today's date in US Pacific, which is when YouTube's quota rolls over."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        # Fixed -8 is wrong for half the year, so say so rather than pretend.
        now = datetime.now(timezone.utc) - timedelta(hours=8)
    return now.strftime("%Y-%m-%d")


def quota_read() -> dict[str, Any]:
    day = _quota_day()
    try:
        data = json.loads(QUOTA_FILE.read_text())
    except Exception:
        data = {}
    if data.get("day") != day:
        data = {"day": day, "spent": 0, "events": []}
    spent = int(data.get("spent", 0))
    return {
        "day": day,
        "spent": spent,
        "remaining": max(0, QUOTA_DAILY_LIMIT - spent),
        "limit": QUOTA_DAILY_LIMIT,
        "uploads_left": max(0, QUOTA_DAILY_LIMIT - spent) // COST_VIDEOS_INSERT,
        "events": data.get("events", [])[-20:],
        "note": "Estimated from this server's own writes. Anything spent by the CLI "
                "or another client is not counted here, so treat it as a floor.",
    }


def quota_charge(units: int, label: str) -> dict[str, Any]:
    with _quota_lock:
        cur = quota_read()
        day, spent = cur["day"], cur["spent"] + units
        events = cur["events"] + [{"label": label, "units": units,
                                   "at": datetime.now(timezone.utc).isoformat()}]
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        QUOTA_FILE.write_text(json.dumps(
            {"day": day, "spent": spent, "events": events[-50:]}, indent=2))
        try:
            os.chmod(QUOTA_FILE, 0o600)
        except OSError:
            pass
    return quota_read()


def quota_guard(units: int, label: str) -> None:
    """Refuse BEFORE spending when the day is already gone.

    A videos.insert that dies mid-transfer still costs 1,600 units, and the
    retry costs another 1,600. Refusing up front is the difference between
    losing a day and losing two.
    """
    cur = quota_read()
    if cur["remaining"] < units:
        raise RuntimeError(
            f"QUOTA: {label} needs {units} units and only {cur['remaining']} remain "
            f"of {QUOTA_DAILY_LIMIT} for {cur['day']} (US Pacific).\n"
            "Refusing up front -- a failed videos.insert still burns the full 1,600 "
            "and the retry burns it again.\n"
            "Quota rolls over at midnight Pacific. There are 26 unpublished episodes; "
            "a full backfill is a five-day burn at 4-5 per day."
        )


mcp = FastMCP(
    "youtube",
    instructions=(
        "Authenticated YouTube writes for Brent's channels (Talk Commerce, Content "
        "Cucumber).\n\n"
        "ALWAYS call `youtube_auth_status` first in a session. A token bound to the "
        "wrong channel returns HTTP 200 on every call and uploads land on an empty "
        "personal channel with no error at all.\n\n"
        "Uploads default to private and are ASYNCHRONOUS: `youtube_upload_video` "
        "returns a job_id immediately, then poll `youtube_upload_status`. A 134 MB "
        "file takes minutes and a synchronous call would time out while the upload "
        "was still running.\n\n"
        "End screens and info cards are NOT in the YouTube Data API. They are "
        "Studio-only, permanently. No tool here does them; the SOP step always ends "
        "with a manual pass."
    ),
)
