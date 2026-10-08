# auto_monitor.py - Robust Auto Monitor
#
# Main goals:
# - Check every non-active watchlist user.
# - Classify every checked user before starting recordings.
# - Never let the recording limit stop result classification.
# - Keep total active recordings <= 5 from this monitor's point of view.
# - Treat m3u8 as evidence of a live stream, but let the Tango API
#   override it when the API explicitly identifies Premium.
# - Handle boolean Premium fields correctly.
# - Keep Telegram summary accurate and internally consistent.

import asyncio
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from playwright.async_api import async_playwright


# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
PAT_TOKEN = os.environ.get("PAT_TOKEN", "")

GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "kalausr8")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "telegram-hls-recorder")

WATCHLIST_PATH = ".recorder/config/watchlist.json"
IDENTITY_PATH = ".recorder/config/identities.json"

# Maximum number of users checked concurrently.
# This is NOT the recording limit.
MAX_CONCURRENT_USERS = 5

# Maximum total active recordings allowed.
MAX_TOTAL_RECORDINGS = 5

# Maximum time spent waiting for stream/API evidence per user.
MAX_WAIT_SECONDS = 12.0

# Diagnostic mode: NEVER dispatch recordings from this run.
# The purpose is to observe Tango network behaviour safely.
DIAGNOSTIC_ONLY = False

# Maximum number of diagnostic entries printed per user for noisy traffic.
MAX_DIAGNOSTIC_NETWORK_LOGS = 80

# Poll interval while waiting for network evidence.
POLL_INTERVAL = 0.5

# Safety settings.
MAX_RECORDING_AGE_HOURS = 6
MAX_STATUS_STALE_MINUTES = 10
MALFORMED_LEASE_GRACE_MINUTES = 30


# ============================================================
# Stream Classification
# ============================================================

class StreamStatus:
    OFFLINE = "OFFLINE"
    LIVE_NORMAL = "LIVE_NORMAL"
    LIVE_PREMIUM = "LIVE_PREMIUM"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


# ============================================================
# Logging
# ============================================================

def log(message):
    print(message, flush=True)


# ============================================================
# Telegram
# ============================================================

async def telegram_request(method, data=None):
    if not BOT_TOKEN:
        log("[TELEGRAM] BOT_TOKEN is missing.")
        return None

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    timeout = aiohttp.ClientTimeout(total=30)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=data or {}) as response:
                text = await response.text()
                log(f"[TELEGRAM] {method}: HTTP {response.status}")

                if response.status != 200:
                    log(f"[TELEGRAM] Response: {text[:1000]}")

                return response.status, text

    except Exception as exc:
        log(f"[TELEGRAM] Error: {exc}")

    return None


async def send_message(text):
    if not ADMIN_USER_ID:
        return

    await telegram_request(
        "sendMessage",
        {
            "chat_id": ADMIN_USER_ID,
            "text": text,
        },
    )


# ============================================================
# GitHub State Management
# ============================================================

async def github_get_file(path):
    if not GITHUB_TOKEN:
        return None

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    )

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "telegram-hls-recorder-monitor",
    }

    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:

                if resp.status == 404:
                    return None

                if resp.status != 200:
                    log(
                        f"[GITHUB] GET failed for {path}: "
                        f"HTTP {resp.status}"
                    )
                    return None

                data = await resp.json()
                encoded = data.get("content")

                if not encoded:
                    return None

                try:
                    raw = base64.b64decode(
                        encoded.replace("\n", "")
                    ).decode(
                        "utf-8",
                        errors="replace",
                    )

                    return json.loads(raw)

                except Exception as exc:
                    log(f"[GITHUB] Decode failed: {exc}")
                    return None

    except Exception as exc:
        log(f"[GITHUB] Exception: {exc}")
        return None


async def github_update_file(path, content, message):
    write_token = PAT_TOKEN or GITHUB_TOKEN
    if not write_token:
        return False

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    )

    headers = {
        "Authorization": f"Bearer {write_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "telegram-hls-recorder-monitor",
    }

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:

            sha = None

            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    sha = (await resp.json()).get("sha")
                elif resp.status != 404:
                    return False

            raw = json.dumps(
                content,
                ensure_ascii=False,
                indent=2,
            ) + "\n"

            body = {
                "message": message,
                "content": base64.b64encode(
                    raw.encode()
                ).decode(),
            }

            if sha:
                body["sha"] = sha

            async with session.put(
                url,
                headers=headers,
                json=body,
            ) as resp:

                if resp.status not in (200, 201):
                    log(
                        f"[GITHUB] Update failed for {path}: "
                        f"HTTP {resp.status}"
                    )
                    return False

                return True

    except Exception as exc:
        log(
            f"[GITHUB] Update exception for {path}: "
            f"{type(exc).__name__}: {exc}"
        )
        return False


async def github_get_file_last_commit_time(path):
    if not GITHUB_TOKEN:
        return None

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/{GITHUB_REPO}/commits"
        f"?path={path}&per_page=1"
    )

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "telegram-hls-recorder-monitor",
    }

    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:

                if resp.status != 200:
                    return None

                data = await resp.json()

                if data and len(data) > 0:
                    commit_date = (
                        data[0]
                        .get("commit", {})
                        .get("committer", {})
                        .get("date", "")
                    )

                    if commit_date:
                        return datetime.fromisoformat(
                            commit_date.replace("Z", "+00:00")
                        )

                return None

    except Exception as exc:
        log(
            "[GITHUB] Exception getting status commit time "
            f"for {path}: {exc}"
        )
        return None


async def github_delete_file(path, message):
    if not PAT_TOKEN:
        log("[GITHUB] Cannot delete: PAT_TOKEN missing")
        return

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    )

    headers = {
        "Authorization": f"Bearer {PAT_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "telegram-hls-recorder-monitor",
    }

    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:

            sha = None

            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    sha = data.get("sha")

            if not sha:
                return

            body = {
                "message": message,
                "sha": sha,
            }

            async with session.delete(
                url,
                headers=headers,
                json=body,
            ) as resp:

                if resp.status not in (200, 204):
                    log(
                        f"[GITHUB] Delete failed for {path}: "
                        f"HTTP {resp.status}"
                    )
                else:
                    log(f"[GITHUB] ✅ Deleted {path}")

    except Exception as exc:
        log(
            f"[GITHUB] Exception deleting {path}: {exc}"
        )


