"""FastMCP singleton, the yt_write bridge, the channel guard, and the quota ledger.

WHY THE UPLOAD RUNS LOCALLY RATHER THAN SERVER-SIDE
---------------------------------------------------
Episode video files are large -- 134 MB is a short one, and an hour-long
recording runs past 400 MB -- and they live on the operator's own machine,
alongside the OAuth token. Routing them through an application API to reach
Google would mean a double transfer into storage that API does not have, plus
moving a refresh token into a server-side secret store for no gain. So the
upload runs where the files and the token already are.

That keeps the division of labour clean for anyone pairing this with a content
system: this server owns the UPLOAD, and the system of record owns the RECORD
of it. It touches no database of any kind.

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

# ---------------------------------------------------------------------------
# Configuration -- all of it from the environment or from the wrapped module.
# ---------------------------------------------------------------------------
# Nothing operator-specific is hardcoded here: no home directories, no channel
# ids, no credential paths. YT_WRITE_PATH is REQUIRED and has no default,
# because a default path for the one file this server is a bridge over would be
# a silent fallback over an endpoint -- exactly the pattern that hides a
# misconfiguration until it matters.

def _required_env(name: str, why: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"{name} is not set. {why}\n"
            "Set it in the `env` block of this server's entry in .mcp.json."
        )
    return val


YT_WRITE_CANONICAL = Path(_required_env(
    "YT_WRITE_PATH",
    "It must be the absolute path to yt_write.py, the CLI this server wraps."))

# Optional. Some installs keep a second copy of yt_write.py at another path;
# they are separate inodes, so editing one and not the other is a live trap.
# We load the canonical one and REPORT any divergence rather than picking
# silently -- a quiet `a or b` over two sources of truth is how the wrong one
# ends up live.
_mirror_env = os.environ.get("YT_WRITE_MIRROR_PATH", "").strip()
YT_WRITE_MIRROR = Path(_mirror_env) if _mirror_env else None

_txt2srt_env = os.environ.get("TXT2SRT_PATH", "").strip()
TXT2SRT = Path(_txt2srt_env) if _txt2srt_env else YT_WRITE_CANONICAL.parent / "txt2srt.py"

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

# Credential location comes from the wrapped module, so there is exactly one
# definition of where the token lives rather than two that can disagree.
CONFIG_DIR = Path(yt_write.CONFIG_DIR)
QUOTA_FILE = CONFIG_DIR / "quota.json"


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

    canon = _digest(YT_WRITE_CANONICAL)
    if YT_WRITE_MIRROR is None:
        return {"in_sync": True, "sha": canon,
                "note": "No YT_WRITE_MIRROR_PATH configured; nothing to compare."}
    mirror = _digest(YT_WRITE_MIRROR)
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
# YouTube channels often sit under BRAND ACCOUNTS, which the OAuth chooser lists
# as PEERS of your email rather than underneath it. Pick the personal row by
# mistake and the token binds to a personal channel that owns nothing -- then
# every API call returns HTTP 200 and uploads land THERE, silently. That is not
# hypothetical; it happened three times in one afternoon before this guard
# existed.
#
# The lists come from the wrapped module so there is one definition rather than
# two that drift, and can be overridden per install without touching code.
def _parse_channel_env(name: str) -> dict[str, str]:
    """Parse `id=label,id=label` into a dict. Empty env -> {}."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        cid, _, label = part.partition("=")
        out[cid.strip()] = label.strip() or cid.strip()
    return out


KNOWN_CHANNELS: dict[str, str] = (
    _parse_channel_env("YOUTUBE_ALLOWED_CHANNELS") or dict(getattr(yt_write, "_KNOWN", {})))
WRONG_CHANNELS: dict[str, str] = (
    _parse_channel_env("YOUTUBE_DENIED_CHANNELS") or dict(getattr(yt_write, "_WRONG", {})))

if not KNOWN_CHANNELS:
    raise RuntimeError(
        "No allowed channels configured. Set YOUTUBE_ALLOWED_CHANNELS to "
        "'UCxxxx=My Channel,UCyyyy=Other' or define _KNOWN in the wrapped "
        "yt_write.py. Without an allowlist this server cannot tell a correct "
        "upload target from a silent wrong-channel one, which is the single "
        "failure it exists to prevent."
    )


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
            "Run this in a terminal, and pick the BRAND ACCOUNT row at the chooser "
            "-- NOT the personal row bearing your own name:\n"
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
            "Quota rolls over at midnight US Pacific. At 1,600 units per upload a "
            "single day buys 4-5 videos, so plan a backfill across days."
        )


mcp = FastMCP(
    "youtube",
    instructions=(
        "Authenticated YouTube writes: uploads, captions, playlists, metadata.\n\n"
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
