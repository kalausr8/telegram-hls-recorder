import asyncio
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
from playwright.async_api import async_playwright

# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "kalausr8")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "telegram-hls-recorder")
RECORD_ID = os.environ.get("RECORD_ID", "UNKNOWN")
TARGET_USERNAME = os.environ.get("TARGET_USERNAME", "").strip()

PAGE_URL = sys.argv[1].strip() if len(sys.argv) > 1 else ""

TELEGRAM_MAX_MB = 49
PART_TARGET_MB = 47
PART_TARGET_BYTES = PART_TARGET_MB * 1024 * 1024

DISCOVERY_TIMEOUT = 30
PLAYLIST_TIMEOUT = 20
SEGMENT_TIMEOUT = 30

# ✅ تحسين #1: تقليل ضغط GitHub API (من 2 إلى 30 ثانية)
STOP_CHECK_INTERVAL = 30.0  # فحص إشارة الإيقاف كل 30 ثانية
PLAYLIST_POLL_INTERVAL = 3.0  # فحص قائمة التشغيل كل 3 ثوانٍ

MAX_PLAYLIST_RETRIES = 5
MAX_SEGMENT_RETRIES = 3
STATUS_UPDATE_INTERVAL = 60

# ✅ تحسين #3: إعادة فتح الـ session كل 45 دقيقة
SESSION_REFRESH_INTERVAL = 45 * 60  # 45 دقيقة

STOP_FILE = f".recorder/stop/{RECORD_ID}"

WORK_DIR = Path("recording_work")
SEGMENTS_DIR = WORK_DIR / "segments"
OUTPUT_DIR = WORK_DIR / "output"
PARTS_DIR = OUTPUT_DIR / "parts"

stop_requested = False

# ============================================================
# Logging
# ============================================================

def log(message):
    print(message, flush=True)

# ============================================================
# Signal handling
# ============================================================

def request_stop(signum, frame):
    global stop_requested
    if not stop_requested:
        stop_requested = True
        log(f"[STOP] Received signal {signum}. Stopping recording safely...")

signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)

# ============================================================
# Telegram
# ============================================================

async def telegram_request(method, data=None, file_data=None, file_name=None):
    if not BOT_TOKEN:
        log("[TELEGRAM] BOT_TOKEN is missing.")
        return None

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    timeout = aiohttp.ClientTimeout(total=180, connect=20, sock_read=150)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if file_data is not None:
                form = aiohttp.FormData()
                for key, value in (data or {}).items():
                    form.add_field(key, str(value))
                form.add_field(
                    "video",
                    file_data,
                    filename=file_name or f"{RECORD_ID}.mp4",
                    content_type="video/mp4",
                )
                async with session.post(url, data=form) as response:
                    text = await response.text()
                    log(f"[TELEGRAM] {method}: HTTP {response.status}")
                    if response.status != 200:
                        log(f"[TELEGRAM] Response: {text[:3000]}")
                    return response.status, text

            async with session.post(url, json=data or {}) as response:
                text = await response.text()
                log(f"[TELEGRAM] {method}: HTTP {response.status}")
                if response.status != 200:
                    log(f"[TELEGRAM] Response: {text[:3000]}")
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

async def github_update_file(path, content_dict, message):
    if not GITHUB_TOKEN:
        return
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "telegram-hls-recorder",
    }
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            sha = None
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    sha = data.get("sha")
            
            encoded = base64.b64encode(json.dumps(content_dict, indent=2).encode("utf-8")).decode("utf-8")
            body = {"message": message, "content": encoded}
            if sha:
                body["sha"] = sha
                
            async with session.put(url, headers=headers, json=body) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    log(f"[GITHUB] Update failed for {path}: HTTP {resp.status} - {text[:500]}")
                else:
                    log(f"[GITHUB] Updated {path}")
    except Exception as exc:
        log(f"[GITHUB] Exception updating {path}: {exc}")

async def github_delete_file(path, message):
    if not GITHUB_TOKEN:
        return
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "telegram-hls-recorder",
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
                    log(f"[GITHUB] Deleted {path}")
    except Exception as exc:
        log(f"[GITHUB] Exception deleting {path}: {exc}")

# ============================================================
# GitHub stop signal
# ============================================================