async def is_recording_actually_alive(record_data):
    """Verify whether an active lease is still genuinely active."""

    if not isinstance(record_data, dict):
        return False

    record_id = str(
        record_data.get("record_id") or "UNKNOWN"
    )

    # ------------------------------------------------------------
    # Malformed lease:
    # Do NOT keep it active forever.
    # Use its timestamp to determine whether it is an old orphan.
    # ------------------------------------------------------------
    if (
        not record_data.get("username")
        or not record_data.get("record_id")
    ):
        timestamp_str = (
            record_data.get("heartbeat_at")
            or record_data.get("started_at")
            or record_data.get("claimed_at")
            or ""
        )

        try:
            lease_time = datetime.fromisoformat(
                str(timestamp_str).replace("Z", "+00:00")
            )

            age_minutes = (
                datetime.now(timezone.utc) - lease_time
            ).total_seconds() / 60

            if age_minutes > MALFORMED_LEASE_GRACE_MINUTES:
                log(
                    f"[VERIFY] 🗑️ {record_id}: malformed lease older than "
                    f"{MALFORMED_LEASE_GRACE_MINUTES}m -> removable"
                )
                return False

            log(
                f"[VERIFY] ⚠️ {record_id}: malformed lease is recent "
                f"({age_minutes:.1f}m) -> KEEPING ACTIVE"
            )
            return True

        except Exception:
            log(
                f"[VERIFY] ⚠️ {record_id}: malformed lease has no valid "
                "timestamp -> KEEPING ACTIVE"
            )
            return True

    state = str(
        record_data.get("state") or "unknown"
    ).lower()

    started_at_str = (
        record_data.get("started_at")
        or record_data.get("claimed_at")
        or ""
    )

    heartbeat_str = (
        record_data.get("heartbeat_at")
        or record_data.get("claimed_at")
        or started_at_str
    )

    try:
        start_time = datetime.fromisoformat(
            str(started_at_str).replace("Z", "+00:00")
        )

        age_hours = (
            datetime.now(timezone.utc) - start_time
        ).total_seconds() / 3600

        if age_hours > MAX_RECORDING_AGE_HOURS:
            log(
                f"[VERIFY] ❌ {record_id}: lease older than "
                f"{MAX_RECORDING_AGE_HOURS}h -> removable"
            )
            return False

    except Exception:
        log(
            f"[VERIFY] ⚠️ {record_id}: invalid timestamp -> KEEPING ACTIVE"
        )
        return True

    try:
        heartbeat_time = datetime.fromisoformat(
            str(heartbeat_str).replace("Z", "+00:00")
        )

        stale_minutes = (
            datetime.now(timezone.utc) - heartbeat_time
        ).total_seconds() / 60

    except Exception:
        log(
            f"[VERIFY] ⚠️ {record_id}: invalid heartbeat -> KEEPING ACTIVE"
        )
        return True

    if state == "starting":

        if stale_minutes <= 15:
            log(
                f"[VERIFY] ✅ {record_id}: STARTING lease fresh "
                f"({stale_minutes:.1f}m)"
            )
            return True

        log(
            f"[VERIFY] 🗑️ {record_id}: abandoned STARTING lease "
            f"({stale_minutes:.1f}m)"
        )
        return False

    if stale_minutes <= MAX_STATUS_STALE_MINUTES:
        log(
            f"[VERIFY] ✅ {record_id}: {state.upper()} heartbeat fresh "
            f"({stale_minutes:.1f}m)"
        )
        return True

    # Conservative safety rule: never free a slot solely because a
    # GitHub status commit is stale or missing.
    status_path = f".recorder/status/{record_id}.json"

    status_data = await github_get_file(status_path)

    if status_data is None:
        log(
            f"[VERIFY] ⚠️ {record_id}: heartbeat stale and status unavailable "
            "-> KEEPING ACTIVE"
        )
        return True

    last_commit_time = await github_get_file_last_commit_time(
        status_path
    )

    if last_commit_time is None:
        log(
            f"[VERIFY] ⚠️ {record_id}: cannot verify status freshness "
            "-> KEEPING ACTIVE"
        )
        return True

    status_stale_minutes = (
        datetime.now(timezone.utc) - last_commit_time
    ).total_seconds() / 60

    if status_stale_minutes <= MAX_STATUS_STALE_MINUTES:
        log(
            f"[VERIFY] ✅ {record_id}: status commit fresh "
            f"({status_stale_minutes:.1f}m)"
        )
        return True

    log(
        f"[VERIFY] ⚠️ {record_id}: heartbeat/status stale "
        f"({status_stale_minutes:.1f}m) -> KEEPING ACTIVE"
    )
    return True


async def github_get_active_recordings():
    """Read active leases and fail closed on malformed/unknown state."""

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/{GITHUB_REPO}/git/trees/main?recursive=1"
    )

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "telegram-hls-recorder-monitor",
    }

    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.get(
                url,
                headers=headers,
            ) as resp:

                if resp.status != 200:
                    log(
                        "[GITHUB] Failed to read repository tree: "
                        f"HTTP {resp.status} -> ABORTING active-state trust"
                    )

                    # Fail closed: do not claim capacity when state cannot
                    # be read. Returning None prevents any new dispatches.
                    return None

                data = await resp.json()

                tree = data.get("tree", [])

                active_files = [
                    item
                    for item in tree
                    if (
                        isinstance(item, dict)
                        and str(
                            item.get("path", "")
                        ).startswith(".recorder/active/")
                        and str(
                            item.get("path", "")
                        ).endswith(".json")
                    )
                ]

                if not active_files:
                    log(
                        "[MONITOR] No active recordings found"
                    )
                    return []

                log(
                    f"[MONITOR] Found {len(active_files)} "
                    "active lease(s), verifying..."
                )

                active_recordings = []

                for file_info in active_files:

                    path = file_info["path"]

                    file_data = await github_get_file(path)

                    # ----------------------------------------------------
                    # Non-dict / unreadable lease.
                    #
                    # This is different from a valid JSON object with
                    # missing fields. The latter is handled by
                    # is_recording_actually_alive().
                    #
                    # We cannot safely inspect timestamps/state here,
                    # therefore keep the conservative behaviour.
                    # ----------------------------------------------------
                    if not isinstance(file_data, dict):

                        record_id = Path(path).stem

                        log(
                            f"[VERIFY] ⚠️ {record_id}: malformed lease "
                            "cannot be verified from JSON -> "
                            "checking timestamp/state"
                        )

                        # If the file itself is not valid JSON or cannot
                        # be represented as a dict, we cannot safely verify
                        # its internal timestamp/state.
                        #
                        # Keep the existing conservative behaviour here.
                        active_recordings.append(
                            {
                                "username": (
                                    f"unknown:{record_id}"
                                ).lower(),
                                "record_id": record_id,
                                "path": path,
                            }
                        )

                        continue

                    is_alive = await is_recording_actually_alive(
                        file_data
                    )

                    if is_alive:

                        username = str(
                            file_data.get("username")
                            or f"unknown:{Path(path).stem}"
                        )

                        record_id = str(
                            file_data.get("record_id")
                            or Path(path).stem
                        )

                        active_recordings.append(
                            {
                                "username": username.lower(),
                                "record_id": record_id,
                                "path": path,
                            }
                        )

                        log(
                            f"[MONITOR] ✅ active lease: "
                            f"{username} (#{record_id})"
                        )

                    else:

                        record_id = str(
                            file_data.get("record_id")
                            or Path(path).stem
                        )

                        log(
                            f"[MONITOR] 🗑️ abandoned lease "
                            f"#{record_id}: cleaning up"
                        )

                        await github_delete_file(
                            path,
                            f"Cleanup abandoned lease {record_id}",
                        )

                        await github_delete_file(
                            f".recorder/status/{record_id}.json",
                            f"Cleanup abandoned status {record_id}",
                        )

                return active_recordings

    except Exception as exc:

        log(
            "[GITHUB] Exception getting active recordings: "
            f"{type(exc).__name__}: {exc} "
            "-> ABORTING capacity decisions"
        )

        return None


