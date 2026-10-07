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
MAX_STARTING_AGE_MINUTES = 15
MAX_HEARTBEAT_STALE_MINUTES = 10


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
    """Read a JSON file from GitHub.

    None means the file genuinely does not exist (404).
    Any other read failure raises GitHubReadError so callers cannot
    accidentally interpret an API outage as an empty state.
    """
    if not GITHUB_TOKEN:
        raise GitHubReadError("GITHUB_TOKEN is missing")

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
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
                    text = await resp.text()
                    raise GitHubReadError(f"GET {path}: HTTP {resp.status}: {text[:300]}")
                data = await resp.json()
                encoded = data.get("content")
                if not encoded:
                    raise GitHubReadError(f"GET {path}: response has no content")
                try:
                    raw = base64.b64decode(encoded.replace("\n", "")).decode("utf-8", errors="replace")
                    return json.loads(raw)
                except Exception as exc:
                    raise GitHubReadError(f"GET {path}: invalid JSON/content: {exc}") from exc
    except GitHubReadError:
        raise
    except Exception as exc:
        raise GitHubReadError(f"GET {path}: {exc}") from exc


class GitHubReadError(RuntimeError):
    pass


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


async def is_recording_actually_alive(record_data):
    """Conservatively validate an active lease.

    A fresh heartbeat keeps a recording active. Missing/stale status alone
    never proves that the recorder is dead; uncertain state is kept active
    to protect the five-recording limit.
    """
    record_id = str(record_data.get("record_id", "UNKNOWN"))
    state = str(record_data.get("state", "recording")).lower()
    started_at_str = record_data.get("started_at") or record_data.get("claimed_at") or ""
    heartbeat_str = record_data.get("heartbeat_at") or record_data.get("claimed_at") or started_at_str

    now = datetime.now(timezone.utc)

    def parse(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    start_time = parse(started_at_str)
    heartbeat = parse(heartbeat_str)

    if start_time:
        age_hours = (now - start_time).total_seconds() / 3600
        if age_hours > MAX_RECORDING_AGE_HOURS:
            log(f"[VERIFY] ❌ {record_id}: lease older than {MAX_RECORDING_AGE_HOURS}h")
            return False

    if state == "starting":
        if heartbeat:
            age_minutes = (now - heartbeat).total_seconds() / 60
            if age_minutes <= MAX_STARTING_AGE_MINUTES:
                log(f"[VERIFY] ✅ {record_id}: recorder is starting ({age_minutes:.1f}m old)")
                return True
        log(f"[VERIFY] 🟠 {record_id}: starting lease is stale; treating as abandoned")
        return False

    if heartbeat:
        heartbeat_age = (now - heartbeat).total_seconds() / 60
        if heartbeat_age <= MAX_HEARTBEAT_STALE_MINUTES:
            log(f"[VERIFY] ✅ {record_id}: active heartbeat fresh ({heartbeat_age:.1f}m ago)")
            return True

    status_path = f".recorder/status/{record_id}.json"
    try:
        status_data = await github_get_file(status_path)
    except GitHubReadError as exc:
        log(f"[VERIFY] ⚠️ {record_id}: cannot read status ({exc}); keeping active")
        return True

    if status_data is None:
        if start_time and (now - start_time).total_seconds() < 120:
            log(f"[VERIFY] ✅ {record_id}: just started; no status yet")
            return True
        log(f"[VERIFY] ⚠️ {record_id}: heartbeat/status unavailable; keeping active conservatively")
        return True

    try:
        last_commit_time = await github_get_file_last_commit_time(status_path)
    except Exception:
        last_commit_time = None

    if last_commit_time is None:
        log(f"[VERIFY] ⚠️ {record_id}: status freshness unavailable; keeping active")
        return True

    stale_minutes = (now - last_commit_time).total_seconds() / 60
    if stale_minutes <= MAX_STATUS_STALE_MINUTES:
        log(f"[VERIFY] ✅ {record_id}: status fresh ({stale_minutes:.1f}m ago)")
        return True

    log(f"[VERIFY] ⚠️ {record_id}: heartbeat/status stale ({stale_minutes:.1f}m); keeping active to avoid duplicate recording")
    return True


async def github_get_active_recordings():
    """Return active recorder leases.

    GitHub read errors are fatal for this monitor run. Returning [] on an API
    failure could violate the hard limit of five recordings.
    """
    if not GITHUB_TOKEN:
        raise GitHubReadError("GITHUB_TOKEN is missing")

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/trees/main?recursive=1"
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
                    text = await resp.text()
                    raise GitHubReadError(f"repository tree HTTP {resp.status}: {text[:300]}")
                data = await resp.json()
                tree = data.get("tree", [])
                active_files = [
                    item for item in tree
                    if item.get("path", "").startswith(".recorder/active/")
                    and item.get("path", "").endswith(".json")
                ]

        if not active_files:
            log("[MONITOR] No active recordings found")
            return []

        log(f"[MONITOR] Found {len(active_files)} active lease(s), verifying...")
        active_recordings = []

        for file_info in active_files:
            path = file_info["path"]
            try:
                file_data = await github_get_file(path)
            except GitHubReadError as exc:
                raise GitHubReadError(f"cannot read active lease {path}: {exc}") from exc

            if not isinstance(file_data, dict) or "username" not in file_data:
                log(f"[VERIFY] ⚠️ Ignoring malformed active lease: {path}")
                continue

            record_id = str(file_data.get("record_id", "UNKNOWN"))
            username = str(file_data.get("username", "")).strip()
            if not username:
                continue

            alive = await is_recording_actually_alive(file_data)
            if alive:
                active_recordings.append({
                    "username": username.lower(),
                    "record_id": record_id,
                    "path": path,
                    "state": file_data.get("state", "recording"),
                })
                log(f"[MONITOR] ✅ {username} occupies a recorder slot (#{record_id})")
            else:
                log(f"[MONITOR] 🗑️ Abandoned starting lease: {username} (#{record_id})")
                await github_delete_file(path, f"Cleanup abandoned recording lease {record_id}")
                await github_delete_file(f".recorder/status/{record_id}.json", f"Cleanup abandoned status {record_id}")

        return active_recordings
    except GitHubReadError:
        raise
    except Exception as exc:
        raise GitHubReadError(f"active recording scan failed: {exc}") from exc


async def github_create_file(path, content_dict, message):
    """Create a new GitHub file without a SHA; existing files fail closed."""
    if not PAT_TOKEN:
        log("[GITHUB] Cannot create lease: PAT_TOKEN missing")
        return False

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {PAT_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "telegram-hls-recorder-monitor",
    }
    encoded = base64.b64encode(json.dumps(content_dict, indent=2).encode("utf-8")).decode("utf-8")
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.put(url, headers=headers, json={"message": message, "content": encoded}) as resp:
                if resp.status in (200, 201):
                    log(f"[GITHUB] ✅ Created lease {path}")
                    return True
                text = await resp.text()
                log(f"[GITHUB] Lease creation failed for {path}: HTTP {resp.status} - {text[:500]}")
                return False
    except Exception as exc:
        log(f"[GITHUB] Exception creating {path}: {exc}")
        return False


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
    """Atomically reserve a recorder slot before dispatching the workflow."""
    if not PAT_TOKEN:
        log("[GITHUB] Cannot trigger recording: PAT_TOKEN missing")
        await send_message("❌ خطأ في الإعدادات: PAT_TOKEN غير موجود.")
        return False

    record_id = generate_record_id()
    lease_path = f".recorder/active/{record_id}.json"
    now = datetime.now(timezone.utc).isoformat()
    lease = {
        "record_id": record_id,
        "url": stream_url,
        "username": username,
        "state": "starting",
        "claimed_at": now,
        "started_at": now,
        "heartbeat_at": now,
    }

    if not await github_create_file(lease_path, lease, f"Reserve recording slot {record_id}"):
        log(f"[GITHUB] ❌ Could not reserve slot for {username}")
        return False

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/dispatches"
    headers = {
        "Authorization": f"Bearer {PAT_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "telegram-hls-recorder-monitor",
    }
    body = {
        "event_type": "telegram_record",
        "client_payload": {"url": stream_url, "record_id": record_id, "username": username},
    }
    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=body) as resp:
                if resp.status == 204:
                    log(f"[GITHUB] Dispatch accepted for {username} (ID: {record_id})")
                    await send_message(
                        f"🔴 Auto-Record: تم قبول تشغيل تسجيل {username} "
                        f"وجرى حجز المقعد. (#{record_id})"
                    )
                    return True
                text = await resp.text()
                log(f"[GITHUB] Trigger failed: HTTP {resp.status} - {text[:500]}")
    except Exception as exc:
        log(f"[GITHUB] Exception triggering recording: {exc}")

    await github_delete_file(lease_path, f"Release failed recording lease {record_id}")
    return False


