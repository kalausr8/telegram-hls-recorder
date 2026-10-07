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

# Maximum number of users checked concurrently.
# This is NOT the recording limit.
MAX_CONCURRENT_USERS = 5

# Maximum total active recordings allowed.
MAX_TOTAL_RECORDINGS = 5

# Maximum time spent waiting for stream/API evidence per user.
MAX_WAIT_SECONDS = 8.0

# Poll interval while waiting for network evidence.
POLL_INTERVAL = 0.5

# Safety settings.
MAX_RECORDING_AGE_HOURS = 6
MAX_STATUS_STALE_MINUTES = 10


# ============================================================
# Stream Classification
# ============================================================

class StreamStatus:
    OFFLINE = "OFFLINE"
    LIVE_NORMAL = "LIVE_NORMAL"
    LIVE_PREMIUM = "LIVE_PREMIUM"


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
        log(f"[GITHUB] Exception deleting {path}: {exc}")


async def is_recording_actually_alive(record_id, started_at_str):
    if started_at_str:
        try:
            start_time = datetime.fromisoformat(
                started_at_str.replace("Z", "+00:00")
            )

            age = datetime.now(timezone.utc) - start_time
            age_hours = age.total_seconds() / 3600

            if age_hours > MAX_RECORDING_AGE_HOURS:
                log(
                    f"[VERIFY] ❌ {record_id}: "
                    f"File too old ({age_hours:.1f}h > "
                    f"{MAX_RECORDING_AGE_HOURS}h)"
                )
                return False

        except Exception as exc:
            log(
                f"[VERIFY] ⚠️ {record_id}: "
                f"Error parsing started_at: {exc}"
            )

    status_path = f".recorder/status/{record_id}.json"
    status_data = await github_get_file(status_path)

    if status_data is None:
        if started_at_str:
            try:
                start_time = datetime.fromisoformat(
                    started_at_str.replace("Z", "+00:00")
                )

                age_minutes = (
                    datetime.now(timezone.utc) - start_time
                ).total_seconds() / 60

                if age_minutes < 2:
                    log(
                        f"[VERIFY] ✅ {record_id}: "
                        f"Just started ({age_minutes:.1f}m ago), "
                        "no status yet - OK"
                    )
                    return True

            except Exception:
                pass

        log(
            f"[VERIFY] ❌ {record_id}: "
            "No status file found - recording likely stopped"
        )
        return False

    last_commit_time = await github_get_file_last_commit_time(
        status_path
    )

    if last_commit_time is None:
        log(
            f"[VERIFY] ⚠️ {record_id}: "
            "Cannot check status freshness - keeping as active"
        )
        return True

    stale_minutes = (
        datetime.now(timezone.utc) - last_commit_time
    ).total_seconds() / 60

    if stale_minutes > MAX_STATUS_STALE_MINUTES:
        log(
            f"[VERIFY] ❌ {record_id}: "
            f"Status stale ({stale_minutes:.1f}m > "
            f"{MAX_STATUS_STALE_MINUTES}m) - recording stopped"
        )
        return False

    log(
        f"[VERIFY] ✅ {record_id}: "
        f"Status fresh ({stale_minutes:.1f}m ago) - recording alive"
    )

    return True


