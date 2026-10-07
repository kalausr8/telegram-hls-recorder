# auto_monitor.py - Modified version

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

DISCOVERY_TIMEOUT = 30

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
# Premium Detection Helpers
# ============================================================

def is_premium_stream(stream_data, details_data):
    """
    Check if stream is premium based on multiple fields.
    Returns True if premium, False otherwise.
    """
    # Check stream object for premium indicators
    if stream_data:
        # Check common premium field names
        premium_fields = [
            "type", "payType", "isPremium", "premium", "vip", 
            "exclusive", "locked", "private", "paid"
        ]
        
        for field in premium_fields:
            value = stream_data.get(field, "").lower()
            if any(keyword in value for keyword in ["premium", "paid", "vip", "exclusive", "locked", "private"]):
                log(f"[MONITOR] 🟡 Premium detected via stream.{field}={value}")
                return True
        
        # Check if stream URL is missing or empty (common for premium)
        stream_url = stream_data.get("url", "") or stream_data.get("streamUrl", "")
        if not stream_url or stream_url.strip() == "":
            log("[MONITOR] 🟡 Premium detected: stream object exists but no stream URL")
            return True
    
    # Check details object for premium indicators
    if details_data:
        premium_fields = [
            "isPremium", "premium", "vip", "exclusive", 
            "locked", "private", "paid", "payType"
        ]
        
        for field in premium_fields:
            value = details_data.get(field, "").lower()
            if any(keyword in value for keyword in ["premium", "paid", "vip", "exclusive", "locked", "private"]):
                log(f"[MONITOR] 🟡 Premium detected via details.{field}={value}")
                return True
    
    return False

def has_premium_indicators_in_html(html_content):
    """
    Check HTML for premium indicators.
    Returns True if premium indicators found.
    """
    premium_indicators = [
        "premium", "vip", "exclusive", "locked", "private",
        "paid stream", "buy coins", "unlock", "member only",
        "premium only", "vip only", "exclusive stream"
    ]
    
    html_lower = html_content.lower()
    for indicator in premium_indicators:
        if indicator in html_lower:
            # Make sure it's not part of a word (e.g., "premium" not "premiumuser")
            # Simple check: look for space or punctuation around it
            if f' {indicator} ' in html_lower or f'"{indicator}"' in html_lower or f"'{indicator}'" in html_lower:
                log(f"[MONITOR] 🟡 Premium indicator found in HTML: {indicator}")
                return True
    
    return False

# ============================================================
# Playwright Monitor (Enhanced Version with Premium Detection)
# ============================================================

async def check_user_live_status(username):
    """
    Use Playwright to check if a user is live and classify stream type.
    Returns (status, stream_url) tuple where status is StreamStatus constant.
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
            "detected_urls": [],
            "watch_api_data": None,  # Store full API response for analysis
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
                            stream_info["watch_api_data"] = data  # Store for analysis
                            
                            # Check if response contains stream info
                            if data and "body" in data and "details" in data["body"]:
                                details = data["body"]["details"]
                                
                                # ✅ NEW: Check for premium indicators first
                                if "stream" in details:
                                    stream = details["stream"]
                                    
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
                        except Exception as exc:
                            log(f"[MONITOR] Error parsing watch API response: {exc}")
                
                # Look for m3u8 URLs (only if not already premium)
                if ".m3u8" in url.lower() and stream_info["status"] != StreamStatus.LIVE_PREMIUM:
                    log(f"[MONITOR] ✅ Found m3u8 URL: {url}")
                    if stream_info["status"] == StreamStatus.OFFLINE:
                        stream_info["status"] = StreamStatus.LIVE_NORMAL
                        stream_info["stream_url"] = profile_url
                        stream_info["detected_urls"].append(url)
                        
            except Exception as exc:
                log(f"[MONITOR] Error in response handler: {exc}")
        
        # Method 2: Intercept network requests
        async def handle_request(request):
            try:
                url = request.url
                
                # Look for m3u8 in requests too (only if not already premium)
                if ".m3u8" in url.lower() and stream_info["status"] != StreamStatus.LIVE_PREMIUM:
                    log(f"[MONITOR] ✅ Found m3u8 in request: {url}")
                    if stream_info["status"] == StreamStatus.OFFLINE:
                        stream_info["status"] = StreamStatus.LIVE_NORMAL
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
            
            # Method 3: Check page content for live indicators and premium markers
            log("[MONITOR] Checking page content for live/premium indicators...")
            try:
                content = await page.content()
                
                # ✅ NEW: Check for premium indicators in HTML first
                if has_premium_indicators_in_html(content):
                    if stream_info["status"] == StreamStatus.OFFLINE:
                        stream_info["status"] = StreamStatus.LIVE_PREMIUM
                        stream_info["stream_url"] = profile_url
                        log("[MONITOR] 🟡 Premium stream detected via HTML indicators")
                
                # Look for common live indicators in HTML (only if not premium)
                if stream_info["status"] != StreamStatus.LIVE_PREMIUM:
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
                            if stream_info["status"] == StreamStatus.OFFLINE:
                                stream_info["status"] = StreamStatus.LIVE_NORMAL
                                stream_info["stream_url"] = profile_url
                
                # Log page title for debugging
                title = await page.title()
                log(f"[MONITOR] Page title: {title}")
                
            except Exception as exc:
                log(f"[MONITOR] Error checking page content: {exc}")
            
            # Method 4: Try to find and click play button if exists (only for normal streams)
            if stream_info["status"] == StreamStatus.LIVE_NORMAL:
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
        log(f"[MONITOR] Final result for {username}: status={stream_info['status']}, stream_url={stream_info['stream_url']}")
        if stream_info["detected_urls"]:
            log(f"[MONITOR] Detected URLs: {stream_info['detected_urls']}")
        
        # ✅ NEW: Log watch API response for debugging (first 1000 chars)
        if stream_info["watch_api_data"]:
            log(f"[MONITOR] Watch API response (first 1000 chars): {json.dumps(stream_info['watch_api_data'], indent=2)[:1000]}")
        
        return stream_info["status"], stream_info["stream_url"]

# ============================================================
# Main Monitor Loop
# ============================================================

async def main():
    log("=" * 60)
    log("Auto-Monitor Started (Enhanced Version with Premium Detection)")
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
            # ✅ NEW: Send notification for premium streams
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