async def github_stop_requested():
    if not GITHUB_TOKEN or not RECORD_ID:
        return False

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{STOP_FILE}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "telegram-hls-recorder",
    }
    timeout = aiohttp.ClientTimeout(total=10, connect=5, sock_read=8)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 404:
                    return False
                if response.status != 200:
                    # لا نحتاج لتسجيل هذا بعد الآن (تقليل الـ noise)
                    return False

                data = await response.json()
                encoded = data.get("content")
                if not encoded:
                    return False

                try:
                    raw = base64.b64decode(encoded.replace("\n", "")).decode("utf-8", errors="replace")
                except Exception as exc:
                    log(f"[STOP] Base64 decode failed: {exc}")
                    return False

                raw_stripped = raw.strip()

                try:
                    obj = json.loads(raw_stripped)
                    record_matches = str(obj.get("record_id", "")) == str(RECORD_ID)
                    stop_value = obj.get("stop")
                    if record_matches and stop_value is True:
                        log("[STOP] Valid JSON stop signal detected.")
                        return True
                except Exception:
                    pass

                lower = raw_stripped.lower()
                record_matches = str(RECORD_ID).lower() in lower
                stop_matches = "stop" in lower or "true" in lower or lower == "1"

                if record_matches and stop_matches:
                    log("[STOP] Valid text stop signal detected.")
                    return True

                if raw_stripped:
                    log(f"[STOP] Stop file exists for {RECORD_ID}; treating it as a stop signal.")
                    return True

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log(f"[STOP] GitHub check failed: {exc}")

    return False

async def check_stop():
    global stop_requested
    if stop_requested:
        return True
    if await github_stop_requested():
        stop_requested = True
        log("[STOP] Cooperative stop signal detected.")
        return True
    return False

# ============================================================
# Helpers
# ============================================================

def normalize_url(url):
    if not url: return None
    return url.strip()

def is_m3u8(url):
    if not url: return False
    return ".m3u8" in url.lower()

def safe_filename(value):
    if not value: return "unknown"
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return value[:180]

def origin_from_url(url):
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc: return ""
    return f"{parsed.scheme}://{parsed.netloc}"

# ============================================================
# BASE_NAME defined AFTER safe_filename
# ============================================================

BASE_NAME = safe_filename(TARGET_USERNAME) if TARGET_USERNAME else safe_filename(RECORD_ID)

# ============================================================
# Browser session headers
# ============================================================

async def build_browser_session(page, context):
    user_agent = await page.evaluate("() => navigator.userAgent")
    cookies = await context.cookies()
    cookie_header = "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)
    origin = origin_from_url(PAGE_URL)

    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": PAGE_URL,
    }
    if origin: headers["Origin"] = origin
    if cookie_header: headers["Cookie"] = cookie_header

    log(f"[SESSION] Browser cookies: {len(cookies)}")
    return headers

# ============================================================
# HLS discovery
# ============================================================

async def inspect_page_for_hls(page, discovered):
    try:
        html = await page.content()
        found = re.findall(r'https?://[^"\']+?\.m3u8[^"\']*', html, flags=re.IGNORECASE)
        for url in found:
            url = url.replace("&amp;", "&")
            if url not in discovered:
                discovered.append(url)
                log(f"[HLS] Discovered from HTML: {url}")
    except Exception as exc:
        log(f"[HLS] HTML inspection failed: {exc}")

    try:
        performance_urls = await page.evaluate("""
            () => performance.getEntriesByType('resource').map(e => e.name).filter(n => n.includes('.m3u8'))
        """)
        for url in performance_urls:
            if url not in discovered:
                discovered.append(url)
                log(f"[HLS] Discovered from performance: {url}")
    except Exception as exc:
        log(f"[HLS] Performance inspection failed: {exc}")

# ============================================================
# Playlist parsing
# ============================================================

def parse_playlist(text, playlist_url):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines: return {"type": "empty", "variants": [], "segments": []}

    variants = []
    segments = []
    is_master = False

    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            is_master = True
            bandwidth = 0
            resolution = ""
            match = re.search(r"BANDWIDTH=(\d+)", line, flags=re.IGNORECASE)
            if match: bandwidth = int(match.group(1))
            match = re.search(r"RESOLUTION=([0-9]+x[0-9]+)", line, flags=re.IGNORECASE)
            if match: resolution = match.group(1)

            if index + 1 < len(lines):
                next_line = lines[index + 1]
                if not next_line.startswith("#"):
                    variant_url = urljoin(playlist_url, next_line)
                    variants.append({"url": variant_url, "bandwidth": bandwidth, "resolution": resolution})

        elif line.startswith("#EXTINF:"):
            if index + 1 < len(lines):
                segment_url = lines[index + 1]
                if not segment_url.startswith("#"):
                    segments.append(urljoin(playlist_url, segment_url))

    if is_master: return {"type": "master", "variants": variants, "segments": []}
    return {"type": "media", "variants": [], "segments": segments}

# ============================================================
# Browser HLS request
# ============================================================

async def browser_get_playlist(context, url):
    log(f"[PLAYLIST] Browser GET: {url}")
    try:
        request = await context.request.get(
            url,
            headers={"Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*", "Referer": PAGE_URL},
            timeout=PLAYLIST_TIMEOUT * 1000,
        )
        status = request.status
        log(f"[PLAYLIST] HTTP {status}: {url}")
        if status < 200 or status >= 300: return None, status
        text = await request.text()
        log(f"[PLAYLIST] Received {len(text)} bytes")
        return text, status
    except Exception as exc:
        log(f"[PLAYLIST] Browser request failed: {exc}")
        return None, None