# Backwards-compatible helper for code that only needs usernames.
async def github_get_active_usernames():

    active_recordings = await github_get_active_recordings()

    return [
        recording["username"]
        for recording in active_recordings
    ]


# ============================================================
# GitHub Actions Trigger
# ============================================================

def generate_record_id():

    import random
    import string

    chars = string.ascii_uppercase + string.digits

    return "".join(
        random.choice(chars)
        for _ in range(6)
    )


async def trigger_recording(
    stream_url,
    username,
    display_name="",
    account_id="",
):

    if not PAT_TOKEN:

        log(
            "[GITHUB] Cannot trigger recording: "
            "PAT_TOKEN missing"
        )

        await send_message(
            "❌ خطأ في الإعدادات: PAT_TOKEN غير موجود."
        )

        return False

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/{GITHUB_REPO}/dispatches"
    )

    headers = {
        "Authorization": f"Bearer {PAT_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "telegram-hls-recorder-monitor",
    }

    record_id = generate_record_id()

    # Reserve the slot BEFORE repository_dispatch so another monitor run
    # cannot start a duplicate recording during GitHub Actions startup.
    lease = {
        "record_id": record_id,
        "url": stream_url,
        "username": username,
        "display_name": display_name or username,
        "account_id": account_id,
        "state": "starting",
        "started_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "heartbeat_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    lease_path = (
        f".recorder/active/{record_id}.json"
    )

    if not await github_update_file(
        lease_path,
        lease,
        f"Reserve recording {record_id}",
    ):

        log(
            f"[GITHUB] ❌ Could not reserve lease "
            f"for {record_id}; dispatch cancelled"
        )

        return False

    body = {
        "event_type": "telegram_record",
        "client_payload": {
            "url": stream_url,
            "record_id": record_id,
            "username": username,
            "display_name": display_name or username,
            "account_id": account_id,
        },
    }

    timeout = aiohttp.ClientTimeout(total=15)

    try:

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.post(
                url,
                headers=headers,
                json=body,
            ) as resp:

                if resp.status == 204:

                    log(
                        f"[GITHUB] Successfully triggered recording "
                        f"for {username} (ID: {record_id})"
                    )

                    await send_message(
                        f"🔴 Auto-Record: بدأ تشغيل تسجيل "
                        f"{display_name or username} تلقائياً!"
                    )

                    return True

                text = await resp.text()

                log(
                    f"[GITHUB] Trigger failed: "
                    f"HTTP {resp.status} - {text[:500]}"
                )

                await github_delete_file(
                    lease_path,
                    f"Remove failed reservation {record_id}",
                )

                return False

    except Exception as exc:

        log(
            f"[GITHUB] Exception triggering recording: {exc}"
        )

        await github_delete_file(
            lease_path,
            f"Remove failed reservation {record_id}",
        )

        return False


# ============================================================
# Premium Detection
# ============================================================

PREMIUM_FIELDS = [
    "type",
    "payType",
    "isPremium",
    "premium",
    "vip",
    "exclusive",
    "locked",
    "private",
    "paid",
    "streamType",
]

PREMIUM_KEYWORDS = {
    "premium",
    "paid",
    "vip",
    "exclusive",
    "locked",
    "private",
}


def value_indicates_premium(value):
    """
    Safely interpret common Premium representations.

    Important:
    - True => Premium
    - "true" => Premium
    - strings such as "premium", "paid", "vip" => Premium
    - False => NOT Premium
    - "false" => NOT Premium
    - None / empty => unknown
    """

    if value is None:
        return False

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value == 1

    if isinstance(value, str):

        normalized = value.strip().lower()

        if not normalized:
            return False

        if normalized in {
            "true",
            "yes",
            "1",
            "premium",
            "paid",
            "vip",
            "exclusive",
            "locked",
            "private",
        }:
            return True

        if normalized in {
            "false",
            "no",
            "0",
            "normal",
            "free",
            "public",
        }:
            return False

        if any(
            keyword in normalized
            for keyword in PREMIUM_KEYWORDS
        ):
            return True

    return False


def is_premium_stream(
    stream_data,
    details_data,
):
    """
    Return True only when the supplied stream/details data
    contains an explicit Premium-like indicator.

    We intentionally do NOT classify:
        stream exists + missing URL
    as Premium by itself.

    Missing data must not be converted into a false Premium
    assertion without evidence.
    """

    if isinstance(stream_data, dict):

        for field in PREMIUM_FIELDS:

            if field not in stream_data:
                continue

            value = stream_data.get(field)

            if value_indicates_premium(value):

                log(
                    f"[MONITOR] 🟡 Premium detected via "
                    f"stream.{field}={value!r}"
                )

                return True

    if isinstance(details_data, dict):

        for field in PREMIUM_FIELDS:

            if field not in details_data:
                continue

            value = details_data.get(field)

            if value_indicates_premium(value):

                log(
                    f"[MONITOR] 🟡 Premium detected via "
                    f"details.{field}={value!r}"
                )

                return True

    return False


# ============================================================
# Identity / username tracking
# ============================================================

def extract_username_from_url(url):

    try:

        from urllib.parse import (
            urlsplit,
            unquote,
        )

        parts = [
            x
            for x in unquote(
                urlsplit(url).path
            ).split("/")
            if x
        ]

        return parts[0] if len(parts) == 1 else ""

    except Exception:

        return ""


def extract_username_from_title(title):

    import re

    match = re.search(
        r"\(@([A-Za-z0-9._-]+)\)",
        title or "",
    )

    return match.group(1) if match else ""


def parse_display_name(
    title,
    username,
):

    title = (title or "").strip()

    marker = f"(@{username})"

    pos = title.lower().find(
        marker.lower()
    )

    if pos >= 0:

        value = (
            title[:pos]
            .strip()
            .rstrip("-")
            .strip()
        )

        if value:
            return value

    if " - Tango Live" in title:

        value = title.split(
            " - Tango Live",
            1,
        )[0].strip()

        if value:
            return value

    return username


def identity_aliases(identity):

    if not isinstance(identity, dict):
        return []

    values = [
        identity.get("username"),
        *(identity.get("aliases") or []),
    ]

    out = []

    for value in values:

        value = str(
            value or ""
        ).strip().lower()

        if value and value not in out:
            out.append(value)

    return out


def merge_identity(
    identities,
    old_username,
    observed,
    now_iso,
):

    key = old_username.lower()

    old = (
        identities.get(key)
        if isinstance(
            identities.get(key),
            dict,
        )
        else {}
    )

    old_name = str(
        old.get("username")
        or old_username
    )

    new_name = str(
        observed.get("username")
        or old_username
    ).strip()

    aliases = identity_aliases(old)

    for value in (
        old_username,
        old_name,
        new_name,
    ):

        if (
            value
            and value.lower() not in aliases
        ):
            aliases.append(
                value.lower()
            )

    changed = (
        bool(old.get("username"))
        and old_name.lower()
        != new_name.lower()
    )

    record = {
        "username": new_name,
        "display_name": str(
            observed.get("display_name")
            or old.get("display_name")
            or new_name
        ),
        "account_id": str(
            observed.get("account_id")
            or old.get("account_id")
            or ""
        ),
        "aliases": aliases,
        "first_seen_at": (
            old.get("first_seen_at")
            or now_iso
        ),
        "last_seen_at": now_iso,
        "username_changed_at": (
            now_iso
            if changed
            else old.get("username_changed_at")
        ),
        "username_change_window_days": 90,
    }

    if changed:

        record["previous_username"] = old_name

        log(
            f"[IDENTITY] 🔄 {old_name} -> "
            f"{new_name} ({record['display_name']})"
        )

    return record


def display_label(
    identity,
    username,
):

    if (
        isinstance(identity, dict)
        and str(
            identity.get("display_name")
            or ""
        ).strip()
    ):
        return str(
            identity["display_name"]
        ).strip()

    return username


# ============================================================
# Diagnostic helpers
# ============================================================

DIAGNOSTIC_PATH_KEYWORDS = (
    "/api/",
    "graphql",
    "live",
    "stream",
    "broadcast",
    "room",
    "watch",
    "player",
    "playback",
    "manifest",
    "playlist",
    ".m3u8",
    ".mpd",
    "websocket",
)


def redact_network_url(url):
    """Return a safe URL containing scheme/host/path only."""

    try:

        from urllib.parse import urlsplit

        parts = urlsplit(url)

        return (
            f"{parts.scheme}://"
            f"{parts.netloc}"
            f"{parts.path}"
        )

    except Exception:

        return (
            str(url)
            .split("?", 1)[0]
            .split("#", 1)[0]
        )


def compact_value(
    value,
    max_len=220,
):

    try:

        text = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

    except Exception:

        text = repr(value)

    if len(text) > max_len:
        return (
            text[:max_len]
            + "..."
        )

    return text


def collect_interesting_keys(
    value,
    prefix="",
    depth=0,
    limit=40,
):
    """Return key paths only; never return values/tokens."""

    found = []

    if depth > 5:
        return found

    if isinstance(value, dict):

        for key, child in value.items():

            path = (
                f"{prefix}.{key}"
                if prefix
                else str(key)
            )

            found.append(path)

            if len(found) >= limit:
                return found

            found.extend(
                collect_interesting_keys(
                    child,
                    path,
                    depth + 1,
                    max(
                        0,
                        limit - len(found),
                    ),
                )
            )

            if len(found) >= limit:
                return found[:limit]

    elif isinstance(value, list):

        for child in value[:5]:

            found.extend(
                collect_interesting_keys(
                    child,
                    (
                        prefix + "[]"
                        if prefix
                        else "[]"
                    ),
                    depth + 1,
                    max(
                        0,
                        limit - len(found),
                    ),
                )
            )

            if len(found) >= limit:
                return found[:limit]

    return found[:limit]


def find_stream_objects(
    value,
    depth=0,
):
    """Recursively find dicts whose keys strongly resemble stream data."""

    if depth > 6:
        return []

    matches = []

    if isinstance(value, dict):

        keys = {
            str(k).lower()
            for k in value.keys()
        }

        score = 0

        for key in (
            "stream",
            "streamid",
            "stream_id",
            "broadcast",
            "broadcastid",
            "broadcast_id",
            "live",
            "is_live",
            "islive",
            "playback",
            "playlist",
            "hls",
            "m3u8",
        ):

            if key in keys:
                score += 1

        if score >= 1:
            matches.append(value)

        for child in value.values():

            matches.extend(
                find_stream_objects(
                    child,
                    depth + 1,
                )
            )

    elif isinstance(value, list):

        for child in value[:20]:

            matches.extend(
                find_stream_objects(
                    child,
                    depth + 1,
                )
            )

    return matches


def object_has_live_signal(obj):

    if not isinstance(obj, dict):
        return False

    true_values = {
        "true",
        "yes",
        "live",
        "online",
        "broadcasting",
        "active",
    }

    for key in (
        "isLive",
        "is_live",
        "live",
        "online",
        "broadcasting",
        "active",
    ):

        if key not in obj:
            continue

        value = obj.get(key)

        if (
            isinstance(value, bool)
            and value
        ):
            return True

        if (
            isinstance(value, str)
            and value.strip().lower()
            in true_values
        ):
            return True

    for key in (
        "stream",
        "streamId",
        "stream_id",
        "broadcast",
        "broadcastId",
        "broadcast_id",
        "playback",
        "playlist",
        "hls",
        "m3u8",
    ):

        if (
            key in obj
            and obj.get(key)
            not in (
                None,
                "",
                [],
                {},
            )
        ):
            return True

    return False


# ============================================================
# Playwright Monitor
# ============================================================

async def check_user_live_status(
    browser,
    username,
):

    profile_url = (
        f"https://www.tango.me/{username}"
    )

    start_time = time.time()

    log(
        f"[MONITOR] [{username}] Checking at "
        f"{profile_url}"
    )

    context = await browser.new_context(
        viewport={
            "width": 1280,
            "height": 720,
        },
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
    )

    page = await context.new_page()

    info = {
        "m3u8_seen": False,
        "api_stream_seen": False,
        "api_premium": False,
        "api_normal": False,
        "page_status": None,
        "page_title": "",
        "response_count": 0,
        "interesting_responses": 0,
        "diagnostic_logs": 0,
        "api_candidates": 0,
        "json_candidates": 0,
        "errors": [],
        "hls_url": None,
        "account_id": None,
    }

    seen_paths = set()

    def diag_allowed():
        return (
            info["diagnostic_logs"]
            < MAX_DIAGNOSTIC_NETWORK_LOGS
        )

    async def inspect_json_response(
        response,
        safe_url,
    ):
        """Inspect JSON structure without logging response values."""

        try:

            content_type = (
                response.headers.get(
                    "content-type"
                )
                or ""
            ).lower()

            if "json" not in content_type:
                return

            info["json_candidates"] += 1

            data = await response.json()

            keys = collect_interesting_keys(
                data
            )

            stream_objects = find_stream_objects(
                data
            )

            live_signal = any(
                object_has_live_signal(obj)
                for obj in stream_objects
            )

            if stream_objects:
                info["api_candidates"] += 1

            if (
                diag_allowed()
                and (
                    live_signal
                    or any(
                        token in safe_url.lower()
                        for token in (
                            "live",
                            "stream",
                            "broadcast",
                            "room",
                            "watch",
                            "graphql",
                        )
                    )
                )
            ):

                info["diagnostic_logs"] += 1

                log(
                    f"[DIAG] [{username}] JSON "
                    f"HTTP={response.status} "
                    f"CT={content_type.split(';', 1)[0]} "
                    f"URL={safe_url}"
                )

                log(
                    f"[DIAG] [{username}] JSON "
                    f"keys={compact_value(keys, 500)} "
                    f"stream_objects="
                    f"{len(stream_objects)} "
                    f"live_signal={live_signal}"
                )

            for obj in stream_objects:

                details = (
                    obj.get("details")
                    if isinstance(obj, dict)
                    else None
                )

                stream = (
                    obj.get("stream")
                    if isinstance(obj, dict)
                    else None
                )

                if (
                    isinstance(details, dict)
                    and isinstance(
                        details.get("stream"),
                        dict,
                    )
                ):
                    stream = details.get(
                        "stream"
                    )

                if isinstance(stream, dict):

                    info["api_stream_seen"] = True

                    stream_id = (
                        stream.get("id")
                        or stream.get("streamId")
                        or stream.get("stream_id")
                    )

                    info["account_id"] = (
                        stream.get(
                            "encryptedAccountId"
                        )
                        or stream.get(
                            "encrypted_account_id"
                        )
                        or info.get(
                            "account_id"
                        )
                    )

                    master_url = (
                        stream.get(
                            "masterListUrl"
                        )
                        or stream.get(
                            "master_list_url"
                        )
                    )

                    if (
                        isinstance(
                            master_url,
                            str,
                        )
                        and ".m3u8"
                        in master_url.lower()
                    ):
                        info["hls_url"] = (
                            master_url
                        )

                    if (
                        stream_id
                        and diag_allowed()
                    ):

                        info[
                            "diagnostic_logs"
                        ] += 1

                        log(
                            f"[DIAG] [{username}] "
                            "stream object detected; "
                            "id_present=yes"
                        )

                    if is_premium_stream(
                        stream,
                        (
                            details
                            if isinstance(
                                details,
                                dict,
                            )
                            else obj
                        ),
                    ):

                        info["api_premium"] = True
                        info["api_normal"] = False

                        if diag_allowed():

                            info[
                                "diagnostic_logs"
                            ] += 1

                            log(
                                f"[DIAG] [{username}] "
                                "EXPLICIT PREMIUM "
                                "signal found in JSON"
                            )

                        return

                    if (
                        object_has_live_signal(
                            stream
                        )
                        or object_has_live_signal(
                            obj
                        )
                    ):
                        info["api_normal"] = True

                elif object_has_live_signal(obj):

                    info["api_normal"] = True

        except Exception as exc:

            if diag_allowed():

                info[
                    "diagnostic_logs"
                ] += 1

                log(
                    f"[DIAG] [{username}] "
                    f"JSON inspection failed: "
                    f"{type(exc).__name__}: {exc}"
                )

    async def handle_response(response):

        try:

            info["response_count"] += 1

            url = response.url

            safe_url = redact_network_url(
                url
            )

            lower_url = safe_url.lower()

            content_type = (
                response.headers.get(
                    "content-type"
                )
                or ""
            ).lower()

            interesting = (
                ".m3u8" in lower_url
                or ".mpd" in lower_url
                or any(
                    token in lower_url
                    for token in DIAGNOSTIC_PATH_KEYWORDS
                )
            )

            if interesting:

                info[
                    "interesting_responses"
                ] += 1

                path_key = (
                    f"{response.status}|"
                    f"{content_type.split(';', 1)[0]}|"
                    f"{safe_url}"
                )

                if (
                    path_key not in seen_paths
                    and diag_allowed()
                ):

                    seen_paths.add(
                        path_key
                    )

                    info[
                        "diagnostic_logs"
                    ] += 1

                    log(
                        f"[DIAG] [{username}] "
                        f"RESPONSE "
                        f"HTTP={response.status} "
                        f"CT={content_type.split(';', 1)[0] or '-'} "
                        f"URL={safe_url}"
                    )

            if ".m3u8" in lower_url:

                info["m3u8_seen"] = True

                info["hls_url"] = (
                    info.get("hls_url")
                    or url
                )

                if diag_allowed():

                    info[
                        "diagnostic_logs"
                    ] += 1

                    log(
                        f"[DIAG] [{username}] "
                        "HLS manifest candidate detected"
                    )

            # Inspect every JSON response whose path looks relevant,
            # plus the known Tango watch endpoint.
            if (
                "json" in content_type
                and interesting
            ):

                await inspect_json_response(
                    response,
                    safe_url,
                )

            # Known endpoint: keep an explicit diagnostic line even if
            # Tango returns an error/non-JSON response.
            if (
                "proxycador/api/public/v1/live/stream/v2/watch"
                in lower_url
            ):

                if diag_allowed():

                    info[
                        "diagnostic_logs"
                    ] += 1

                    log(
                        f"[DIAG] [{username}] "
                        f"KNOWN WATCH ENDPOINT "
                        f"HTTP={response.status} "
                        f"CT={content_type.split(';', 1)[0] or '-'}"
                    )

                if "json" in content_type:

                    await inspect_json_response(
                        response,
                        safe_url,
                    )

        except Exception as exc:

            if diag_allowed():

                info[
                    "diagnostic_logs"
                ] += 1

                log(
                    f"[DIAG] [{username}] "
                    f"Response handler error: "
                    f"{type(exc).__name__}: {exc}"
                )

    page.on(
        "response",
        handle_response,
    )

    try:

        response = await page.goto(
            profile_url,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        if response is not None:
            info["page_status"] = (
                response.status
            )

        try:
            info["page_title"] = (
                await page.title()
            )
        except Exception:
            pass

        log(
            f"[DIAG] [{username}] PAGE "
            f"HTTP={info['page_status']} "
            f"title={info['page_title'][:120]!r}"
        )

        # Give Tango JS a chance to initialize. Also perform a small,
        # harmless scroll to trigger lazy-loaded player components.
        try:

            await page.mouse.move(
                640,
                360,
            )

            await page.mouse.wheel(
                0,
                450,
            )

            await asyncio.sleep(
                1.0
            )

            await page.mouse.wheel(
                0,
                -450,
            )

        except Exception:
            pass

        deadline = (
            time.monotonic()
            + MAX_WAIT_SECONDS
        )

        while time.monotonic() < deadline:

            await asyncio.sleep(
                POLL_INTERVAL
            )

            if info["api_premium"]:
                break

            if info["api_normal"]:

                # Do not stop immediately in diagnostic mode. We want
                # to capture whether another endpoint later says Premium.
                if not DIAGNOSTIC_ONLY:
                    break

    except Exception as exc:

        info["errors"].append(
            f"{type(exc).__name__}: {exc}"
        )

        log(
            f"[DIAG] [{username}] "
            f"PAGE ERROR: "
            f"{type(exc).__name__}: {exc}"
        )

    finally:

        # A final snapshot of the rendered DOM is useful for diagnosing
        # bot/challenge/player failures, but we never log its contents.
        try:

            body_text = await page.locator(
                "body"
            ).inner_text(
                timeout=1000
            )

            normalized = (
                " ".join(
                    body_text.split()
                ).lower()
            )

            markers = []

            for marker in (
                "captcha",
                "verify you are human",
                "access denied",
                "unusual traffic",
                "robot",
                "sign in",
                "log in",
            ):

                if marker in normalized:
                    markers.append(marker)

            if markers:

                log(
                    f"[DIAG] [{username}] "
                    f"PAGE MARKERS={markers}"
                )

        except Exception:
            pass

        try:
            await context.close()
        except Exception:
            pass

    final_url = info.get(
        "hls_url"
    )

    if info["api_premium"]:

        final_status = (
            StreamStatus.LIVE_PREMIUM
        )

        final_url = None

    elif (
        info["api_normal"]
        and final_url
    ):

        final_status = (
            StreamStatus.LIVE_NORMAL
        )

    elif (
        info["m3u8_seen"]
        and final_url
    ):

        final_status = (
            StreamStatus.LIVE_NORMAL
        )

    elif info["errors"]:

        final_status = (
            StreamStatus.ERROR
        )

        final_url = None

    else:

        final_status = (
            StreamStatus.OFFLINE
        )

        final_url = None

    resolved_username = (
        extract_username_from_url(
            page.url
        )
        or extract_username_from_title(
            info.get(
                "page_title",
                "",
            )
        )
        or username
    )

    identity = {
        "username": resolved_username,
        "display_name": parse_display_name(
            info.get(
                "page_title",
                "",
            ),
            resolved_username,
        ),
        "account_id": str(
            info.get(
                "account_id"
            )
            or ""
        ),
    }

    elapsed = (
        time.time()
        - start_time
    )

    log(
        f"[DIAG] [{username}] SUMMARY "
        f"status={final_status} "
        f"page_http={info['page_status']} "
        f"responses={info['response_count']} "
        f"interesting={info['interesting_responses']} "
        f"json={info['json_candidates']} "
        f"stream_objects={info['api_stream_seen']} "
        f"m3u8={info['m3u8_seen']} "
        f"premium={info['api_premium']} "
        f"normal_signal={info['api_normal']} "
        f"elapsed={elapsed:.1f}s"
    )

    return (
        final_status,
        final_url,
        identity,
    )


# ============================================================
# Main Monitor Loop
# ============================================================

async def main():

    log("=" * 60)
    log("Auto-Monitor Started")
    log("=" * 60)

    start_time = time.time()

    # ------------------------------------------------------------
    # Load watchlist
    # ------------------------------------------------------------

    watchlist_data = await github_get_file(
        WATCHLIST_PATH
    )

    if (
        not watchlist_data
        or not isinstance(
            watchlist_data,
            list,
        )
        or not watchlist_data
    ):

        log(
            "[MONITOR] Watchlist is empty or not found"
        )

        return 0

    # Normalize usernames while preserving their original
    # display form.
    watchlist = []

    for username in watchlist_data:

        if not isinstance(
            username,
            str,
        ):
            continue

        username = username.strip()

        if not username:
            continue

        watchlist.append(
            username
        )

    total_watchlist = len(
        watchlist
    )

    identities = await github_get_file(
        IDENTITY_PATH
    )

    if not isinstance(
        identities,
        dict,
    ):
        identities = {}

    if total_watchlist == 0:

        log(
            "[MONITOR] Watchlist contains no valid usernames"
        )

        return 0

    log(
        f"[MONITOR] Found "
        f"{total_watchlist} users in watchlist"
    )

    # ------------------------------------------------------------
    # Read genuinely active recordings
    # ------------------------------------------------------------

    active_recordings = (
        await github_get_active_recordings()
    )

    if active_recordings is None:

        log(
            "[MONITOR] ❌ Active-state read failed. "
            "No recordings will be dispatched in this run."
        )

        await send_message(
            "⚠️ تعذر قراءة حالة التسجيلات الحالية من GitHub، "
            "لذلك لم يتم تشغيل أي تسجيل جديد حفاظاً على حد 5 تسجيلات."
        )

        return 1

    active_usernames = set()

    for recording in active_recordings:

        ru = str(
            recording.get("username")
            or ""
        ).lower()

        if ru:
            active_usernames.add(ru)

        for alias in identity_aliases(
            identities.get(ru)
        ):

            active_usernames.add(
                alias
            )

    active_count = len(
        active_recordings
    )

    log(
        f"[MONITOR] Verified active recordings: "
        f"{active_count}"
    )

    log(
        f"[MONITOR] Active usernames: "
        f"{sorted(active_usernames)}"
    )

    # ------------------------------------------------------------
    # Build list to check
    # ------------------------------------------------------------

    users_to_check = []

    for username in watchlist:

        username_lower = (
            username.lower()
        )

        if username_lower in active_usernames:

            log(
                f"[MONITOR] {username} is already recording, "
                "skipping"
            )

        else:

            users_to_check.append(
                username
            )

    skipped_active_count = (
        total_watchlist
        - len(users_to_check)
    )

    log(
        f"[MONITOR] Will check "
        f"{len(users_to_check)} users "
        f"(skipped "
        f"{skipped_active_count} already recording)"
    )

    # ------------------------------------------------------------
    # Statistics for EVERY checked user
    # ------------------------------------------------------------

    stats = {
        "offline": 0,
        "live_normal": 0,
        "live_premium": 0,
        "unknown": 0,
        "errors": 0,
    }

    # This is separate from classification.
    recordings_triggered = 0
    recordings_failed = 0
    recordings_blocked_by_limit = 0

    # ------------------------------------------------------------
    # Check every user
    # ------------------------------------------------------------

    results = []

    async with async_playwright() as playwright:

        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        semaphore = asyncio.Semaphore(
            MAX_CONCURRENT_USERS
        )

        async def check_with_semaphore(
            username
        ):

            async with semaphore:

                try:

                    (
                        status,
                        url,
                        identity,
                    ) = await check_user_live_status(
                        browser,
                        username,
                    )

                    return (
                        username,
                        status,
                        url,
                        identity,
                    )

                except Exception as exc:

                    log(
                        f"[MONITOR] Error checking "
                        f"{username}: {exc}"
                    )

                    return (
                        username,
                        StreamStatus.ERROR,
                        None,
                        {
                            "username": username,
                            "display_name": username,
                            "account_id": "",
                        },
                    )

        log(
            f"[MONITOR] Starting concurrent check for "
            f"{len(users_to_check)} users with maximum "
            f"{MAX_CONCURRENT_USERS} concurrent checks..."
        )

        tasks = [
            check_with_semaphore(
                username
            )
            for username in users_to_check
        ]

        # IMPORTANT:
        # gather waits for ALL users.
        # No recording limit is applied here.
        results = await asyncio.gather(
            *tasks
        )

        await browser.close()

    # ------------------------------------------------------------
    # CLASSIFICATION PHASE
    #
    # Every checked user is counted BEFORE any recording is
    # triggered. This guarantees:
    #
    # normal + premium + offline == checked users
    # ------------------------------------------------------------

    normal_results = []
    identity_updates = {}

    for (
        username,
        status,
        stream_url,
        identity,
    ) in results:

        identity_updates[
            username
        ] = identity

        if status == StreamStatus.LIVE_NORMAL:

            stats[
                "live_normal"
            ] += 1

            normal_results.append(
                (
                    username,
                    stream_url,
                )
            )

        elif (
            status
            == StreamStatus.LIVE_PREMIUM
        ):

            stats[
                "live_premium"
            ] += 1

        elif (
            status
            == StreamStatus.UNKNOWN
        ):

            stats[
                "unknown"
            ] += 1

        elif (
            status
            == StreamStatus.ERROR
        ):

            stats[
                "errors"
            ] += 1

        else:

            stats[
                "offline"
            ] += 1

    # Safety invariant.
    classified_count = (
        stats["live_normal"]
        + stats["live_premium"]
        + stats["offline"]
        + stats["unknown"]
        + stats["errors"]
    )

    if (
        classified_count
        != len(users_to_check)
    ):

        log(
            "[MONITOR] ⚠️ Classification count mismatch: "
            f"classified={classified_count}, "
            f"checked={len(users_to_check)}"
        )

    log("=" * 60)

    log(
        "Classification completed:"
    )

    log(
        f"  - Normal Live: "
        f"{stats['live_normal']}"
    )

    log(
        f"  - Premium Live: "
        f"{stats['live_premium']}"
    )

    log(
        f"  - Offline: "
        f"{stats['offline']}"
    )

    log(
        f"  - Checked: "
        f"{len(users_to_check)}"
    )

    log("=" * 60)

    # Persist identities and migrate usernames when Tango exposes
    # a new canonical URL.
    now_iso = datetime.now(
        timezone.utc
    ).isoformat()

    migrated_watchlist = list(
        watchlist
    )

    renamed = False

    for (
        old_username,
        observed,
    ) in identity_updates.items():

        merged = merge_identity(
            identities,
            old_username,
            observed,
            now_iso,
        )

        identities[
            old_username.lower()
        ] = merged

        new_username = str(
            merged.get("username")
            or old_username
        ).strip()

        identities[
            new_username.lower()
        ] = merged

        for i, current in enumerate(
            migrated_watchlist
        ):

            if (
                current.lower()
                == old_username.lower()
                and new_username.lower()
                != current.lower()
            ):

                migrated_watchlist[
                    i
                ] = new_username

                renamed = True

    if identity_updates:

        await github_update_file(
            IDENTITY_PATH,
            identities,
            "Update Tango identity map",
        )

    if renamed:

        seen = set()
        deduped = []

        for value in migrated_watchlist:

            if value.lower() not in seen:

                seen.add(
                    value.lower()
                )

                deduped.append(
                    value
                )

        await github_update_file(
            WATCHLIST_PATH,
            deduped,
            "Update watchlist after Tango username change",
        )

        watchlist = deduped

        log(
            "[IDENTITY] ✅ watchlist migrated "
            "to current Tango usernames"
        )

    # ------------------------------------------------------------
    # RECORDING CAPACITY PHASE
    # ------------------------------------------------------------

    if DIAGNOSTIC_ONLY:

        log(
            "[MONITOR] 🔎 DIAGNOSTIC_ONLY=True: "
            "no repository_dispatch will be sent."
        )

        available_slots = max(
            0,
            MAX_TOTAL_RECORDINGS
            - active_count,
        )

        recordings_triggered = 0
        recordings_failed = 0
        recordings_blocked_by_limit = 0

        log("=" * 60)

        log(
            "Diagnostic classification completed:"
        )

        log(
            f"  - Normal Live: "
            f"{stats['live_normal']}"
        )

        log(
            f"  - Premium Live: "
            f"{stats['live_premium']}"
        )

        log(
            f"  - Offline: "
            f"{stats['offline']}"
        )

        log(
            f"  - Unknown: "
            f"{stats['unknown']}"
        )

        log(
            f"  - Errors: "
            f"{stats['errors']}"
        )

        log(
            f"  - Checked: "
            f"{len(users_to_check)}"
        )

        log("=" * 60)

        elapsed_total = (
            time.time()
            - start_time
        )

        summary = (
            f"🔎 Diagnostic Auto-Monitor "
            f"انتهى في {elapsed_total:.1f} ثانية.\n\n"
            f"📊 القائمة: {total_watchlist}\n"
            f"• قيد التسجيل: {skipped_active_count}\n"
            f"• تم فحصه: {len(users_to_check)}\n\n"
            f"📈 النتائج التشخيصية:\n"
            f"• 🟢 Normal: {stats['live_normal']}\n"
            f"• 🟡 Premium: {stats['live_premium']}\n"
            f"• ⚪ Offline: {stats['offline']}\n"
            f"• ❓ Unknown: {stats['unknown']}\n"
            f"• ❌ Errors: {stats['errors']}\n\n"
            "🛑 لم يتم تشغيل أي تسجيل في الوضع التشخيصي.\n"
            "راجع سجلات [DIAG] لمعرفة API/HLS الفعلي "
            "الذي يراه Chromium."
        )

        await send_message(
            summary
        )

        return 0

    available_slots = max(
        0,
        MAX_TOTAL_RECORDINGS
        - active_count,
    )

    log(
        f"[MONITOR] Recording capacity: "
        f"{active_count}/{MAX_TOTAL_RECORDINGS} active, "
        f"{available_slots} slot(s) available"
    )

    # IMPORTANT:
    # We DO NOT break classification if there are more normal
    # streams than available recording slots.
    #
    # All normal streams remain counted in stats.
    #
    # Only the actual triggering phase is limited.
    # ------------------------------------------------------------

    for (
        username,
        stream_url,
    ) in normal_results:

        if (
            recordings_triggered
            >= available_slots
        ):

            recordings_blocked_by_limit += 1

            log(
                f"[MONITOR] ⛔ {username} is NORMAL live, "
                "but no recording slot is available"
            )

            continue

        if not stream_url:

            recordings_failed += 1

            log(
                f"[MONITOR] ⚠️ {username} is NORMAL live "
                "but no stream URL is available"
            )

            continue

        log(
            f"[MONITOR] ✅ {username} is LIVE (NORMAL). "
            "Starting recording..."
        )

        identity = (
            identity_updates.get(
                username
            )
            or identities.get(
                username.lower()
            )
            or {}
        )

        success = await trigger_recording(
            stream_url,
            username,
            display_label(
                identity,
                username,
            ),
            str(
                identity.get(
                    "account_id"
                )
                or ""
            ),
        )

        if success:

            recordings_triggered += 1

            # Reserve the slot locally immediately after a
            # successful repository_dispatch.
            active_usernames.add(
                username.lower()
            )

            log(
                f"[MONITOR] 🎬 Recording dispatch accepted "
                f"for {username} "
                f"({recordings_triggered}/"
                f"{available_slots} new slots used)"
            )

        else:

            recordings_failed += 1

            log(
                f"[MONITOR] ❌ Failed to start recording "
                f"for {username}"
            )

    # ------------------------------------------------------------
    # Final statistics
    # ------------------------------------------------------------

    elapsed_total = (
        time.time()
        - start_time
    )

    log("=" * 60)

    log(
        f"Auto-Monitor Completed in "
        f"{elapsed_total:.1f}s:"
    )

    log(
        f"  - Watchlist: "
        f"{total_watchlist}"
    )

    log(
        f"  - Already recording: "
        f"{skipped_active_count}"
    )

    log(
        f"  - Checked now: "
        f"{len(users_to_check)}"
    )

    log(
        f"  - Normal Live: "
        f"{stats['live_normal']}"
    )

    log(
        f"  - Premium Live: "
        f"{stats['live_premium']}"
    )

    log(
        f"  - Offline: "
        f"{stats['offline']}"
    )

    log(
        f"  - Unknown: "
        f"{stats['unknown']}"
    )

    log(
        f"  - Errors: "
        f"{stats['errors']}"
    )

    log(
        f"  - Existing active recordings: "
        f"{active_count}"
    )

    log(
        f"  - Recording slots available: "
        f"{available_slots}"
    )

    log(
        f"  - Recording dispatches accepted: "
        f"{recordings_triggered}"
    )

    log(
        f"  - Recording dispatches failed: "
        f"{recordings_failed}"
    )

    log(
        f"  - Normal streams blocked by limit: "
        f"{recordings_blocked_by_limit}"
    )

    log("=" * 60)

    # ------------------------------------------------------------
    # Telegram summary
    # ------------------------------------------------------------

    summary = (
        f"✅ انتهى الفحص في "
        f"{elapsed_total:.1f} ثانية.\n\n"
    )

    summary += (
        f"📊 إحصائيات القائمة "
        f"({total_watchlist} مستخدم):\n"
    )

    summary += (
        f"• قيد التسجيل مسبقاً: "
        f"{skipped_active_count}\n"
    )

    summary += (
        f"• تم فحصه الآن: "
        f"{len(users_to_check)}\n\n"
    )

    summary += (
        "📈 نتائج الفحص:\n"
    )

    summary += (
        f"• 🟢 بث عادي: "
        f"{stats['live_normal']}\n"
    )

    summary += (
        f"• 🟡 بث مدفوع (تم التجاهل): "
        f"{stats['live_premium']}\n"
    )

    summary += (
        f"• ⚪ غير متصل: "
        f"{stats['offline']}\n"
    )

    summary += (
        f"• ❓ غير محسوم: "
        f"{stats['unknown']}\n"
    )

    summary += (
        f"• ❌ أخطاء: "
        f"{stats['errors']}\n\n"
    )

    # ------------------------------------------------------------
    # Recording result
    # ------------------------------------------------------------

    if recordings_triggered > 0:

        summary += (
            f"🔴 تم تشغيل "
            f"{recordings_triggered} تسجيل(ات) جديد(ة)."
        )

    else:

        if stats["live_normal"] == 0:

            summary += (
                "⚪ لم يتم بدء أي تسجيل جديد "
                "(لا توجد بثوث عادية متاحة للتسجيل)."
            )

        elif available_slots == 0:

            summary += (
                "⛔ لم يتم بدء تسجيل جديد "
                "(تم الوصول إلى الحد الأقصى "
                f"{MAX_TOTAL_RECORDINGS} "
                "تسجيلات متزامنة)."
            )

        elif recordings_failed > 0:

            summary += (
                "⚠️ تم اكتشاف بث عادي، "
                "لكن فشل تشغيل التسجيل."
            )

        else:

            summary += (
                "⚪ لم يتم بدء أي تسجيل جديد."
            )

    # Additional diagnostic information only when relevant.
    if recordings_blocked_by_limit > 0:

        summary += (
            "\n\n⛔ بثوث عادية لم يبدأ تسجيلها بسبب "
            f"حد التسجيلات: "
            f"{recordings_blocked_by_limit}"
        )

    if recordings_failed > 0:

        summary += (
            "\n⚠️ فشل تشغيل تسجيلات: "
            f"{recordings_failed}"
        )

    await send_message(
        summary
    )

    return 0


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":

    try:

        exit_code = asyncio.run(
            main()
        )

        sys.exit(
            exit_code
        )

    except KeyboardInterrupt:

        log(
            "[MONITOR] Keyboard interrupt."
        )

        sys.exit(0)

    except Exception as exc:

        log(
            f"[FATAL] {type(exc).__name__}: {exc}"
        )

        try:

            asyncio.run(
                send_message(
                    "❌ حدث خطأ في Auto-Monitor: "
                    f"{type(exc).__name__}"
                )
            )

        except Exception:
            pass

        sys.exit(1)
