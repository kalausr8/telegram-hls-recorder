# auto_monitor.py - Final Corrected & Robust Version

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

# ✅ Optimized & Robust Settings
MAX_CONCURRENT_USERS = 5
MAX_WAIT_SECONDS = 8.0      # وقت انتظار آمن لضمان تحميل البث
POLL_INTERVAL = 0.5         # فحص كل نصف ثانية

# ✅ Safety Settings
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
                    log(f"[GITHUB] ✅ Deleted {path}")
    except Exception as exc:
        log(f"[GITHUB] Exception deleting {path}: {exc}")

async def is_recording_actually_alive(record_id, started_at_str):
    if started_at_str:
        try:
            start_time = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - start_time
            age_hours = age.total_seconds() / 3600
            
            if age_hours > MAX_RECORDING_AGE_HOURS:
                log(f"[VERIFY] ❌ {record_id}: File too old ({age_hours:.1f}h > {MAX_RECORDING_AGE_HOURS}h)")
                return False
        except Exception as exc:
            log(f"[VERIFY] ⚠️ {record_id}: Error parsing started_at: {exc}")
    
    status_path = f".recorder/status/{record_id}.json"
    status_data = await github_get_file(status_path)
    
    if status_data is None:
        if started_at_str:
            try:
                start_time = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
                age_minutes = (datetime.now(timezone.utc) - start_time).total_seconds() / 60
                if age_minutes < 2:
                    log(f"[VERIFY] ✅ {record_id}: Just started ({age_minutes:.1f}m ago), no status yet - OK")
                    return True
            except Exception:
                pass
        log(f"[VERIFY] ❌ {record_id}: No status file found - recording likely stopped")
        return False
    
    last_commit_time = await github_get_file_last_commit_time(status_path)
    if last_commit_time is None:
        log(f"[VERIFY] ⚠️ {record_id}: Cannot check status freshness - keeping as active")
        return True
    
    stale_minutes = (datetime.now(timezone.utc) - last_commit_time).total_seconds() / 60
    if stale_minutes > MAX_STATUS_STALE_MINUTES:
        log(f"[VERIFY] ❌ {record_id}: Status stale ({stale_minutes:.1f}m > {MAX_STATUS_STALE_MINUTES}m) - recording stopped")
        return False
    
    log(f"[VERIFY] ✅ {record_id}: Status fresh ({stale_minutes:.1f}m ago) - recording alive")
    return True

async def github_get_active_usernames():
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
                    
                    is_alive = await is_recording_actually_alive(record_id, started_at)
                    
                    if is_alive:
                        usernames.append(username.lower())
                        log(f"[MONITOR] ✅ {username} is genuinely recording (#{record_id})")
                    else:
                        log(f"[MONITOR] 🗑️ {username} recording is dead, cleaning up (#{record_id})...")
                        await github_delete_file(file_info["path"], f"Cleanup dead recording {record_id}")
                        await github_delete_file(f".recorder/status/{record_id}.json", f"Cleanup dead status {record_id}")
                
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
    if not PAT_TOKEN:
        log("[GITHUB] Cannot trigger recording: PAT_TOKEN missing")
        await send_message("❌ خطأ في الإعدادات: PAT_TOKEN غير موجود.")
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
    if stream_data:
        premium_fields = ["type", "payType", "isPremium", "premium", "vip", "exclusive", "locked", "private", "paid", "streamType"]
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
        premium_fields = ["isPremium", "premium", "vip", "exclusive", "locked", "private", "paid", "payType", "streamType"]
        for field in premium_fields:
            value = str(details_data.get(field, "")).lower()
            if any(keyword in value for keyword in ["premium", "paid", "vip", "exclusive", "locked", "private"]):
                log(f"[MONITOR] 🟡 Premium detected via details.{field}={value}")
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
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    )
    
    page = await context.new_page()
    
    stream_info = {
        "status": StreamStatus.OFFLINE,
        "stream_url": None,
        "stream_id": None,
    }
    
    async def handle_response(response):
        try:
            url = response.url
            if "proxycador/api/public/v1/live/stream/v2/watch" in url:
                if response.status == 200:
                    try:
                        data = await response.json()
                        if data and "body" in data and "details" in data["body"]:
                            details = data["body"]["details"]
                            if "stream" in details:
                                stream = details["stream"]
                                if is_premium_stream(stream, details):
                                    stream_info["status"] = StreamStatus.LIVE_PREMIUM
                                    stream_info["stream_id"] = stream.get("id")
                                    log(f"[MONITOR] [{username}] 🟡 PREMIUM detected")
                                else:
                                    stream_info["status"] = StreamStatus.LIVE_NORMAL
                                    stream_info["stream_id"] = stream.get("id")
                                    stream_info["stream_url"] = profile_url
                                    log(f"[MONITOR] [{username}] ✅ NORMAL live detected via API")
                    except Exception:
                        pass
            
            if ".m3u8" in url.lower() and stream_info["status"] == StreamStatus.OFFLINE:
                log(f"[MONITOR] [{username}] ✅ m3u8 detected")
                stream_info["status"] = StreamStatus.LIVE_NORMAL
                stream_info["stream_url"] = profile_url
                    
        except Exception:
            pass
    
    page.on("response", handle_response)
    
    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
        
        # ✅ خدعة حركة الماوس لإجبار المتصفح على تحميل مشغل الفيديو
        try:
            await page.mouse.move(100, 100)
            await asyncio.sleep(0.5)
        except Exception:
            pass
        
        max_iterations = int(MAX_WAIT_SECONDS / POLL_INTERVAL)
        for i in range(max_iterations):
            await asyncio.sleep(POLL_INTERVAL)
            if stream_info["status"] != StreamStatus.OFFLINE:
                elapsed = time.time() - start_time
                log(f"[MONITOR] [{username}] ⚡ Early exit after {elapsed:.1f}s (status: {stream_info['status']})")
                break
        
    except Exception as exc:
        log(f"[MONITOR] [{username}] Error: {exc}")
    finally:
        await context.close()
    
    elapsed = time.time() - start_time
    log(f"[MONITOR] [{username}] Completed in {elapsed:.1f}s: status={stream_info['status']}")
    
    return stream_info["status"], stream_info["stream_url"]