# ============================================================
# Find live media playlist (with quality logging)
# ============================================================

async def find_live_playlist(context, discovered):
    checked = set()
    candidates = list(discovered)

    for url in candidates:
        if await check_stop(): return None, None
        if url in checked: continue
        checked.add(url)

        text, status = await browser_get_playlist(context, url)
        if not text: continue

        parsed = parse_playlist(text, url)
        if parsed["type"] == "media" and parsed["segments"]:
            log(f"[HLS] Usable media playlist found: {url}")
            return url, parsed

        if parsed["type"] == "master":
            variants = parsed["variants"]
            # ✅ Sort by bandwidth descending (highest quality first)
            variants.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
            
            # ✅ NEW: Log all available qualities
            log(f"[HLS] Master playlist contains {len(variants)} variants:")
            for i, v in enumerate(variants, 1):
                resolution = v.get("resolution", "unknown")
                bandwidth = v.get("bandwidth", 0)
                log(f"  [{i}] Resolution: {resolution} | Bandwidth: {bandwidth} bps")
            
            if variants:
                log(f"[HLS] 🎯 Selected highest quality: {variants[0].get('resolution', 'unknown')}")

            for variant in variants:
                if await check_stop(): return None, None
                variant_url = variant["url"]
                if variant_url in checked: continue
                checked.add(variant_url)
                log(f"[HLS] Trying variant: {variant_url}")

                variant_text, _ = await browser_get_playlist(context, variant_url)
                if not variant_text: continue

                variant_parsed = parse_playlist(variant_text, variant_url)
                if variant_parsed["type"] == "media" and variant_parsed["segments"]:
                    log(f"[HLS] Usable variant found: {variant_url}")
                    return variant_url, variant_parsed

    return None, None

# ============================================================
# Segment downloader
# ============================================================

async def download_segment(session, url, path, headers):
    try:
        timeout = aiohttp.ClientTimeout(total=SEGMENT_TIMEOUT)
        async with session.get(url, headers=headers, timeout=timeout) as response:
            if response.status != 200:
                log(f"[SEGMENT] HTTP {response.status}: {url}")
                return False, response.status
            data = await response.read()
            if not data: return False, response.status
            path.write_bytes(data)
            return True, 200
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log(f"[SEGMENT] Download failed: {exc}")
        return False, None

# ============================================================
# Recording loop (مع تحسين Session Refresh)
# ============================================================