# ============================================================
# Premium Detection
# ============================================================

PREMIUM_BOOLEAN_FIELDS = {
    "isPremium", "premium", "vip", "exclusive", "locked", "private", "paid"
}
PREMIUM_TEXT_FIELDS = {"type", "payType", "streamType", *PREMIUM_BOOLEAN_FIELDS}
PREMIUM_KEYWORDS = {"premium", "paid", "vip", "exclusive", "locked", "private"}


def value_indicates_premium(value, field=None):
    if value is None:
        return False
    if isinstance(value, bool):
        return value if field in PREMIUM_BOOLEAN_FIELDS else False
    if isinstance(value, (int, float)):
        # Numeric enum values such as type=1/payType=1 are deliberately
        # NOT interpreted as Premium without a documented field meaning.
        return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return False
        if field in PREMIUM_BOOLEAN_FIELDS and normalized in {"true", "yes", "1"}:
            return True
        if normalized in PREMIUM_KEYWORDS:
            return True
        if any(keyword in normalized for keyword in PREMIUM_KEYWORDS):
            return True
    return False


def is_premium_stream(stream_data, details_data):
    for data_name, data in (("stream", stream_data), ("details", details_data)):
        if not isinstance(data, dict):
            continue
        for field in PREMIUM_TEXT_FIELDS:
            if field not in data:
                continue
            if value_indicates_premium(data.get(field), field):
                log(f"[MONITOR] 🟡 Premium detected via {data_name}.{field}")
                return True
    return False


