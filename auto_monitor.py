# auto_monitor.py - Complete Version with Status Verification

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

# âœ… Optimized Settings
MAX_CONCURRENT_USERS = 5
MAX_WAIT_SECONDS = 4.0
POLL_INTERVAL = 0.2

# âœ… Safety Settings
MAX_RECORDING_AGE_HOURS = 6
MAX_STATUS_STALE_MINUTES = 10  # Ø¥Ø°Ø§ Ù„Ù… ÙŠÙØ­Ø¯Ù‘Ø« status Ø®Ù„Ø§Ù„ 10 Ø¯Ù‚Ø§Ø¦Ù‚ â†’ ØªÙˆÙ‚Ù

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
    await telegram_request("sendMessage", {"chat_id": ADMIN_USER_ID, "text": text})

# ============================================================
# GitHub State Management
# ============================================================

async def github_get_file(path):
    if not GITHUB_TOKEN:
        return None
    
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
                    log(f"[GITHUB] GET failed for {path}: HTTP {resp.status}")
                    return None
                
                data = await resp.json()
                encoded = data.get("content")
                if not encoded:
                    return None
                
                try:
                    raw = base64.b64decode(encoded.replace("\n", "")).decode("utf-8", errors="replace")
                    return json.loads(raw)
                except Exception as exc:
                    log(f"[GITHUB] Decode failed: {exc}")
                    return None
    except Exception as exc:
        log(f"[GITHUB] Exception: {exc}")
        return None

async def github_get_file_last_commit_time(path):
    """Get the last commit time for a file (when it was last updated)"""
    if not GITHUB_TOKEN:
        return None
    
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/commits?path={path}&per_page=1"
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
                    commit_date = data[0].get("commit", {}).get("committer", {}).get("date", "")
                    if commit_date:
                        return datetime.fromisoformat(commit_date.replace("Z", "+00:00"))
                return None
    except Exception as exc:
        log(f"[GITHUB] Exception getting commit time for {path}: {exc}")
        return None

async def github_delete_file(path, message):
    """Delete file from GitHub using PAT_TOKEN (write permissions)"""
    if not PAT_TOKEN:
        log("[GITHUB] Cannot delete: PAT_TOKEN missing")
        return
    
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
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
                
            body = {"message": message, "sha": sha}
            async with session.delete(url, headers=headers, json=body) as resp:
                if resp.status not in (200, 204):
                    log(f"[GITHUB] Delete failed for {path}: HTTP {resp.status}")
                else:
                    log(f"[GITHUB] âœ… Deleted {path}")
    except Exception as exc:
        log(f"[GITHUB] Exception deleting {path}: {exc}")

async def is_recording_actually_alive(record_id, started_at_str):
    """
    Verify if a recording is actually still running by checking:
    1. File age (must be < MAX_RECORDING_AGE_HOURS)
    2. Status file existence and freshness (must be updated within MAX_STATUS_STALE_MINUTES)
    
    Returns True if recording is alive, False if it should be cleaned up.
    """
    # Check 1: File age
    if started_at_str:
        try:
            start_time = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - start_time
            age_hours = age.total_seconds() / 3600
            
            if age_hours > MAX_RECORDING_AGE_HOURS:
                log(f"[VERIFY] âŒ {record_id}: File too old ({age_hours:.1f}h > {MAX_RECORDING_AGE_HOURS}h)")
                return False
        except Exception as exc:
            log(f"[VERIFY] âš ï¸ {record_id}: Error parsing started_at: {exc}")
    
    # Check 2: Status file freshness
    status_path = f".recorder/status/{record_id}.json"
    status_data = await github_get_file(status_path)
    
    if status_data is None:
        # No status file exists
        # If recording just started (< 2 minutes ago), give it benefit of doubt
        if started_at_str:
            try:
                start_time = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
                age_minutes = (datetime.now(timezone.utc) - start_time).total_seconds() / 60
                if age_minutes < 2:
                    log(f"[VERIFY] âœ… {record_id}: Just started ({age_minutes:.1f}m ago), no status yet - OK")
                    return True
            except Exception:
                pass
        
        log(f"[VERIFY] âŒ {record_id}: No status file found - recording likely stopped")
        return False
    
    # Status file exists - check when it was last updated
    last_commit_time = await github_get_file_last_commit_time(status_path)
    
    if last_commit_time is None:
        # Cannot determine last update time - be conservative and keep it
        log(f"[VERIFY] âš ï¸ {record_id}: Cannot check status freshness - keeping as active")
        return True
    
    stale_minutes = (datetime.now(timezone.utc) - last_commit_time).total_seconds() / 60
    
    if stale_minutes > MAX_STATUS_STALE_MINUTES:
        log(f"[VERIFY] âŒ {record_id}: Status stale ({stale_minutes:.1f}m > {MAX_STATUS_STALE_MINUTES}m) - recording stopped")
        return False
    
    log(f"[VERIFY] âœ… {record_id}: Status fresh ({stale_minutes:.1f}m ago) - recording alive")
    return True