async def record_hls(playwright, context, page, playlist_url, initial_playlist, headers):
    """
    ✅ تحسين #3: Session Refresh كل 45 دقيقة لمنع Memory Leak
    """
    global stop_requested
    log("[RECORDER] Starting HLS recording...")
    SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)

    downloaded = set()
    segment_files = []
    initial_segments = initial_playlist.get("segments", [])
    log(f"[RECORDER] Initial segments: {len(initial_segments)}")

    timeout = aiohttp.ClientTimeout(total=SEGMENT_TIMEOUT)
    connector = aiohttp.TCPConnector(limit=10, ssl=False)
    
    consecutive_playlist_errors = 0
    consecutive_segment_errors = 0
    last_status_update = 0
    last_session_refresh = time.time()  # ✅ جديد: تتبع وقت آخر refresh
    recording_start_time = time.time()
    start_time_iso = datetime.now(timezone.utc).isoformat()

    await github_update_file(
        f".recorder/active/{RECORD_ID}.json",
        {"record_id": RECORD_ID, "url": PAGE_URL, "username": TARGET_USERNAME, "started_at": start_time_iso},
        f"Start recording {RECORD_ID}"
    )

    # ✅ جديد: متغير للتحكم في فحص الإيقاف كل 30 ثانية
    stop_check_counter = 0

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        while not stop_requested:
            stop_check_counter += 1
            
            # ✅ تحسين #1: فحص الإيقاف كل 10 loops × 3s = 30 ثانية
            if stop_check_counter % 10 == 0:
                if await check_stop(): 
                    break
            
            if time.time() - last_status_update > STATUS_UPDATE_INTERVAL:
                last_status_update = time.time()
                duration = int(time.time() - recording_start_time)
                size_bytes = sum(p.stat().st_size for p in segment_files if p.exists())
                size_mb = size_bytes / 1024 / 1024
                await github_update_file(
                    f".recorder/status/{RECORD_ID}.json",
                    {"record_id": RECORD_ID, "url": PAGE_URL, "username": TARGET_USERNAME, "duration_seconds": duration, "segments_count": len(segment_files), "size_mb": round(size_mb, 2), "hls_status": "recording"},
                    f"Update status {RECORD_ID}"
                )
                try:
                    video_missing = await page.evaluate("() => !document.querySelector('video')")
                    if video_missing: log("[RECORDER] Video element missing from page. Possible end/premium.")
                except Exception: pass

            # ✅ تحسين #3: إعادة فتح الـ session كل 45 دقيقة
            if time.time() - last_session_refresh > SESSION_REFRESH_INTERVAL:
                log(f"[SESSION] 🔄 Refreshing browser session ({SESSION_REFRESH_INTERVAL // 60}min passed)...")
                try:
                    # إغلاق المتصفح القديم
                    try:
                        await context.close()
                    except Exception:
                        pass
                    
                    # فتح context جديد
                    context = await playwright.chromium.new_context(
                        viewport={"width": 1280, "height": 720},
                        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    )
                    page = await context.new_page()
                    
                    # إعادة ربط الـ response handler
                    async def handle_response(response):
                        # لا نحتاج لعمل شيء هنا، فقط للحفاظ على الاتصال
                        pass
                    page.on("response", handle_response)
                    
                    # إعادة فتح الصفحة
                    try:
                        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
                        last_session_refresh = time.time()
                        log("[SESSION] ✅ Session refreshed successfully")
                    except Exception as goto_exc:
                        log(f"[SESSION] ⚠️ Could not reload page: {goto_exc}")
                        # الاستمرار بالـ session الجديد حتى لو فشل إعادة التحميل
                        
                except Exception as exc:
                    log(f"[SESSION] ⚠️ Refresh failed: {exc}")
                    # الاستمرار بالـ session القديم كـ fallback

            try:
                current_headers = await build_browser_session(page, context)
                headers.update(current_headers)
            except Exception as exc:
                log(f"[SESSION] Could not refresh headers: {exc}")

            playlist_text, status = await browser_get_playlist(context, playlist_url)

            if not playlist_text:
                consecutive_playlist_errors += 1
                log(f"[RECORDER] Playlist request failed ({consecutive_playlist_errors}/{MAX_PLAYLIST_RETRIES})...")
                if consecutive_playlist_errors >= MAX_PLAYLIST_RETRIES:
                    log("[RECORDER] Max playlist retries reached. Stopping.")
                    await send_message(f"⚠️ التسجيل #{RECORD_ID} توقف.\n\nالسبب:\nانتهت محاولات إعادة الاتصال بـ HLS.")
                    break
                await asyncio.sleep(PLAYLIST_POLL_INTERVAL)
                continue

            consecutive_playlist_errors = 0
            parsed = parse_playlist(playlist_text, playlist_url)

            if parsed["type"] == "master":
                variants = parsed["variants"]
                variants.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
                switched = False
                for variant in variants:
                    if await check_stop(): break
                    variant_text, _ = await browser_get_playlist(context, variant["url"])
                    if not variant_text: continue
                    variant_parsed = parse_playlist(variant_text, variant["url"])
                    if variant_parsed["type"] == "media" and variant_parsed["segments"]:
                        playlist_url = variant["url"]
                        parsed = variant_parsed
                        switched = True
                        break
                if stop_requested: break
                if not switched:
                    await asyncio.sleep(PLAYLIST_POLL_INTERVAL)
                    continue

            segments = parsed.get("segments", [])
            new_segments = [url for url in segments if url not in downloaded]
            if new_segments: log(f"[RECORDER] New segments: {len(new_segments)}")

            for segment_url in new_segments:
                if await check_stop(): break

                segment_number = len(segment_files)
                filename = f"{segment_number:08d}.ts"
                path = SEGMENTS_DIR / filename

                ok = False
                seg_status = None
                for attempt in range(MAX_SEGMENT_RETRIES):
                    ok, seg_status = await download_segment(session, segment_url, path, headers)
                    if ok: break
                    await asyncio.sleep(2)

                if ok:
                    consecutive_segment_errors = 0
                    downloaded.add(segment_url)
                    segment_files.append(path)
                    log(f"[SEGMENT] Saved {filename} ({path.stat().st_size} bytes)")
                else:
                    consecutive_segment_errors += 1
                    log(f"[SEGMENT] Failed after retries: {segment_url} (HTTP {seg_status})")
                    if seg_status in (402, 403, 404, 410):
                        log("[RECORDER] Premium/Ended detected via HTTP status.")
                        await send_message(f"⚠️ التسجيل #{RECORD_ID} توقف.\n\nالسبب:\nتحول البث إلى خاص (Premium) أو انتهى.")
                        stop_requested = True
                        break
                    if consecutive_segment_errors >= 5:
                        log("[RECORDER] Too many consecutive segment errors. Stopping.")
                        await send_message(f"⚠️ التسجيل #{RECORD_ID} توقف.\n\nالسبب:\nفشل متكرر في تحميل المقاطع.")
                        break

            if await check_stop(): break
            await asyncio.sleep(PLAYLIST_POLL_INTERVAL)

    log(f"[RECORDER] Recording stopped. Segments saved: {len(segment_files)}")
    return segment_files, start_time_iso, recording_start_time

