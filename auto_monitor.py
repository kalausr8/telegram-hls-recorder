# auto_monitor.py - Optimized with Concurrency

import asyncio
import base64
import json
import os
import sys
import time
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

# ✅ Concurrency Settings
MAX_CONCURRENT_USERS = 5  # فحص 5 مستخدمين في نفس الوقت
DETECTION_WAIT = 3.0  # 3 seconds for network events
WATCH_API_TIMEOUT = 5000  # 5 seconds
M3U8_TIMEOUT = 3000  # 3 seconds

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

async def github_get_active_usernames():
    """Get list of usernames currently being recorded"""
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
                
                usernames = []
                for file_info in active_files:
                    file_data = await github_get_file(file_info["path"])
                    if file_data and "username" in file_data:
                        usernames.append(file_data["username"].lower())
                
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
        await send_message("❌ خطأ في الإعدادات: PAT_TOKEN غير موجود.\n\nيرجى إضافة PAT_TOKEN في GitHub Secrets.")
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
                    await send_message(f"🔴 Auto-Record: بدأ تسجيل {username} تلقائياً! (#{record_id})")
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
                log(f"[MONITOR] 🟡 Premium detected via stream.{field}={value}")
                return True
        
        stream_url = stream_data.get("url", "") or stream_data.get("streamUrl", "") or stream_data.get("hlsUrl", "")
        if not stream_url or stream_url.strip() == "":
            log("[MONITOR] 🟡 Premium detected: stream object exists but no stream URL")
            return True
    
    if details_data:
        premium_fields = [
            "isPremium", "premium", "vip", "exclusive", 
            "locked", "private", "paid", "payType", "streamType"
        ]
        
        for field in premium_fields:
            value = str(details_data.get(field, "")).lower()
            if any(keyword in value for keyword in ["premium", "paid", "vip", "exclusive", "locked", "private"]):
                log(f"[MONITOR] 🟡 Premium detected via details.{field}={value}")
                return True
    
    return False

# ============================================================
# Playwright Monitor (Optimized with Concurrency)
# ============================================================

async def check_user_live_status(browser, username):
    """
    Optimized detection with shared browser instance.
    Uses asyncio.sleep instead of wait_for_event to allow concurrency.
    """
    profile_url = f"https://www.tango.me/{username}"
    start_time = time.time()
    
    log(f"[MONITOR] Checking {username} at {profile_url}")
    
    # Create new page for this user (shared browser)
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
        "has_stream_object": False,
    }
    
    # Intercept network responses
    async def handle_response(response):
        try:
            url = response.url
            
            # Look for Tango's stream watch API
            if "proxycador/api/public/v1/live/stream/v2/watch" in url:
                log(f"[MONITOR] [{username}] Found Tango watch API")
                if response.status == 200:
                    try:
                        data = await response.json()
                        stream_info["watch_api_data"] = data
                        
                        if data and "body" in data and "details" in data["body"]:
                            details = data["body"]["details"]
                            
                            if "stream" in details:
                                stream = details["stream"]
                                stream_info["has_stream_object"] = True
                                
                                log(f"[MONITOR] [{username}] Stream object found: {json.dumps(stream, indent=2)[:500]}")
                                
                                if is_premium_stream(stream, details):
                                    stream_info["status"] = StreamStatus.LIVE_PREMIUM
                                    stream_info["stream_id"] = stream.get("id")
                                    log(f"[MONITOR] [{username}] 🟡 Detected PREMIUM stream")
                                else:
                                    stream_info["status"] = StreamStatus.LIVE_NORMAL
                                    stream_info["stream_id"] = stream.get("id")
                                    stream_info["stream_url"] = profile_url
                                    log(f"[MONITOR] [{username}] ✅ Detected NORMAL live stream")
                            else:
                                log(f"[MONITOR] [{username}] Watch API has no stream object → OFFLINE")
                    except Exception as exc:
                        log(f"[MONITOR] [{username}] Error parsing watch API: {exc}")
            
            # Look for actual m3u8 requests
            if ".m3u8" in url.lower() and stream_info["status"] == StreamStatus.OFFLINE:
                log(f"[MONITOR] [{username}] ✅ Found m3u8 request")
                stream_info["status"] = StreamStatus.LIVE_NORMAL
                stream_info["stream_url"] = profile_url
                    
        except Exception as exc:
            log(f"[MONITOR] [{username}] Error in response handler: {exc}")
    
    page.on("response", handle_response)
    
    try:
        # Navigate to profile page
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
        
        # ✅ Wait for network events (non-blocking for concurrency)
        await asyncio.sleep(DETECTION_WAIT)
        
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
    log("Auto-Monitor Started (Optimized with Concurrency)")
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
    
    # Get currently recording usernames
    active_usernames = await github_get_active_usernames()
    log(f"[MONITOR] Currently recording: {active_usernames}")
    
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
    
    # ✅ Use shared browser with concurrency
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
        
        # ✅ Check all users concurrently
        log(f"[MONITOR] Starting concurrent check for {len(users_to_check)} users (max {MAX_CONCURRENT_USERS} at a time)...")
        tasks = [check_with_semaphore(username) for username in users_to_check]
        results = await asyncio.gather(*tasks)
        
        await browser.close()
    
    # Process results and trigger recordings
    for username, status, stream_url in results:
        if new_recordings >= max_new_recordings:
            log(f"[MONITOR] Reached max new recordings limit ({max_new_recordings})")
            break
        
        if status == StreamStatus.LIVE_NORMAL and stream_url:
            log(f"[MONITOR] ✅ {username} is LIVE (NORMAL)! Starting recording...")
            stats["live_normal"] += 1
            
            success = await trigger_recording(stream_url, username)
            if success:
                new_recordings += 1
                active_usernames.append(username.lower())
        
        elif status == StreamStatus.LIVE_PREMIUM:
            log(f"[MONITOR] 🟡 {username} is LIVE but PREMIUM - SKIPPING")
            stats["live_premium"] += 1
            await send_message(f"🟡 {username} يبث حالياً لكن البث مدفوع (Premium) - تم التجاهل")
        
        else:
            log(f"[MONITOR] ❌ {username} is OFFLINE")
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
    summary = f"✅ انتهى الفحص في {elapsed_total:.1f} ثانية.\n\n"
    summary += f"📊 النتائج:\n"
    summary += f"• بث عادي: {stats['live_normal']}\n"
    summary += f"• بث مدفوع: {stats['live_premium']}\n"
    summary += f"• غير متصل: {stats['offline']}\n\n"
    
    if new_recordings > 0:
        summary += f"🔴 تم بدء {new_recordings} تسجيل(ات) جديد(ة)."
    else:
        summary += f"⚪ لم يتم بدء أي تسجيل جديد."
    
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
            asyncio.run(send_message(f"❌ حدث خطأ في Auto-Monitor: {type(exc).__name__}"))
        except Exception:
            pass
        sys.exit(1)