async def github_get_active_usernames():
    """
    Get list of usernames currently being recorded.
    Verifies each recording is actually alive before including it.
    Cleans up stale/dead recordings.
    """
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
                    return []
                
                data = await resp.json()
                tree = data.get("tree", [])
                
                active_files = [f for f in tree if f["path"].startswith(".recorder/active/") and f["path"].endswith(".json")]
                
                if not active_files:
                    log("[MONITOR] No active recordings found")
                    return []
                
                log(f"[MONITOR] Found {len(active_files)} active file(s), verifying...")
                
                usernames = []
                for file_info in active_files:
                    file_data = await github_get_file(file_info["path"])
                    if not file_data or "username" not in file_data:
                        continue
                    
                    record_id = file_data.get("record_id", "UNKNOWN")
                    started_at = file_data.get("started_at", "")
                    username = file_data["username"]
                    
                    # âœ… Verify recording is actually alive
                    is_alive = await is_recording_actually_alive(record_id, started_at)
                    
                    if is_alive:
                        usernames.append(username.lower())
                        log(f"[MONITOR] âœ… {username} is genuinely recording (#{record_id})")
                    else:
                        # Clean up dead recording
                        log(f"[MONITOR] ðŸ—‘ï¸ {username} recording is dead, cleaning up (#{record_id})...")
                        await github_delete_file(file_info["path"], f"Cleanup dead recording {record_id}")
                        # Also clean up status file if exists
                        status_path = f".recorder/status/{record_id}.json"
                        await github_delete_file(status_path, f"Cleanup dead status {record_id}")
                
                return usernames
    except Exception as exc:
        log(f"[GITHUB] Exception getting active usernames: {exc}")
        return []

# ============================================================
# GitHub Actions Trigger
# ============================================================

def generate_record_id():
    import random
    import string
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choice(chars) for _ in range(6))

async def trigger_recording(stream_url, username):
    """Trigger GitHub Actions to start recording using PAT_TOKEN"""
    if not PAT_TOKEN:
        log("[GITHUB] Cannot trigger recording: PAT_TOKEN missing")
        await send_message("âŒ Ø®Ø·Ø£ ÙÙŠ Ø§Ù„Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª: PAT_TOKEN ØºÙŠØ± Ù…ÙˆØ¬ÙˆØ¯.\n\nÙŠØ±Ø¬Ù‰ Ø¥Ø¶Ø§ÙØ© PAT_TOKEN ÙÙŠ GitHub Secrets.")
        return False
    
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/dispatches"
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
            "username": username
        }
    }
    
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=body) as resp:
                if resp.status == 204:
                    log(f"[GITHUB] Successfully triggered recording for {username} (ID: {record_id})")
                    await send_message(f"ðŸ”´ Auto-Record: Ø¨Ø¯Ø£ ØªØ³Ø¬ÙŠÙ„ {username} ØªÙ„Ù‚Ø§Ø¦ÙŠØ§Ù‹! (#{record_id})")
                    return True
                else:
                    text = await resp.text()
                    log(f"[GITHUB] Trigger failed: HTTP {resp.status} - {text[:500]}")
                    return False
    except Exception as exc:
        log(f"[GITHUB] Exception triggering recording: {exc}")
        return False

# ============================================================
# Premium Detection
# ============================================================

def is_premium_stream(stream_data, details_data):
    """
    Check if stream is premium based on multiple fields.
    Returns True if premium, False otherwise.
    """
    if stream_data:
        premium_fields = [
            "type", "payType", "isPremium", "premium", "vip", 
            "exclusive", "locked", "private", "paid", "streamType"
        ]
        
        for field in premium_fields:
            value = str(stream_data.get(field, "")).lower()
            if any(keyword in value for keyword in ["premium", "paid", "vip", "exclusive", "locked", "private"]):
                log(f"[MONITOR] ðŸŸ¡ Premium detected via stream.{field}={value}")
                return True
        
        stream_url = stream_data.get("url", "") or stream_data.get("streamUrl", "") or stream_data.get("hlsUrl", "")
        if not stream_url or stream_url.strip() == "":
            log("[MONITOR] ðŸŸ¡ Premium detected: stream object exists but no stream URL")
            return True
    
    if details_data:
        premium_fields = [
            "isPremium", "premium", "vip", "exclusive", 
            "locked", "private", "paid", "payType", "streamType"
        ]
        
        for field in premium_fields:
            value = str(details_data.get(field, "")).lower()
            if any(keyword in value for keyword in ["premium", "paid", "vip", "exclusive", "locked", "private"]):
                log(f"[MONITOR] ðŸŸ¡ Premium detected via details.{field}={value}")
                return True
    
    return False