# ============================================================
# MP4 muxing (مع تحسين FFmpeg للبث الطويل)
# ============================================================

def create_concat_file(segment_files, filename="segments.txt"):
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    concat_file = WORK_DIR / filename
    with concat_file.open("w", encoding="utf-8") as file:
        for path in segment_files:
            absolute_path = path.resolve()
            escaped = str(absolute_path).replace("'", "'\\''")
            file.write(f"file '{escaped}'\n")
    return concat_file

def mux_segments(segment_files):
    if not segment_files:
        log("[MUX] No recorded segments.")
        return None

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    concat_file = create_concat_file(segment_files)
    output_file = OUTPUT_DIR / f"{BASE_NAME}.mp4"

    log(f"[MUX] Creating MP4: {output_file}")
    
    # ✅ تحسين #2: FFmpeg flags للبثوث الطويلة
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-fflags", "+discardcorrupt+genpts",  # ✅ جديد: إصلاح timestamps التالفة
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-c", "copy",
        "-movflags", "+faststart",
        "-avoid_negative_ts", "make_zero",  # ✅ جديد: منع timestamps سالبة
        str(output_file),
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            log("[MUX] FFmpeg failed:")
            log(result.stderr[-5000:])
            return None
        if not output_file.exists():
            log("[MUX] Output file was not created.")
            return None

        size_mb = output_file.stat().st_size / 1024 / 1024
        log(f"[MUX] MP4 ready: {size_mb:.2f} MB")
        return output_file
    except Exception as exc:
        log(f"[MUX] Exception: {exc}")
        return None

# ============================================================
# Split large MP4 into valid MP4 video parts
# ============================================================

def mux_part(segment_files, part_number):
    if not segment_files: return None
    PARTS_DIR.mkdir(parents=True, exist_ok=True)
    part_file = PARTS_DIR / f"{BASE_NAME}_part_{part_number:03d}.mp4"
    concat_name = f"part_{part_number:03d}.txt"
    concat_file = create_concat_file(segment_files, concat_name)

    # ✅ تحسين #2: نفس الـ flags للبثوث الطويلة
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-fflags", "+discardcorrupt+genpts",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-c", "copy",
        "-movflags", "+faststart",
        "-avoid_negative_ts", "make_zero",
        str(part_file),
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            log(f"[PART] FFmpeg failed for Part {part_number}:")
            log(result.stderr[-3000:])
            try: part_file.unlink(missing_ok=True)
            except Exception: pass
            return None

        if not part_file.exists():
            log(f"[PART] FFmpeg completed but output file does not exist: {part_file}")
            return None

        size = part_file.stat().st_size
        if size <= 0:
            log(f"[PART] Output file is empty: {part_file}")
            try: part_file.unlink(missing_ok=True)
            except Exception: pass
            return None
        return part_file
    except Exception as exc:
        log(f"[PART] Exception while creating Part {part_number}: {exc}")
        try: part_file.unlink(missing_ok=True)
        except Exception: pass
        return None