def find_stream_objects(value, depth=0):
    """Find plausible live-stream objects in unknown Tango JSON schemas."""
    if depth > 7:
        return []
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_l = str(key).lower()
            if key_l in {"stream", "livestream", "live_stream", "broadcast", "livebroadcast"} and isinstance(child, dict):
                found.append(child)
            found.extend(find_stream_objects(child, depth + 1))
    elif isinstance(value, list):
        for child in value[:100]:
            found.extend(find_stream_objects(child, depth + 1))
    return found


def object_has_live_signal(obj):
    if not isinstance(obj, dict):
        return False
    for key in ("isLive", "is_live", "live", "online", "isOnline", "is_online"):
        if obj.get(key) is True:
            return True
    for key in ("status", "state", "streamStatus", "liveStatus"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip().lower() in {"live", "online", "broadcasting", "streaming", "active"}:
            return True
    return False


# ============================================================
# Playwright Monitor
# ============================================================

async def check_user_live_status(browser, username):
    profile_url = f"https://www.tango.me/{username}"
    start_time = time.time()
    log(f"[MONITOR] [{username}] Checking at {profile_url}")

    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    )
    page = await context.new_page()
    info = {
        "api_premium": False,
        "api_normal": False,
        "api_stream_seen": False,
        "m3u8_seen": False,
        "error": False,
        "page_loaded": False,
        "candidate_urls": set(),
    }

    async def handle_response(response):
        try:
            url = response.url
            low_url = url.lower()
            if ".m3u8" in low_url:
                info["m3u8_seen"] = True
                log(f"[MONITOR] [{username}] m3u8 detected")

            if response.status != 200:
                return

            # The old endpoint is retained, but we also observe likely live
            # API endpoints so a Tango path/version change does not silently
            # turn a live user into OFFLINE.
            if "proxycador/api/public/v1/live/stream/v2/watch" in low_url or (
                "/api/" in low_url and any(token in low_url for token in ("live", "stream", "watch", "broadcast", "room"))
            ):
                info["candidate_urls"].add(url.split("?", 1)[0])
                try:
                    data = await response.json()
                except Exception:
                    return
                candidates = find_stream_objects(data)
                if not candidates and isinstance(data, dict):
                    candidates = [data] if object_has_live_signal(data) else []

                nested_details = {}
                if isinstance(data, dict):
                    body = data.get("body")
                    if isinstance(body, dict) and isinstance(body.get("details"), dict):
                        nested_details = body["details"]
                for stream in candidates:
                    details = stream.get("details") if isinstance(stream.get("details"), dict) else nested_details
                    if is_premium_stream(stream, details):
                        info["api_premium"] = True
                        info["api_normal"] = False
                        log(f"[MONITOR] [{username}] 🟡 PREMIUM detected via live API")
                        return

                    if object_has_live_signal(stream) or stream.get("id") or stream.get("streamId") or stream.get("stream_id"):
                        info["api_stream_seen"] = True
                        info["api_normal"] = True
                        log(f"[MONITOR] [{username}] ✅ live stream evidence found in API")
        except Exception as exc:
            log(f"[MONITOR] [{username}] Response handler error: {exc}")

    page.on("response", handle_response)
    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
        info["page_loaded"] = True
        try:
            await page.mouse.move(100, 100)
        except Exception:
            pass
        for _ in range(int(MAX_WAIT_SECONDS / POLL_INTERVAL)):
            await asyncio.sleep(POLL_INTERVAL)
            if info["api_premium"]:
                break
            if info["api_normal"] and time.time() - start_time >= 3.0:
                break
    except Exception as exc:
        info["error"] = True
        log(f"[MONITOR] [{username}] Error: {exc}")
    finally:
        await context.close()

    if info["api_premium"]:
        status, url = StreamStatus.LIVE_PREMIUM, None
    elif info["api_normal"]:
        status, url = StreamStatus.LIVE_NORMAL, profile_url
    elif info["error"] and not info["page_loaded"]:
        status, url = StreamStatus.ERROR, None
    elif info["m3u8_seen"]:
        # HLS proves that something live-like exists, but without a normal
        # API signal it is intentionally UNKNOWN rather than recordable.
        status, url = StreamStatus.UNKNOWN, None
    else:
        # No evidence is not proof of offline. Keep it UNKNOWN so discovery
        # failures cannot produce false OFFLINE statistics.
        status, url = StreamStatus.UNKNOWN, None

    elapsed = time.time() - start_time
    log(f"[MONITOR] [{username}] Completed in {elapsed:.1f}s: status={status}")
    return status, url

