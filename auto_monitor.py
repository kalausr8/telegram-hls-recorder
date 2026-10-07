# auto_monitor.py - Optimized Version

import asyncio
import base64
import json
import os
import sys
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

# ✅ أسرع: 15 ثانية بدلاً من 30
WATCH_API_TIMEOUT = 10000  # 10 seconds
M3U8_TIMEOUT = 5000  # 5 seconds

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
    # Check stream object for premium indicators
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
        
        # Check if stream URL is missing (common for premium)
        stream_url = stream_data.get("url", "") or stream_data.get("streamUrl", "") or stream_data.get("hlsUrl", "")
        if not stream_url or stream_url.strip() == "":
            log("[MONITOR] 🟡 Premium detected: stream object exists but no stream URL")
            return True
    
    # Check details object for premium indicators
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
# Playwright Monitor (Optimized Version)
# ============================================================

async def check_user_live_status(username):
    """
    Optimized detection strategy:
    1. Wait for watch API response (10 seconds)
    2. If watch API has stream object → check for premium
    3. If no watch API → wait for m3u8 request (5 seconds)
    4. Otherwise → OFFLINE
    
    We IGNORE HTML indicators completely (unreliable).
    """
    profile_url = f"https://www.tango.me/{username}"
    
    log(f"[MONITOR] Checking {username} at {profile_url}")
    
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        )
        
        page = await context.new_page()
        
        # Track discovered stream info
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
                    log(f"[MONITOR] Found Tango watch API: {url}")
                    if response.status == 200:
                        try:
                            data = await response.json()
                            stream_info["watch_api_data"] = data
                            
                            # Check if response contains stream info
                            if data and "body" in data and "details" in data["body"]:
                                details = data["body"]["details"]
                                
                                if "stream" in details:
                                    stream = details["stream"]
                                    stream_info["has_stream_object"] = True
                                    
                                    # Log stream object for debugging
                                    log(f"[MONITOR] Stream object found: {json.dumps(stream, indent=2)[:500]}")
                                    
                                    # Check if premium
                                    if is_premium_stream(stream, details):
                                        stream_info["status"] = StreamStatus.LIVE_PREMIUM
                                        stream_info["stream_id"] = stream.get("id")
                                        log(f"[MONITOR] 🟡 Detected PREMIUM stream: ID={stream_info['stream_id']}")
                                    else:
                                        # Regular live stream
                                        stream_info["status"] = StreamStatus.LIVE_NORMAL
                                        stream_info["stream_id"] = stream.get("id")
                                        stream_info["stream_url"] = profile_url
                                        log(f"[MONITOR] ✅ Detected NORMAL live stream via watch API: ID={stream_info['stream_id']}")
                                else:
                                    log("[MONITOR] Watch API response has no stream object → OFFLINE")
                        except Exception as exc:
                            log(f"[MONITOR] Error parsing watch API response: {exc}")
                
                # Look for actual m3u8 requests (not in HTML)
                if ".m3u8" in url.lower() and stream_info["status"] == StreamStatus.OFFLINE:
                    log(f"[MONITOR] ✅ Found actual m3u8 request: {url}")
                    stream_info["status"] = StreamStatus.LIVE_NORMAL
                    stream_info["stream_url"] = profile_url
                        
            except Exception as exc:
                log(f"[MONITOR] Error in response handler: {exc}")
        
        page.on("response", handle_response)
        
        try:
            # Navigate to profile page
            log(f"[MONITOR] Navigating to {profile_url}...")
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            
            # ✅ Wait for watch API (10 seconds)
            log(f"[MONITOR] Waiting for watch API ({WATCH_API_TIMEOUT}ms)...")
            try:
                await page.wait_for_event(
                    "response",
                    lambda response: "proxycador/api/public/v1/live/stream/v2/watch" in response.url,
                    timeout=WATCH_API_TIMEOUT
                )
                log("[MONITOR] ✅ Watch API response received")
            except Exception:
                log("[MONITOR] ⏱️ No watch API response within timeout")
            
            # ✅ If watch API came but no stream object, wait for m3u8 (5 seconds)
            if stream_info["status"] == StreamStatus.OFFLINE and stream_info["watch_api_data"] is not None:
                log(f"[MONITOR] Waiting for m3u8 request ({M3U8_TIMEOUT}ms)...")
                try:
                    await page.wait_for_event(
                        "response",
                        lambda response: ".m3u8" in response.url.lower(),
                        timeout=M3U8_TIMEOUT
                    )
                    log("[MONITOR] ✅ m3u8 request detected")
                except Exception:
                    log("[MONITOR] ⏱️ No m3u8 request within timeout")
            
        except Exception as exc:
            log(f"[MONITOR] Error navigating to {profile_url}: {exc}")
        finally:
            await browser.close()
        
        # Log final result
        log(f"[MONITOR] Final result for {username}: status={stream_info['status']}, stream_url={stream_info['stream_url']}")
        
        # Log watch API response for debugging
        if stream_info["watch_api_data"]:
            log(f"[MONITOR] Watch API response (first 1000 chars): {json.dumps(stream_info['watch_api_data'], indent=2)[:1000]}")
        
        return stream_info["status"], stream_info["stream_url"]