def split_recording_into_parts(segment_files):
    if not segment_files: return []
    valid_segments = [path for path in segment_files if path.exists() and path.is_file() and path.stat().st_size > 0]
    if not valid_segments:
        log("[PARTS] No valid segment files.")
        return []

    total_bytes = sum(path.stat().st_size for path in valid_segments)
    total_mb = total_bytes / 1024 / 1024
    log(f"[PARTS] Recorded TS data: {total_mb:.2f} MB")

    if total_mb < TELEGRAM_MAX_MB:
        log("[PARTS] Recording is small enough for one Telegram video.")
        return []

    log("[PARTS] Large recording detected.")
    log(f"[PARTS] Target per part: {PART_TARGET_MB} MB")
    PARTS_DIR.mkdir(parents=True, exist_ok=True)

    for old_file in PARTS_DIR.glob("*.mp4"):
        try: old_file.unlink()
        except Exception as exc: log(f"[PARTS] Could not remove old part {old_file}: {exc}")

    ESTIMATE_TARGET_BYTES = int(PART_TARGET_BYTES * 0.90)
    groups = []
    current_group = []
    current_bytes = 0

    for segment in valid_segments:
        segment_size = segment.stat().st_size
        if current_group and current_bytes + segment_size > ESTIMATE_TARGET_BYTES:
            groups.append(current_group)
            current_group = []
            current_bytes = 0
        current_group.append(segment)
        current_bytes += segment_size

    if current_group: groups.append(current_group)
    log(f"[PARTS] Initial estimated groups: {len(groups)}")

    verified_groups = []

    def verify_group(group, part_number_hint):
        if not group: return
        if len(group) == 1:
            part_file = mux_part(group, part_number_hint)
            if part_file is None: raise RuntimeError("Failed to mux a single HLS segment.")
            size = part_file.stat().st_size
            size_mb = size / 1024 / 1024
            log(f"[PARTS] Single-segment Part {part_number_hint}: {size_mb:.2f} MB")
            if size < TELEGRAM_MAX_MB:
                verified_groups.append((part_file, size))
                return
            raise RuntimeError(f"A single HLS segment produced a {size_mb:.2f} MB MP4, which is too large for Telegram.")

        part_file = mux_part(group, part_number_hint)
        if part_file is None: raise RuntimeError(f"Failed to mux Part {part_number_hint}.")
        size = part_file.stat().st_size
        size_mb = size / 1024 / 1024
        log(f"[PARTS] Verified candidate Part {part_number_hint}: {size_mb:.2f} MB ({len(group)} segments)")

        if size < PART_TARGET_BYTES:
            verified_groups.append((part_file, size))
            return

        try: part_file.unlink(missing_ok=True)
        except Exception: pass

        middle = len(group) // 2
        left = group[:middle]
        right = group[middle:]
        log(f"[PARTS] Part {part_number_hint} is too large ({size_mb:.2f} MB). Splitting {len(group)} segments into {len(left)} + {len(right)}.")
        verify_group(left, part_number_hint)
        next_hint = len(verified_groups) + 1
        verify_group(right, next_hint)

    try:
        for group in groups:
            if stop_requested: log("[PARTS] Stop flag detected during part generation.")
            hint = len(verified_groups) + 1
            verify_group(group, hint)
    except Exception as exc:
        log(f"[PARTS] Fatal error while creating parts: {exc}")
        for part_file, _ in verified_groups:
            try: part_file.unlink(missing_ok=True)
            except Exception: pass
        return []

    final_parts = []
    for index, (old_file, size) in enumerate(verified_groups, start=1):
        final_name = PARTS_DIR / f"{BASE_NAME}_part_{index:03d}.mp4"
        try:
            if old_file != final_name:
                if final_name.exists(): final_name.unlink()
                old_file.rename(final_name)
            if not final_name.exists():
                log(f"[PARTS] Final part does not exist after rename: {final_name}")
                return []
            final_size = final_name.stat().st_size / 1024 / 1024
            if final_size >= TELEGRAM_MAX_MB:
                log(f"[PARTS] SAFETY CHECK FAILED: {final_name.name} = {final_size:.2f} MB")
                return []
            final_parts.append(final_name)
            log(f"[PARTS] Final Part {index}: {final_size:.2f} MB")
        except Exception as exc:
            log(f"[PARTS] Failed to finalize Part {index}: {exc}")
            return []

    log(f"[PARTS] Successfully created {len(final_parts)} video part(s).")
    return final_parts

# ============================================================
# Telegram upload
# ============================================================

async def send_video_file(file_path, part_number=None, total_parts=None):
    if not file_path or not file_path.exists():
        log(f"[TELEGRAM] Output file does not exist: {file_path}")
        return False

    size_mb = file_path.stat().st_size / 1024 / 1024
    log(f"[TELEGRAM] Video size: {size_mb:.2f} MB")
    if size_mb >= TELEGRAM_MAX_MB:
        log(f"[TELEGRAM] Refusing to upload a file too close to/over the Telegram limit: {size_mb:.2f} MB")
        return False

    if TARGET_USERNAME:
        caption = f"🎥 تسجيل {TARGET_USERNAME} (#{RECORD_ID})\n"
    else:
        caption = f"🎥 التسجيل #{RECORD_ID}\n"
        
    if part_number is not None:
        caption += f"Part {part_number}"
        if total_parts is not None: caption += f" / {total_parts}"

    MAX_UPLOAD_RETRIES = 3
    for attempt in range(MAX_UPLOAD_RETRIES):
        try:
            log(f"[TELEGRAM] Uploading video (attempt {attempt+1}): {file_path.name}")
            with file_path.open("rb") as file:
                result = await telegram_request(
                    "sendVideo",
                    data={"chat_id": ADMIN_USER_ID, "caption": caption, "supports_streaming": "true"},
                    file_data=file,
                    file_name=file_path.name,
                )
            if result and result[0] == 200:
                log(f"[TELEGRAM] Video uploaded successfully: {file_path.name}")
                return True
            log(f"[TELEGRAM] Video upload failed (attempt {attempt+1}): {file_path.name}")
        except Exception as exc:
            log(f"[TELEGRAM] Upload failed: {exc}")
        if attempt < MAX_UPLOAD_RETRIES - 1:
            wait_time = 5 * (attempt + 1)
            log(f"[TELEGRAM] Retrying in {wait_time} seconds...")
            await asyncio.sleep(wait_time)
    return False