# ============================================================
# Main Monitor Loop
# ============================================================

async def main():
    log("=" * 60)
    log("Auto-Monitor Started")
    log("=" * 60)
    start_time = time.time()

    try:
        watchlist_data = await github_get_file(WATCHLIST_PATH)
    except GitHubReadError as exc:
        log(f"[MONITOR] ❌ Cannot read watchlist safely: {exc}")
        await send_message("❌ تعذر قراءة قائمة المراقبة من GitHub؛ لم يبدأ أي تسجيل حفاظاً على حد التسجيلات.")
        return 1

    if not isinstance(watchlist_data, list):
        log("[MONITOR] ❌ Watchlist file is missing or invalid")
        await send_message("❌ ملف قائمة المراقبة غير صالح؛ لم يبدأ أي تسجيل.")
        return 1

    watchlist = [u.strip() for u in watchlist_data if isinstance(u, str) and u.strip()]
    total_watchlist = len(watchlist)
    if not total_watchlist:
        log("[MONITOR] Watchlist contains no valid usernames")
        return 0
    log(f"[MONITOR] Found {total_watchlist} users in watchlist")

    try:
        active_recordings = await github_get_active_recordings()
    except GitHubReadError as exc:
        log(f"[MONITOR] ❌ Cannot verify active recordings: {exc}")
        await send_message("❌ تعذر التحقق من التسجيلات الحالية؛ لم يبدأ أي تسجيل جديد حفاظاً على الحد الأقصى 5.")
        return 1

    active_usernames = {r["username"].lower() for r in active_recordings}
    active_count = len(active_recordings)
    log(f"[MONITOR] Verified active recorder leases: {active_count}")
    log(f"[MONITOR] Active usernames: {sorted(active_usernames)}")

    users_to_check = []
    for username in watchlist:
        if username.lower() in active_usernames:
            log(f"[MONITOR] {username} is already active, skipping")
        else:
            users_to_check.append(username)
    skipped_active_count = total_watchlist - len(users_to_check)
    log(f"[MONITOR] Will check {len(users_to_check)} users (skipped {skipped_active_count} active)")

    stats = {"offline": 0, "live_normal": 0, "live_premium": 0, "unknown": 0, "error": 0}
    recordings_triggered = 0
    recordings_failed = 0
    recordings_blocked_by_limit = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_USERS)

        async def check_with_semaphore(username):
            async with semaphore:
                try:
                    status, url = await check_user_live_status(browser, username)
                    return username, status, url
                except Exception as exc:
                    log(f"[MONITOR] Error checking {username}: {exc}")
                    return username, StreamStatus.ERROR, None

        log(f"[MONITOR] Starting concurrent check for {len(users_to_check)} users with maximum {MAX_CONCURRENT_USERS} concurrent checks...")
        results = await asyncio.gather(*(check_with_semaphore(u) for u in users_to_check))
        await browser.close()

    normal_results = []
    for username, status, stream_url in results:
        if status == StreamStatus.LIVE_NORMAL:
            stats["live_normal"] += 1
            normal_results.append((username, stream_url))
        elif status == StreamStatus.LIVE_PREMIUM:
            stats["live_premium"] += 1
        elif status == StreamStatus.UNKNOWN:
            stats["unknown"] += 1
        elif status == StreamStatus.ERROR:
            stats["error"] += 1
        else:
            stats["offline"] += 1

    classified_count = sum(stats.values())
    if classified_count != len(users_to_check):
        log(f"[MONITOR] ⚠️ Classification count mismatch: {classified_count} vs {len(users_to_check)}")
        await send_message("❌ حدث خلل في إحصائيات الفحص؛ لم يبدأ أي تسجيل جديد في هذه الدورة.")
        return 1

    log("=" * 60)
    log(f"  - Normal Live: {stats['live_normal']}")
    log(f"  - Premium Live: {stats['live_premium']}")
    log(f"  - Offline: {stats['offline']}")
    log(f"  - Unknown: {stats['unknown']}")
    log(f"  - Errors: {stats['error']}")
    log(f"  - Checked: {len(users_to_check)}")
    log("=" * 60)

    available_slots = max(0, MAX_TOTAL_RECORDINGS - active_count)
    log(f"[MONITOR] Recording capacity: {active_count}/{MAX_TOTAL_RECORDINGS}, {available_slots} slot(s) available")

    for username, stream_url in normal_results:
        if recordings_triggered >= available_slots:
            recordings_blocked_by_limit += 1
            log(f"[MONITOR] ⛔ {username} is NORMAL live, but no recording slot is available")
            continue
        if not stream_url:
            recordings_failed += 1
            continue
        log(f"[MONITOR] ✅ {username} is LIVE (NORMAL). Reserving and dispatching recording...")
        if await trigger_recording(stream_url, username):
            recordings_triggered += 1
            active_usernames.add(username.lower())
        else:
            recordings_failed += 1

    elapsed_total = time.time() - start_time
    log("=" * 60)
    log(f"Auto-Monitor Completed in {elapsed_total:.1f}s")
    log(f"  - Watchlist: {total_watchlist}")
    log(f"  - Already active: {skipped_active_count}")
    log(f"  - Checked now: {len(users_to_check)}")
    log(f"  - Normal Live: {stats['live_normal']}")
    log(f"  - Premium Live: {stats['live_premium']}")
    log(f"  - Offline: {stats['offline']}")
    log(f"  - Unknown: {stats['unknown']}")
    log(f"  - Errors: {stats['error']}")
    log(f"  - Existing active leases: {active_count}")
    log(f"  - Recording slots available: {available_slots}")
    log(f"  - Dispatches accepted: {recordings_triggered}")
    log(f"  - Dispatches failed: {recordings_failed}")
    log(f"  - Normal blocked by limit: {recordings_blocked_by_limit}")
    log("=" * 60)

    summary = (
        f"✅ انتهى الفحص في {elapsed_total:.1f} ثانية.\n\n"
        f"📊 إحصائيات القائمة ({total_watchlist} مستخدم):\n"
        f"• قيد التسجيل/البدء مسبقاً: {skipped_active_count}\n"
        f"• تم فحصه الآن: {len(users_to_check)}\n\n"
        f"📈 نتائج الفحص:\n"
        f"• 🟢 بث عادي: {stats['live_normal']}\n"
        f"• 🟡 بث مدفوع (تم التجاهل): {stats['live_premium']}\n"
        f"• ⚪ غير متصل مؤكد: {stats['offline']}\n"
        f"• ❔ غير محسوم: {stats['unknown']}\n"
        f"• ⚠️ أخطاء فحص: {stats['error']}\n\n"
    )
    if recordings_triggered:
        summary += f"🔴 تم قبول {recordings_triggered} تسجيل(ات) جديد(ة) وحجز مقعد لكل منها."
    elif stats["live_normal"] == 0:
        summary += "⚪ لم يتم بدء تسجيل جديد: لا توجد بثوث عادية مؤكدة للتسجيل."
    elif available_slots == 0:
        summary += f"⛔ لم يبدأ تسجيل جديد: تم الوصول إلى الحد الأقصى {MAX_TOTAL_RECORDINGS}."
    elif recordings_failed:
        summary += "⚠️ تم اكتشاف بث عادي، لكن فشل حجز/تشغيل بعض التسجيلات."
    else:
        summary += "⚪ لم يتم بدء تسجيل جديد."
    if recordings_blocked_by_limit:
        summary += f"\n\n⛔ بثوث عادية محجوبة بسبب الحد: {recordings_blocked_by_limit}"
    if recordings_failed:
        summary += f"\n⚠️ فشل الحجز/التشغيل: {recordings_failed}"
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