async def github_get_active_recordings():
    """
    Return the genuinely active recordings.

    Result:
        [
            {
                "username": "...",
                "record_id": "...",
                "path": "..."
            },
            ...
        ]
    """

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/{GITHUB_REPO}/git/trees/main"
        f"?recursive=1"
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
                    log(
                        "[GITHUB] Failed to read repository tree: "
                        f"HTTP {resp.status}"
                    )
                    return []

                data = await resp.json()

                tree = data.get("tree", [])

                active_files = [
                    file_info
                    for file_info in tree
                    if file_info["path"].startswith(
                        ".recorder/active/"
                    )
                    and file_info["path"].endswith(".json")
                ]

                if not active_files:
                    log("[MONITOR] No active recordings found")
                    return []

                log(
                    f"[MONITOR] Found {len(active_files)} active file(s), "
                    "verifying..."
                )

                active_recordings = []

                for file_info in active_files:

                    path = file_info["path"]

                    file_data = await github_get_file(path)

                    if not file_data or "username" not in file_data:
                        continue

                    record_id = file_data.get(
                        "record_id",
                        "UNKNOWN",
                    )

                    started_at = file_data.get(
                        "started_at",
                        "",
                    )

                    username = file_data["username"]

                    is_alive = await is_recording_actually_alive(
                        record_id,
                        started_at,
                    )

                    if is_alive:
                        active_recordings.append(
                            {
                                "username": username.lower(),
                                "record_id": record_id,
                                "path": path,
                            }
                        )

                        log(
                            f"[MONITOR] ✅ {username} is genuinely "
                            f"recording (#{record_id})"
                        )

                    else:
                        log(
                            f"[MONITOR] 🗑️ {username} recording is dead, "
                            f"cleaning up (#{record_id})..."
                        )

                        await github_delete_file(
                            path,
                            f"Cleanup dead recording {record_id}",
                        )

                        await github_delete_file(
                            f".recorder/status/{record_id}.json",
                            f"Cleanup dead status {record_id}",
                        )

                return active_recordings

    except Exception as exc:
        log(
            f"[GITHUB] Exception getting active recordings: {exc}"
        )
        return []


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