# ============================================================
# Main Monitor Loop
# ============================================================

async def main():
    log("=" * 60)
    log("Auto-Monitor Started (Final Corrected Version)")
    log("=" * 60)
    
    start_time = time.time()
    
    watchlist_data = await github_get_file(WATCHLIST_PATH)
    if not watchlist_data or not isinstance(watchlist_data, list) or not watchlist_data:
        log("[MONITOR] Watchlist is empty or not found")
        return 0
    
    total_watchlist = len(watchlist_data)
    log(f"[MONITOR] Found {total_watchlist} users in watchlist")
    
    active_usernames = await github_get_active_usernames()
    log(f"[MONITOR] Verified active recordings: {active_usernames}")
    
    users_to_check = []
    # ✅ تم تصحيح الخطأ هنا: استخدام watchlist_data بدلاً من watchlist
    for username in watchlist_data:
        username_lower = username.lower()
        if username_lower in active_usernames:
            log(f"[MONITOR] {username} is already recording, skipping")
        else:
            users_to_check.append(username)
    
    log(f"[MONITOR] Will check {len(users_to_check)} users (skipped {total_watchlist - len(users_to_check)} already recording)")
    
    stats = {"offline": 0, "live_normal": 0, "live_premium": 0}
    new_recordings = 0
    max_new_recordings = 5
    
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
                    return username, StreamStatus.OFFLINE, None
        
        log(f"[MONITOR] Starting concurrent check for {len(users_to_check)} users...")
        tasks = [check_with_semaphore(username) for username in users_to_check]
        results = await asyncio.gather(*tasks)
        await browser.close()
    
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
            stats["offline"] += 1
    
    elapsed_total = time.time() - start_time
    
    log("=" * 60)
    log(f"Auto-Monitor Completed in {elapsed_total:.1f}s:")
    log(f"  - Normal Live: {stats['live_normal']}")
    log(f"  - Premium Live: {stats['live_premium']}")
    log(f"  - Offline: {stats['offline']}")
    log(f"  - New recordings started: {new_recordings}")
    log("=" * 60)
    
    summary = f"✅ انتهى الفحص في {elapsed_total:.1f} ثانية.\n\n"
    summary += f"📊 إحصائيات القائمة ({total_watchlist} مستخدم):\n"
    summary += f"• قيد التسجيل مسبقاً: {total_watchlist - len(users_to_check)}\n"
    summary += f"• تم فحصه الآن: {len(users_to_check)}\n\n"
    summary += f"📈 نتائج الفحص:\n"
    summary += f"• 🟢 بث عادي (تم التسجيل): {stats['live_normal']}\n"
    summary += f"• 🟡 بث مدفوع (تم التجاهل): {stats['live_premium']}\n"
    summary += f"• ⚪ غير متصل: {stats['offline']}\n\n"
    
    if new_recordings > 0:
        summary += f"🔴 تم بدء {new_recordings} تسجيل(ات) جديد(ة) بنجاح."
    else:
        summary += f"⚪ لم يتم بدء أي تسجيل جديد (الجميع أوفلاين أو قيد التسجيل)."
    
    await send_message(summary)
    return 0

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