async def send_recording(output_file, segment_files, duration_seconds):
    """
    ✅ تحسين #4: إحصائيات مفصلة في رسالة Telegram النهائية
    """
    if not output_file or not output_file.exists():
        log(f"[TELEGRAM] Final MP4 does not exist: {output_file}")
        return False

    full_size_mb = output_file.stat().st_size / 1024 / 1024
    log(f"[TELEGRAM] Final MP4 size: {full_size_mb:.2f} MB")

    # ✅ جديد: تنسيق المدة بشكل أفضل
    duration_mins = duration_seconds // 60
    duration_hours = duration_mins // 60
    duration_mins_remaining = duration_mins % 60

    if duration_hours > 0:
        duration_str = f"{duration_hours}h {duration_mins_remaining}m"
    else:
        duration_str = f"{duration_mins}m"

    if full_size_mb < TELEGRAM_MAX_MB:
        # ✅ تحسين #4: رسالة مفصلة
        await send_message(
            f"🎬 اكتمل التسجيل #{RECORD_ID}\n"
            f"👤 {TARGET_USERNAME}\n"
            f"⏱️ المدة: {duration_str}\n"
            f"📦 الحجم: {full_size_mb:.1f} MB\n"
            f"🎞️ المقاطع: {len(segment_files)}\n"
            f"🎯 الجودة: 720p (HD)\n"
            f"📤 جارٍ إرسال الفيديو..."
        )
        uploaded = await send_video_file(output_file)
        return uploaded

    # ✅ تحسين #4: رسالة مفصلة للتسجيلات الكبيرة
    await send_message(
        f"🎬 اكتمل التسجيل #{RECORD_ID}\n"
        f"👤 {TARGET_USERNAME}\n"
        f"⏱️ المدة: {duration_str}\n"
        f"📦 الحجم الكامل: {full_size_mb:.1f} MB\n"
        f"🎞️ المقاطع: {len(segment_files)}\n"
        f"🎯 الجودة: 720p (HD)\n"
        f"✂️ الملف كبير، سيتم تقسيمه إلى عدة فيديوهات وإرسالها بالترتيب..."
    )
    parts = split_recording_into_parts(segment_files)
    if not parts:
        await send_message("❌ تعذر تقسيم التسجيل إلى أجزاء قابلة للإرسال.")
        return False

    total_parts = len(parts)
    await send_message(f"📤 سيتم إرسال {total_parts} فيديوهات بالترتيب.")
    all_uploaded = True

    for index, part_file in enumerate(parts, start=1):
        if not part_file.exists():
            log(f"[TELEGRAM] Part is missing: {part_file}")
            all_uploaded = False
            await send_message(f"⚠️ الجزء {index} من {total_parts} غير موجود.")
            continue

        size_mb = part_file.stat().st_size / 1024 / 1024
        log(f"[TELEGRAM] Sending Part {index}/{total_parts}: {size_mb:.2f} MB")
        uploaded = await send_video_file(part_file, part_number=index, total_parts=total_parts)
        if not uploaded:
            all_uploaded = False
            await send_message(f"⚠️ تعذر إرسال Part {index} من {total_parts}.")
            continue

    if all_uploaded:
        await send_message(f"✅ تم إرسال التسجيل #{RECORD_ID} بالكامل في {total_parts} أجزاء.")
    else:
        await send_message(f"⚠️ انتهى إرسال التسجيل #{RECORD_ID}، لكن تعذر إرسال جزء أو أكثر.")
    return all_uploaded

# ============================================================
# Main
# ============================================================