async def trigger_recording(stream_url, username):
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

    body = {
        "event_type": "telegram_record",
        "client_payload": {
            "url": stream_url,
            "record_id": record_id,
            "username": username,
        },
    }

    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
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
                        f"{username} تلقائياً! (#{record_id})"
                    )

                    return True

                text = await resp.text()

                log(
                    f"[GITHUB] Trigger failed: "
                    f"HTTP {resp.status} - {text[:500]}"
                )

                return False

    except Exception as exc:
        log(
            f"[GITHUB] Exception triggering recording: {exc}"
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


def is_premium_stream(stream_data, details_data):
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
# Playwright Monitor
# ============================================================

async def check_user_live_status(browser, username):
    profile_url = f"https://www.tango.me/{username}"
    start_time = time.time()

    log(
        f"[MONITOR] [{username}] Checking at {profile_url}"
    )

    context = await browser.new_context(
        viewport={
            "width": 1280,
            "height": 720,
        },
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
    )

    page = await context.new_page()

    stream_info = {
        "status": StreamStatus.OFFLINE,
        "stream_url": None,
        "stream_id": None,

        # m3u8 is evidence of a live stream, but it is not
        # allowed to permanently override a later Premium API result.
        "m3u8_seen": False,

        # Whether Tango's watch API returned a stream object.
        "api_stream_seen": False,

        # Whether the API explicitly classified the stream as Premium.
        "api_premium": False,

        # Whether the API explicitly classified the stream as normal.
        "api_normal": False,
    }

    async def handle_response(response):
        try:
            url = response.url

            # ----------------------------------------------------
            # Tango watch API
            # ----------------------------------------------------

            if (
                "proxycador/api/public/v1/live/stream/v2/watch"
                in url
            ):
                if response.status == 200:

                    try:
                        data = await response.json()

                        if not isinstance(data, dict):
                            return

                        body = data.get("body")

                        if not isinstance(body, dict):
                            return

                        details = body.get("details")

                        if not isinstance(details, dict):
                            return

                        stream = details.get("stream")

                        if not isinstance(stream, dict):
                            return

                        stream_info["api_stream_seen"] = True
                        stream_info["stream_id"] = stream.get("id")

                        # Premium has priority over every other
                        # stream signal.
                        if is_premium_stream(
                            stream,
                            details,
                        ):
                            stream_info["api_premium"] = True
                            stream_info["api_normal"] = False
                            stream_info["status"] = (
                                StreamStatus.LIVE_PREMIUM
                            )

                            log(
                                f"[MONITOR] [{username}] "
                                "🟡 PREMIUM detected via API"
                            )

                        else:
                            stream_info["api_normal"] = True

                            # Only classify as normal here if
                            # Premium was not explicitly detected.
                            if not stream_info["api_premium"]:
                                stream_info["status"] = (
                                    StreamStatus.LIVE_NORMAL
                                )
                                stream_info["stream_url"] = (
                                    profile_url
                                )

                                log(
                                    f"[MONITOR] [{username}] "
                                    "✅ NORMAL live detected via API"
                                )

                    except Exception as exc:
                        log(
                            f"[MONITOR] [{username}] "
                            f"Watch API parse error: {exc}"
                        )

            # ----------------------------------------------------
            # HLS m3u8
            # ----------------------------------------------------

            if ".m3u8" in url.lower():

                stream_info["m3u8_seen"] = True

                log(
                    f"[MONITOR] [{username}] "
                    "m3u8 detected"
                )

                # m3u8 confirms that a stream resource exists,
                # but if Premium has already been confirmed,
                # NEVER downgrade it to Normal.
                if not stream_info["api_premium"]:
                    stream_info["stream_url"] = profile_url

                    # This is provisional evidence.
                    # Final classification is resolved after
                    # the observation window.
                    if stream_info["status"] == StreamStatus.OFFLINE:
                        stream_info["status"] = (
                            StreamStatus.LIVE_NORMAL
                        )

        except Exception as exc:
            log(
                f"[MONITOR] [{username}] "
                f"Response handler error: {exc}"
            )

    page.on("response", handle_response)

    try:
        await page.goto(
            profile_url,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        # Encourage the player to initialize.
        try:
            await page.mouse.move(100, 100)
            await asyncio.sleep(0.5)
        except Exception:
            pass

        max_iterations = int(
            MAX_WAIT_SECONDS / POLL_INTERVAL
        )

        for _ in range(max_iterations):

            await asyncio.sleep(POLL_INTERVAL)

            # Premium is terminal and has priority.
            if stream_info["api_premium"]:
                break

            # An explicit normal API result is strong enough to
            # finish early.
            if stream_info["api_normal"]:
                break

            # m3u8 alone remains provisional. We intentionally
            # continue observing until the normal timeout so that
            # a Premium API response has an opportunity to arrive.
            #
            # Therefore there is intentionally NO early exit here
            # merely because m3u8 was seen.

    except Exception as exc:
        log(
            f"[MONITOR] [{username}] Error: {exc}"
        )

    finally:
        await context.close()

    # ------------------------------------------------------------
    # Final classification
    # ------------------------------------------------------------

    if stream_info["api_premium"]:
        final_status = StreamStatus.LIVE_PREMIUM
        final_url = None

    elif stream_info["api_normal"]:
        final_status = StreamStatus.LIVE_NORMAL
        final_url = profile_url

    elif stream_info["m3u8_seen"]:
        # No explicit Premium API evidence was received during
        # the observation window. m3u8 is therefore accepted as
        # live-stream evidence.
        final_status = StreamStatus.LIVE_NORMAL
        final_url = profile_url

    else:
        final_status = StreamStatus.OFFLINE
        final_url = None

    elapsed = time.time() - start_time

    log(
        f"[MONITOR] [{username}] Completed in "
        f"{elapsed:.1f}s: status={final_status}"
    )

    return final_status, final_url


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
        or not isinstance(watchlist_data, list)
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
        if not isinstance(username, str):
            continue

        username = username.strip()

        if not username:
            continue

        watchlist.append(username)

    total_watchlist = len(watchlist)

    if total_watchlist == 0:
        log("[MONITOR] Watchlist contains no valid usernames")
        return 0

    log(
        f"[MONITOR] Found {total_watchlist} users in watchlist"
    )

    # ------------------------------------------------------------
    # Read genuinely active recordings
    # ------------------------------------------------------------

    active_recordings = await github_get_active_recordings()

    active_usernames = {
        recording["username"].lower()
        for recording in active_recordings
    }

    active_count = len(active_recordings)

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

        username_lower = username.lower()

        if username_lower in active_usernames:
            log(
                f"[MONITOR] {username} is already recording, "
                "skipping"
            )
        else:
            users_to_check.append(username)

    skipped_active_count = (
        total_watchlist - len(users_to_check)
    )

    log(
        f"[MONITOR] Will check {len(users_to_check)} users "
        f"(skipped {skipped_active_count} already recording)"
    )

    # ------------------------------------------------------------
    # Statistics for EVERY checked user
    # ------------------------------------------------------------

    stats = {
        "offline": 0,
        "live_normal": 0,
        "live_premium": 0,
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

        async def check_with_semaphore(username):
            async with semaphore:
                try:
                    status, url = await check_user_live_status(
                        browser,
                        username,
                    )

                    return username, status, url

                except Exception as exc:
                    log(
                        f"[MONITOR] Error checking "
                        f"{username}: {exc}"
                    )

                    # An unexpected check failure is kept as
                    # OFFLINE for backward compatibility.
                    # It is still counted, so the summary remains
                    # internally consistent.
                    return (
                        username,
                        StreamStatus.OFFLINE,
                        None,
                    )

        log(
            f"[MONITOR] Starting concurrent check for "
            f"{len(users_to_check)} users with maximum "
            f"{MAX_CONCURRENT_USERS} concurrent checks..."
        )

        tasks = [
            check_with_semaphore(username)
            for username in users_to_check
        ]

        # IMPORTANT:
        # gather waits for ALL users.
        # No recording limit is applied here.
        results = await asyncio.gather(*tasks)

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

    for username, status, stream_url in results:

        if status == StreamStatus.LIVE_NORMAL:
            stats["live_normal"] += 1

            normal_results.append(
                (
                    username,
                    stream_url,
                )
            )

        elif status == StreamStatus.LIVE_PREMIUM:
            stats["live_premium"] += 1

        else:
            stats["offline"] += 1

    # Safety invariant.
    classified_count = (
        stats["live_normal"]
        + stats["live_premium"]
        + stats["offline"]
    )

    if classified_count != len(users_to_check):
        log(
            "[MONITOR] ⚠️ Classification count mismatch: "
            f"classified={classified_count}, "
            f"checked={len(users_to_check)}"
        )

    log("=" * 60)
    log("Classification completed:")
    log(
        f"  - Normal Live: {stats['live_normal']}"
    )
    log(
        f"  - Premium Live: {stats['live_premium']}"
    )
    log(
        f"  - Offline: {stats['offline']}"
    )
    log(
        f"  - Checked: {len(users_to_check)}"
    )
    log("=" * 60)

    # ------------------------------------------------------------
    # RECORDING CAPACITY PHASE
    # ------------------------------------------------------------

    available_slots = max(
        0,
        MAX_TOTAL_RECORDINGS - active_count,
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

    for username, stream_url in normal_results:

        if recordings_triggered >= available_slots:

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

        success = await trigger_recording(
            stream_url,
            username,
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

    elapsed_total = time.time() - start_time

    log("=" * 60)
    log(
        f"Auto-Monitor Completed in "
        f"{elapsed_total:.1f}s:"
    )

    log(
        f"  - Watchlist: {total_watchlist}"
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

    summary += "📈 نتائج الفحص:\n"

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
        f"{stats['offline']}\n\n"
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

    await send_message(summary)

    return 0


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":

    try:

        exit_code = asyncio.run(main())
        sys.exit(exit_code)

    except KeyboardInterrupt:

        log("[MONITOR] Keyboard interrupt.")
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