# ============================================================
# Main Monitor Loop
# ============================================================

async def main():
    log("=" * 60)
    log("Auto-Monitor Started (Optimized Version)")
    log("=" * 60)
    
    # Get watchlist
    watchlist_data = await github_get_file(WATCHLIST_PATH)
    if not watchlist_data:
        log("[MONITOR] Watchlist is empty or not found")
        return 0
    
    watchlist = watchlist_data if isinstance(watchlist_data, list) else []
    
    if not watchlist:
        log("[MONITOR] Watchlist is empty")
        return 0
    
    log(f"[MONITOR] Found {len(watchlist)} users in watchlist: {watchlist}")
    
    # Get currently recording usernames to avoid duplicates
    active_usernames = await github_get_active_usernames()
    log(f"[MONITOR] Currently recording: {active_usernames}")
    
    # Check each user
    new_recordings = 0
    max_new_recordings = 5
    
    # Track statistics
    stats = {
        "offline": 0,
        "live_normal": 0,
        "live_premium": 0,
    }
    
    for username in watchlist:
        if new_recordings >= max_new_recordings:
            log(f"[MONITOR] Reached max new recordings limit ({max_new_recordings})")
            break
        
        username_lower = username.lower()
        
        # Skip if already recording
        if username_lower in active_usernames:
            log(f"[MONITOR] {username} is already recording, skipping")
            continue
        
        # Check if user is live
        status, stream_url = await check_user_live_status(username)
        
        if status == StreamStatus.LIVE_NORMAL and stream_url:
            log(f"[MONITOR] ✅ {username} is LIVE (NORMAL)! Starting recording...")
            stats["live_normal"] += 1
            
            # Trigger recording
            success = await trigger_recording(stream_url, username)
            if success:
                new_recordings += 1
                active_usernames.append(username_lower)
        
        elif status == StreamStatus.LIVE_PREMIUM:
            log(f"[MONITOR] 🟡 {username} is LIVE but PREMIUM - SKIPPING")
            stats["live_premium"] += 1
            await send_message(f"🟡 {username} يبث حالياً لكن البث مدفوع (Premium) - تم التجاهل")
        
        else:
            log(f"[MONITOR] ❌ {username} is OFFLINE")
            stats["offline"] += 1
    
    log("=" * 60)
    log(f"Auto-Monitor Completed:")
    log(f"  - Normal Live: {stats['live_normal']}")
    log(f"  - Premium Live: {stats['live_premium']}")
    log(f"  - Offline: {stats['offline']}")
    log(f"  - New recordings started: {new_recordings}")
    log("=" * 60)
    
    # Send summary message
    summary = f"✅ انتهى الفحص.\n\n"
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