async def main():
    if not PAGE_URL:
        print("Usage: python record_once.py \"https://example.com/stream/...\"")
        return 1

    log("Starting persistent HLS recorder...")
    log(f"Opening stream page: {PAGE_URL}")
    log(f"[SESSION] Record ID: {RECORD_ID}")
    if TARGET_USERNAME:
        log(f"[SESSION] Target Username: {TARGET_USERNAME}")
    log(f"[STOP] Watching GitHub file: {STOP_FILE}")

    discovered = []
    segment_files = []
    start_time_iso = datetime.now(timezone.utc).isoformat()
    recording_start_time = time.time()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        async def handle_response(response):
            try:
                url = response.url
                if is_m3u8(url):
                    if url not in discovered:
                        discovered.append(url)
                        log(f"[HLS] Discovered from network response: {url}")
            except Exception: pass

        page.on("response", handle_response)

        try:
            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            log(f"[PAGE] goto warning: {exc}")

        discovery_started = time.time()
        while time.time() - discovery_started < DISCOVERY_TIMEOUT:
            if await check_stop(): break
            await inspect_page_for_hls(page, discovered)
            if discovered:
                if time.time() - discovery_started > 5: break
            await asyncio.sleep(1)

        await inspect_page_for_hls(page, discovered)

        if await check_stop():
            log("[STOP] Stop requested before recording began.")
            await browser.close()
            await send_message(f"⏹️ تم إيقاف التسجيل #{RECORD_ID} قبل بدء تسجيل المقاطع.")
            await github_delete_file(f".recorder/active/{RECORD_ID}.json", f"Remove active {RECORD_ID}")
            return 0

        if not discovered:
            log("[HLS] No HLS playlist discovered.")
            await send_message("❌ لم يتم اكتشاف رابط HLS للبث.")
            await browser.close()
            await github_delete_file(f".recorder/active/{RECORD_ID}.json", f"Remove active {RECORD_ID}")
            return 1

        log(f"[HLS] Total discovered URLs: {len(discovered)}")
        playlist_url, playlist = await find_live_playlist(context, discovered)

        if not playlist_url or not playlist:
            if await check_stop():
                await browser.close()
                await send_message(f"⏹️ تم إيقاف التسجيل #{RECORD_ID}.")
                await github_delete_file(f".recorder/active/{RECORD_ID}.json", f"Remove active {RECORD_ID}")
                return 0
            log("[HLS] Discovered URLs were not usable media playlists.")
            await send_message("❌ تم اكتشاف HLS لكن تعذر الوصول إلى قائمة المقاطع الخاصة بالبث.")
            await browser.close()
            await github_delete_file(f".recorder/active/{RECORD_ID}.json", f"Remove active {RECORD_ID}")
            return 1

        log(f"[HLS] Recording playlist: {playlist_url}")
        headers = await build_browser_session(page, context)
        
        # ✅ تغيير: تمرير playwright بدلاً من context فقط (للـ session refresh)
        segment_files, start_time_iso, recording_start_time = await record_hls(
            playwright, context, page, playlist_url, playlist, headers
        )

        try: await browser.close()
        except Exception: pass

    log("Stopping recorder and preparing final MP4...")

    if not segment_files:
        log("[MUX] No recorded segments.")
        if stop_requested:
            await send_message(f"⏹️ تم إيقاف التسجيل #{RECORD_ID}، لكن لم يتم تسجيل أي مقطع.")
        else:
            await send_message("❌ لم يتم تسجيل أي جزء من البث.")
        await github_delete_file(f".recorder/active/{RECORD_ID}.json", f"Remove active {RECORD_ID}")
        await github_delete_file(f".recorder/status/{RECORD_ID}.json", f"Remove status {RECORD_ID}")
        return 0

    output_file = mux_segments(segment_files)
    if not output_file:
        await send_message("❌ فشل إنشاء ملف MP4 بعد انتهاء التسجيل.")
        await github_delete_file(f".recorder/active/{RECORD_ID}.json", f"Remove active {RECORD_ID}")
        await github_delete_file(f".recorder/status/{RECORD_ID}.json", f"Remove status {RECORD_ID}")
        return 1

    duration = int(time.time() - recording_start_time)
    
    # ✅ تغيير: تمرير duration إلى send_recording
    uploaded = await send_recording(output_file, segment_files, duration)

    full_size_mb = output_file.stat().st_size / 1024 / 1024 if output_file and output_file.exists() else 0
    parts_count = len(list(PARTS_DIR.glob("*.mp4"))) if PARTS_DIR.exists() else 1

    await github_update_file(
        f".recorder/history/{RECORD_ID}.json",
        {"record_id": RECORD_ID, "url": PAGE_URL, "username": TARGET_USERNAME, "started_at": start_time_iso, "ended_at": datetime.now(timezone.utc).isoformat(), "duration_seconds": duration, "size_mb": round(full_size_mb, 2), "parts_count": parts_count, "status": "success" if uploaded else "failed"},
        f"History {RECORD_ID}"
    )

    await github_delete_file(f".recorder/active/{RECORD_ID}.json", f"Remove active {RECORD_ID}")
    await github_delete_file(f".recorder/status/{RECORD_ID}.json", f"Remove status {RECORD_ID}")

    if uploaded:
        try:
            if WORK_DIR.exists():
                shutil.rmtree(WORK_DIR)
                log("[CLEANUP] Workspace cleaned.")
        except Exception as e:
            log(f"[CLEANUP] Error: {e}")
            
        if stop_requested:
            await send_message(f"⏹️ تم إيقاف التسجيل #{RECORD_ID} وإرسال التسجيل بنجاح.")
        else:
            await send_message(f"✅ انتهى التسجيل #{RECORD_ID} وتم إرسال التسجيل بنجاح.")
    else:
        await send_message(f"⚠️ انتهى التسجيل #{RECORD_ID}، لكن تعذر إرسال التسجيل بالكامل.")
        return 1

    return 0

# ============================================================
# Entrypoint
# ============================================================

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        log("[STOP] Keyboard interrupt.")
        sys.exit(0)
    except Exception as exc:
        log(f"[FATAL] {type(exc).__name__}: {exc}")
        try:
            asyncio.run(send_message("❌ حدث خطأ غير متوقع أثناء التسجيل."))
        except Exception: pass
        sys.exit(1)