# ============================================================
# Playwright Monitor (with Smart Early Exit)
# ============================================================

async def check_user_live_status(browser, username):
    """
    Smart Early Exit:
    - Checks every 0.2 seconds if stream detected
    - Exits immediately when found (saves time)
    - Waits full 4 seconds only for OFFLINE users
    """
    profile_url = f"https://www.tango.me/{username}"
    start_time = time.time()
    
    log(f"[MONITOR] [{username}] Checking at {profile_url}")
    
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    )
    
    page = await context.new_page()
    
    stream_info = {
        "status": StreamStatus.OFFLINE,
        "stream_url": None,
        "stream_id": None,
        "watch_api_data": None,
    }
    
    async def handle_response(response):
        try:
            url = response.url
            
            # Look for Tango's stream watch API
            if "proxycador/api/public/v1/live/stream/v2/watch" in url:
                log(f"[MONITOR] [{username}] Found watch API")
                if response.status == 200:
                    try:
                        data = await response.json()
                        stream_info["watch_api_data"] = data
                        
                        if data and "body" in data and "details" in data["body"]:
                            details = data["body"]["details"]
                            
                            if "stream" in details:
                                stream = details["stream"]
                                
                                if is_premium_stream(stream, details):
                                    stream_info["status"] = StreamStatus.LIVE_PREMIUM
                                    stream_info["stream_id"] = stream.get("id")
                                    log(f"[MONITOR] [{username}] ðŸŸ¡ PREMIUM detected")
                                else:
                                    stream_info["status"] = StreamStatus.LIVE_NORMAL
                                    stream_info["stream_id"] = stream.get("id")
                                    stream_info["stream_url"] = profile_url
                                    log(f"[MONITOR] [{username}] âœ… NORMAL live detected")
                            else:
                                log(f"[MONITOR] [{username}] Watch API has no stream â†’ OFFLINE")
                    except Exception as exc:
                        log(f"[MONITOR] [{username}] Error parsing watch API: {exc}")
            
            # Look for actual m3u8 requests
            if ".m3u8" in url.lower() and stream_info["status"] == StreamStatus.OFFLINE:
                log(f"[MONITOR] [{username}] âœ… m3u8 detected")
                stream_info["status"] = StreamStatus.LIVE_NORMAL
                stream_info["stream_url"] = profile_url
                    
        except Exception as exc:
            log(f"[MONITOR] [{username}] Error in response handler: {exc}")
    
    page.on("response", handle_response)
    
    try:
        # Navigate to profile page
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
        
        # Smart Early Exit: Poll every 0.2s, exit early if detected
        max_iterations = int(MAX_WAIT_SECONDS / POLL_INTERVAL)
        for i in range(max_iterations):
            await asyncio.sleep(POLL_INTERVAL)
            
            # Early exit if stream detected
            if stream_info["status"] != StreamStatus.OFFLINE:
                elapsed = time.time() - start_time
                log(f"[MONITOR] [{username}] âš¡ Early exit after {elapsed:.1f}s (status: {stream_info['status']})")
                break
        
    except Exception as exc:
        log(f"[MONITOR] [{username}] Error: {exc}")
    finally:
        await context.close()
    
    elapsed = time.time() - start_time
    log(f"[MONITOR] [{username}] Completed in {elapsed:.1f}s: status={stream_info['status']}")
    
    return stream_info["status"], stream_info["stream_url"]

# ============================================================
# Main Monitor Loop (with Concurrency)
# ============================================================

