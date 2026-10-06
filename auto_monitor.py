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
PAT_TOKEN = os.environ.get("PAT_TOKEN", "")  # New: Personal Access Token

GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "kalausr8")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "telegram-hls-recorder")

WATCHLIST_PATH = ".recorder/config/watchlist.json"

DISCOVERY_TIMEOUT = 30

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
        "Authorization": f"Bearer {PAT_TOKEN}",  # Use PAT_TOKEN instead of GITHUB_TOKEN
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
# Playwright Monitor (Enhanced Version)
# ============================================================

async def check_user_live_status(username):
    """
    Use Playwright to check if a user is live.
    Returns (is_live, stream_url) tuple.
    Enhanced with multiple detection methods.
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
            "is_live": False,
            "stream_url": None,
            "stream_id": None,
            "detected_urls": [],
        }
        
        # Method 1: Intercept network responses
        async def handle_response(response):
            try:
                url = response.url
                
                # Look for Tango's stream watch API
                if "proxycador/api/public/v1/live/stream/v2/watch" in url:
                    log(f"[MONITOR] Found Tango watch API: {url}")
                    if response.status == 200:
                        try:
                            data = await response.json()
                            
                            # Check if response contains stream info
                            if data and "body" in data and "details" in data["body"]:
                                details = data["body"]["details"]
                                if "stream" in details:
                                    stream = details["stream"]
                                    stream_info["is_live"] = True
                                    stream_info["stream_id"] = stream.get("id")
                                    stream_info["stream_url"] = profile_url
                                    log(f"[MONITOR] ✅ Detected live stream via watch API: ID={stream_info['stream_id']}")
                        except Exception as exc:
                            log(f"[MONITOR] Error parsing watch API response: {exc}")
                
                # Look for m3u8 URLs
                if ".m3u8" in url.lower():
                    log(f"[MONITOR] ✅ Found m3u8 URL: {url}")
                    if not stream_info["is_live"]:
                        stream_info["is_live"] = True
                        stream_info["stream_url"] = profile_url
                        stream_info["detected_urls"].append(url)
                        
            except Exception as exc:
                log(f"[MONITOR] Error in response handler: {exc}")
        
        # Method 2: Intercept network requests
        async def handle_request(request):
            try:
                url = request.url
                
                # Look for m3u8 in requests too
                if ".m3u8" in url.lower():
                    log(f"[MONITOR] ✅ Found m3u8 in request: {url}")
                    if not stream_info["is_live"]:
                        stream_info["is_live"] = True
                        stream_info["stream_url"] = profile_url
                        stream_info["detected_urls"].append(url)
                        
            except Exception as exc:
                log(f"[MONITOR] Error in request handler: {exc}")
        
        page.on("response", handle_response)
        page.on("request", handle_request)
        
        try:
            # Navigate to profile page
            log(f"[MONITOR] Navigating to {profile_url}...")
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            
            # Wait a bit for dynamic content to load
            await asyncio.sleep(3)
            
            # Method 3: Check page content for live indicators
            log("[MONITOR] Checking page content for live indicators...")
            try:
                content = await page.content()
                
                # Look for common live indicators in HTML
                live_indicators = [
                    '"isLive":true',
                    '"is_live":true',
                    '"status":"live"',
                    '"live":true',
                    'LIVE',
                    'isLive',
                    'live-stream',
                    'm3u8',
                ]
                
                for indicator in live_indicators:
                    if indicator.lower() in content.lower():
                        log(f"[MONITOR] ✅ Found live indicator in HTML: {indicator}")
                        if not stream_info["is_live"]:
                            stream_info["is_live"] = True
                            stream_info["stream_url"] = profile_url
                
                # Log page title for debugging
                title = await page.title()
                log(f"[MONITOR] Page title: {title}")
                
            except Exception as exc:
                log(f"[MONITOR] Error checking page content: {exc}")
            
            # Method 4: Try to find and click play button if exists
            try:
                log("[MONITOR] Looking for play button...")
                play_button = await page.query_selector("button:has-text('Join'), button:has-text('Watch'), button:has-text('Play'), [data-testid*='play'], [class*='play']")
                if play_button:
                    log("[MONITOR] ✅ Found play button, clicking...")
                    await play_button.click()
                    await asyncio.sleep(3)
                else:
                    log("[MONITOR] No play button found")
            except Exception as exc:
                log(f"[MONITOR] Error with play button: {exc}")
            
        except Exception as exc:
            log(f"[MONITOR] Error navigating to {profile_url}: {exc}")
        finally:
            await browser.close()
        
        # Log final result
        log(f"[MONITOR] Final result for {username}: is_live={stream_info['is_live']}, stream_url={stream_info['stream_url']}")
        if stream_info["detected_urls"]:
            log(f"[MONITOR] Detected URLs: {stream_info['detected_urls']}")
        
        return stream_info["is_live"], stream_info["stream_url"]

# ============================================================
# Main Monitor Loop
# ============================================================

async def main():
    log("=" * 60)
    log("Auto-Monitor Started (Enhanced Version)")
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
        is_live, stream_url = await check_user_live_status(username)
        
        if is_live and stream_url:
            log(f"[MONITOR] ✅ {username} is LIVE! Starting recording...")
            
            # Trigger recording
            success = await trigger_recording(stream_url, username)
            if success:
                new_recordings += 1
                active_usernames.append(username_lower)
        else:
            log(f"[MONITOR] ❌ {username} is OFFLINE")
    
    log("=" * 60)
    log(f"Auto-Monitor Completed: {new_recordings} new recording(s) started")
    log("=" * 60)
    
    if new_recordings > 0:
        await send_message(f"✅ انتهى الفحص.\nتم بدء {new_recordings} تسجيل(ات) جديد(ة).")
    else:
        await send_message(f"✅ انتهى الفحص.\nجميع المستخدمين OFFLINE أو قيد التسجيل.")
    
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