async def main():
    log("=" * 60)
    log("Auto-Monitor Started (Complete Version)")
    log("=" * 60)
    
    start_time = time.time()
    
    # Get watchlist
    watchlist_data = await github_get_file(WATCHLIST_PATH)
    if not watchlist_data:
        log("[MONITOR] Watchlist is empty or not found")
        return 0
    
    watchlist = watchlist_data if isinstance(watchlist_data, list) else []
    
    if not watchlist:
        log("[MONITOR] Watchlist is empty")
        return 0
    
    log(f"[MONITOR] Found {len(watchlist)} users in watchlist")
    
    # Get currently recording usernames (with verification and cleanup)
    active_usernames = await github_get_active_usernames()
    log(f"[MONITOR] Verified active recordings: {active_usernames}")
    
    # Filter users to check
    users_to_check = []
    for username in watchlist:
        username_lower = username.lower()
        if username_lower in active_usernames:
            log(f"[MONITOR] {username} is already recording, skipping")
        else:
            users_to_check.append(username)
    
    log(f"[MONITOR] Will check {len(users_to_check)} users (skipped {len(watchlist) - len(users_to_check)} already recording)")
    
    # Initialize statistics
    stats = {
        "offline": 0,
        "live_normal": 0,
        "live_premium": 0,
    }
    
    new_recordings = 0
    max_new_recordings = 5
    
    # Use shared browser with concurrency
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        
        # Create semaphore for concurrency control
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_USERS)
        
        async def check_with_semaphore(username):
            async with semaphore:
                try:
                    status, url = await check_user_live_status(browser, username)
                    return username, status, url
                except Exception as exc:
                    log(f"[MONITOR] Error checking {username}: {exc}")
                    return username, StreamStatus.OFFLINE, None
        
        # Check all users concurrently
        log(f"[MONITOR] Starting concurrent check for {len(users_to_check)} users...")
        tasks = [check_with_semaphore(username) for username in users_to_check]
        results = await asyncio.gather(*tasks)
        
        await browser.close()
    
    # Process results and trigger recordings
    for username, status, stream_url in results:
        if new_recordings >= max_new_recordings:
            log(f"[MONITOR] Reached max new recordings limit ({max_new_recordings})")
            break
        
        if status == StreamStatus.LIVE_NORMAL and stream_url:
            log(f"[MONITOR] âœ… {username} is LIVE (NORMAL)! Starting recording...")
            stats["live_normal"] += 1
            
            success = await trigger_recording(stream_url, username)
            if success:
                new_recordings += 1
                active_usernames.append(username.lower())
        
        elif status == StreamStatus.LIVE_PREMIUM:
            log(f"[MONITOR] ðŸŸ¡ {username} is LIVE but PREMIUM - SKIPPING")
            stats["live_premium"] += 1
            await send_message(f"ðŸŸ¡ {username} ÙŠØ¨Ø« Ø­Ø§Ù„ÙŠØ§Ù‹ Ù„ÙƒÙ† Ø§Ù„Ø¨Ø« Ù…Ø¯ÙÙˆØ¹ (Premium) - ØªÙ… Ø§Ù„ØªØ¬Ø§Ù‡Ù„")
        
        else:
            stats["offline"] += 1
    
    elapsed_total = time.time() - start_time
    
    log("=" * 60)
    log(f"Auto-Monitor Completed in {elapsed_total:.1f}s:")
    log(f"  - Normal Live: {stats['live_normal']}")
    log(f"  - Premium Live: {stats['live_premium']}")
    log(f"  - Offline: {stats['offline']}")
    log(f"  - New recordings started: {new_recordings}")
    log("=" * 60)
    
    # Send summary message
    summary = f"âœ… Ø§Ù†ØªÙ‡Ù‰ Ø§Ù„ÙØ­Øµ ÙÙŠ {elapsed_total:.1f} Ø«Ø§Ù†ÙŠØ©.\n\n"
    summary += f"ðŸ“Š Ø§Ù„Ù†ØªØ§Ø¦Ø¬:\n"
    summary += f"â€¢ Ø¨Ø« Ø¹Ø§Ø¯ÙŠ: {stats['live_normal']}\n"
    summary += f"â€¢ Ø¨Ø« Ù…Ø¯ÙÙˆØ¹: {stats['live_premium']}\n"
    summary += f"â€¢ ØºÙŠØ± Ù…ØªØµÙ„: {stats['offline']}\n\n"
    
    if new_recordings > 0:
        summary += f"ðŸ”´ ØªÙ… Ø¨Ø¯Ø¡ {new_recordings} ØªØ³Ø¬ÙŠÙ„(Ø§Øª) Ø¬Ø¯ÙŠØ¯(Ø©)."
    else:
        summary += f"âšª Ù„Ù… ÙŠØªÙ… Ø¨Ø¯Ø¡ Ø£ÙŠ ØªØ³Ø¬ÙŠÙ„ Ø¬Ø¯ÙŠØ¯."
    
    await send_message(summary)
    
    return 0

# ============================================================
# Entrypoint
# ============================================================

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        log("[MONITOR] Keyboard interrupt.")
        sys.exit(0)
    except Exception as exc:
        log(f"[FATAL] {type(exc).__name__}: {exc}")
        try:
            asyncio.run(send_message(f"âŒ Ø­Ø¯Ø« Ø®Ø·Ø£ ÙÙŠ Auto-Monitor: {type(exc).__name__}"))
        except Exception:
            pass
        sys.exit(1)
