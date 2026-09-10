import asyncio
import base64
import json
import os
import re
import signal
import subprocess
import sys
import time
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

PAGE_URL = sys.argv[1] if len(sys.argv) > 1 else ""

TELEGRAM_MAX_MB = 49
DISCOVERY_TIMEOUT = 30
PLAYLIST_TIMEOUT = 20
SEGMENT_TIMEOUT = 30
POLL_INTERVAL = 2.0

WORK_DIR = Path("recording_work")
SEGMENTS_DIR = WORK_DIR / "segments"
OUTPUT_DIR = WORK_DIR / "output"

STOP_FILE = f".recorder/stop/{RECORD_ID}"

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

async def telegram_request(method, data=None, file_data=None):
    if not BOT_TOKEN:
        return None

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    timeout = aiohttp.ClientTimeout(
        total=120,
        connect=20,
        sock_read=100,
    )

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if file_data is not None:
                form = aiohttp.FormData()

                for key, value in (data or {}).items():
                    form.add_field(key, str(value))

                form.add_field(
                    "video",
                    file_data,
                    filename=f"{RECORD_ID}.mp4",
                    content_type="video/mp4",
                )

                async with session.post(url, data=form) as response:
                    text = await response.text()
                    log(f"[TELEGRAM] {method}: HTTP {response.status}")
                    return response.status, text

            async with session.post(url, json=data or {}) as response:
                text = await response.text()
                log(f"[TELEGRAM] {method}: HTTP {response.status}")
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
# GitHub cooperative stop
# ============================================================

async def github_stop_requested():
    """
    Checks whether Cloudflare Worker created:

        .recorder/stop/{RECORD_ID}

    in the repository.
    """

    if not GITHUB_TOKEN or not RECORD_ID:
        return False

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/{GITHUB_REPO}/contents/{STOP_FILE}"
    )

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "telegram-hls-recorder",
    }

    timeout = aiohttp.ClientTimeout(total=15)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:

                if response.status == 404:
                    return False

                if response.status != 200:
                    log(
                        f"[STOP] GitHub stop check HTTP "
                        f"{response.status}"
                    )
                    return False

                data = await response.json()

                if data.get("content"):
                    try:
                        raw = base64.b64decode(
                            data["content"].replace("\n", "")
                        ).decode("utf-8")

                        obj = json.loads(raw)

                        if (
                            obj.get("record_id") == RECORD_ID
                            and obj.get("stop") is True
                        ):
                            return True

                    except Exception as exc:
                        log(f"[STOP] Could not parse stop file: {exc}")

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
    if not url:
        return None

    return url.strip()


def is_m3u8(url):
    if not url:
        return False

    return ".m3u8" in url.lower()


def safe_filename(value):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return value[:180]


def origin_from_url(url):
    parsed = urlparse(url)

    if not parsed.scheme or not parsed.netloc:
        return ""

    return f"{parsed.scheme}://{parsed.netloc}"


# ============================================================
# Browser session headers
# ============================================================

async def build_browser_session(page, context):
    user_agent = await page.evaluate(
        "() => navigator.userAgent"
    )

    cookies = await context.cookies()

    cookie_header = "; ".join(
        f"{cookie['name']}={cookie['value']}"
        for cookie in cookies
    )

    origin = origin_from_url(PAGE_URL)

    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": PAGE_URL,
    }

    if origin:
        headers["Origin"] = origin

    if cookie_header:
        headers["Cookie"] = cookie_header

    log(
        f"[SESSION] Browser cookies: "
        f"{len(cookies)}"
    )

    return headers


# ============================================================
# HLS discovery
# ============================================================

async def inspect_page_for_hls(page, discovered):
    try:
        html = await page.content()

        found = re.findall(
            r'https?://[^"\']+?\.m3u8[^"\']*',
            html,
            flags=re.IGNORECASE,
        )

        for url in found:
            url = url.replace("&amp;", "&")

            if url not in discovered:
                discovered.append(url)
                log(f"[HLS] Discovered from HTML: {url}")

    except Exception as exc:
        log(f"[HLS] HTML inspection failed: {exc}")

    try:
        performance_urls = await page.evaluate(
            """
            () => performance.getEntriesByType('resource')
                .map(e => e.name)
                .filter(n => n.includes('.m3u8'))
            """
        )

        for url in performance_urls:
            if url not in discovered:
                discovered.append(url)
                log(
                    "[HLS] Discovered from performance: "
                    f"{url}"
                )

    except Exception as exc:
        log(
            "[HLS] Performance inspection failed: "
            f"{exc}"
        )


# ============================================================
# Playlist parsing
# ============================================================

def parse_playlist(text, playlist_url):
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    if not lines:
        return {
            "type": "empty",
            "variants": [],
            "segments": [],
        }

    variants = []
    segments = []

    is_master = False

    for index, line in enumerate(lines):

        if line.startswith("#EXT-X-STREAM-INF"):
            is_master = True

            bandwidth = 0
            resolution = ""

            match = re.search(
                r"BANDWIDTH=(\d+)",
                line,
                flags=re.IGNORECASE,
            )

            if match:
                bandwidth = int(match.group(1))

            match = re.search(
                r"RESOLUTION=([0-9]+x[0-9]+)",
                line,
                flags=re.IGNORECASE,
            )

            if match:
                resolution = match.group(1)

            if index + 1 < len(lines):
                next_line = lines[index + 1]

                if not next_line.startswith("#"):
                    variant_url = urljoin(
                        playlist_url,
                        next_line,
                    )

                    variants.append(
                        {
                            "url": variant_url,
                            "bandwidth": bandwidth,
                            "resolution": resolution,
                        }
                    )

        elif line.startswith("#EXTINF:"):

            if index + 1 < len(lines):
                segment_url = lines[index + 1]

                if not segment_url.startswith("#"):
                    segments.append(
                        urljoin(
                            playlist_url,
                            segment_url,
                        )
                    )

    if is_master:
        return {
            "type": "master",
            "variants": variants,
            "segments": [],
        }

    return {
        "type": "media",
        "variants": [],
        "segments": segments,
    }


# ============================================================
# Browser HLS request
# ============================================================

async def browser_get_playlist(context, url):
    log(f"[PLAYLIST] Browser GET: {url}")

    try:
        request = await context.request.get(
            url,
            headers={
                "Accept": (
                    "application/vnd.apple.mpegurl,"
                    "application/x-mpegURL,"
                    "*/*"
                ),
                "Referer": PAGE_URL,
            },
            timeout=PLAYLIST_TIMEOUT * 1000,
        )

        status = request.status

        log(
            f"[PLAYLIST] HTTP {status}: {url}"
        )

        if status < 200 or status >= 300:
            return None, status

        text = await request.text()

        log(
            f"[PLAYLIST] Received {len(text)} bytes"
        )

        return text, status

    except Exception as exc:
        log(
            f"[PLAYLIST] Browser request failed: "
            f"{exc}"
        )
        return None, None


# ============================================================
# Find live media playlist
# ============================================================

async def find_live_playlist(context, discovered):
    checked = set()

    # First try discovered URLs.
    candidates = list(discovered)

    for url in candidates:

        if await check_stop():
            return None, None

        if url in checked:
            continue

        checked.add(url)

        text, status = await browser_get_playlist(
            context,
            url,
        )

        if not text:
            continue

        parsed = parse_playlist(
            text,
            url,
        )

        if parsed["type"] == "media":
            if parsed["segments"]:
                log(
                    "[HLS] Usable media playlist found:"
                    f" {url}"
                )

                return url, parsed

        elif parsed["type"] == "master":

            variants = parsed["variants"]

            variants.sort(
                key=lambda x: x.get("bandwidth", 0),
                reverse=True,
            )

            log(
                f"[HLS] Master playlist contains "
                f"{len(variants)} variants."
            )

            for variant in variants:

                if await check_stop():
                    return None, None

                variant_url = variant["url"]

                if variant_url in checked:
                    continue

                checked.add(variant_url)

                log(
                    "[HLS] Trying variant: "
                    f"{variant_url}"
                )

                variant_text, variant_status = (
                    await browser_get_playlist(
                        context,
                        variant_url,
                    )
                )

                if not variant_text:
                    continue

                variant_parsed = parse_playlist(
                    variant_text,
                    variant_url,
                )

                if (
                    variant_parsed["type"] == "media"
                    and variant_parsed["segments"]
                ):
                    log(
                        "[HLS] Usable variant found:"
                        f" {variant_url}"
                    )

                    return (
                        variant_url,
                        variant_parsed,
                    )

    return None, None


# ============================================================
# Segment downloader
# ============================================================

async def download_segment(
    session,
    url,
    path,
    headers,
):
    try:
        timeout = aiohttp.ClientTimeout(
            total=SEGMENT_TIMEOUT
        )

        async with session.get(
            url,
            headers=headers,
            timeout=timeout,
        ) as response:

            if response.status != 200:
                log(
                    f"[SEGMENT] HTTP "
                    f"{response.status}: {url}"
                )
                return False

            data = await response.read()

            if not data:
                return False

            path.write_bytes(data)

            return True

    except Exception as exc:
        log(
            f"[SEGMENT] Download failed: "
            f"{exc}"
        )
        return False


# ============================================================
# Recording loop
# ============================================================

async def record_hls(
    context,
    page,
    playlist_url,
    initial_playlist,
    headers,
):
    log("[RECORDER] Starting HLS recording...")

    SEGMENTS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    downloaded = set()
    segment_files = []

    # Existing segments.
    initial_segments = initial_playlist.get(
        "segments",
        [],
    )

    log(
        f"[RECORDER] Initial segments: "
        f"{len(initial_segments)}"
    )

    timeout = aiohttp.ClientTimeout(
        total=SEGMENT_TIMEOUT
    )

    connector = aiohttp.TCPConnector(
        limit=10,
        ssl=False,
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
    ) as session:

        while not stop_requested:

            if await check_stop():
                break

            # Refresh browser session headers.
            try:
                current_headers = await build_browser_session(
                    page,
                    context,
                )

                headers.update(current_headers)

            except Exception:
                pass

            playlist_text, status = (
                await browser_get_playlist(
                    context,
                    playlist_url,
                )
            )

            if not playlist_text:
                log(
                    "[RECORDER] Playlist request failed; "
                    "retrying..."
                )

                await asyncio.sleep(
                    POLL_INTERVAL
                )

                continue

            parsed = parse_playlist(
                playlist_text,
                playlist_url,
            )

            if parsed["type"] == "master":
                variants = parsed["variants"]

                variants.sort(
                    key=lambda x: x.get("bandwidth", 0),
                    reverse=True,
                )

                switched = False

                for variant in variants:
                    variant_text, variant_status = (
                        await browser_get_playlist(
                            context,
                            variant["url"],
                        )
                    )

                    if not variant_text:
                        continue

                    variant_parsed = parse_playlist(
                        variant_text,
                        variant["url"],
                    )

                    if (
                        variant_parsed["type"] == "media"
                        and variant_parsed["segments"]
                    ):
                        playlist_url = variant["url"]
                        parsed = variant_parsed
                        switched = True
                        break

                if not switched:
                    await asyncio.sleep(
                        POLL_INTERVAL
                    )
                    continue

            segments = parsed.get(
                "segments",
                [],
            )

            new_segments = [
                url
                for url in segments
                if url not in downloaded
            ]

            if new_segments:
                log(
                    f"[RECORDER] New segments: "
                    f"{len(new_segments)}"
                )

            for index, segment_url in enumerate(
                new_segments
            ):

                if await check_stop():
                    break

                segment_number = (
                    len(segment_files)
                )

                filename = (
                    f"{segment_number:08d}.ts"
                )

                path = SEGMENTS_DIR / filename

                ok = await download_segment(
                    session,
                    segment_url,
                    path,
                    headers,
                )

                if ok:
                    downloaded.add(
                        segment_url
                    )

                    segment_files.append(
                        path
                    )

                    log(
                        "[SEGMENT] Saved "
                        f"{filename} "
                        f"({path.stat().st_size} bytes)"
                    )

                else:
                    log(
                        "[SEGMENT] Failed: "
                        f"{segment_url}"
                    )

            await asyncio.sleep(
                POLL_INTERVAL
            )

    log(
        f"[RECORDER] Recording stopped. "
        f"Segments saved: {len(segment_files)}"
    )

    return segment_files


# ============================================================
# MP4 muxing
# ============================================================

def create_concat_file(segment_files):
    concat_file = WORK_DIR / "segments.txt"

    with concat_file.open(
        "w",
        encoding="utf-8",
    ) as file:

        for path in segment_files:
            absolute_path = path.resolve()

            escaped = str(
                absolute_path
            ).replace(
                "'",
                "'\\''",
            )

            file.write(
                f"file '{escaped}'\n"
            )

    return concat_file


def mux_segments(segment_files):
    if not segment_files:
        log("[MUX] No recorded segments.")
        return None

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    concat_file = create_concat_file(
        segment_files
    )

    output_file = (
        OUTPUT_DIR
        / f"{safe_filename(RECORD_ID)}.mp4"
    )

    log(
        f"[MUX] Creating MP4: {output_file}"
    )

    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_file),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            log(
                "[MUX] FFmpeg failed:"
            )
            log(result.stderr[-5000:])
            return None

        if not output_file.exists():
            log(
                "[MUX] Output file was not created."
            )
            return None

        size_mb = (
            output_file.stat().st_size
            / 1024
            / 1024
        )

        log(
            f"[MUX] MP4 ready: "
            f"{size_mb:.2f} MB"
        )

        return output_file

    except Exception as exc:
        log(
            f"[MUX] Exception: {exc}"
        )
        return None


# ============================================================
# Telegram upload
# ============================================================

async def send_video(file_path):
    if not file_path:
        return False

    size_mb = (
        file_path.stat().st_size
        / 1024
        / 1024
    )

    log(
        f"[TELEGRAM] Final MP4 size: "
        f"{size_mb:.2f} MB"
    )

    if size_mb > TELEGRAM_MAX_MB:
        await send_message(
            "⚠️ انتهى التسجيل، لكن حجم ملف MP4 "
            f"({size_mb:.1f} MB) أكبر من الحد المسموح."
        )
        return False

    await send_message(
        f"🎬 اكتمل التسجيل #{RECORD_ID}\n"
        f"📦 الحجم: {size_mb:.1f} MB\n"
        f"📤 جارٍ إرسال الملف..."
    )

    try:
        with file_path.open(
            "rb"
        ) as file:

            result = await telegram_request(
                "sendVideo",
                data={
                    "chat_id": ADMIN_USER_ID,
                    "caption": (
                        f"🎥 Recording #{RECORD_ID}"
                    ),
                    "supports_streaming": "true",
                },
                file_data=file,
            )

        if result and result[0] == 200:
            log(
                "[TELEGRAM] Video uploaded successfully."
            )
            return True

    except Exception as exc:
        log(
            f"[TELEGRAM] Upload failed: {exc}"
        )

    return False


# ============================================================
# Main
# ============================================================

async def main():
    if not PAGE_URL:
        print(
            "Usage: python record_once.py "
            "\"https://example.com/stream/...\""
        )
        return 1

    log(
        "Starting persistent HLS recorder..."
    )

    log(
        f"Opening stream page: {PAGE_URL}"
    )

    discovered = []

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

        async def handle_response(response):
            try:
                url = response.url

                if is_m3u8(url):
                    if url not in discovered:
                        discovered.append(url)

                        log(
                            "[HLS] Discovered from network response: "
                            f"{url}"
                        )

            except Exception:
                pass

        # IMPORTANT:
        # Listener is attached BEFORE goto().
        page.on(
            "response",
            handle_response,
        )

        try:
            await page.goto(
                PAGE_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

        except Exception as exc:
            log(
                f"[PAGE] goto warning: {exc}"
            )

        # Give the player time to initialize.
        discovery_started = time.time()

        while (
            time.time() - discovery_started
            < DISCOVERY_TIMEOUT
        ):

            if await check_stop():
                break

            await inspect_page_for_hls(
                page,
                discovered,
            )

            if discovered:
                # Continue briefly so that both master
                # and live playlists can appear.
                if (
                    time.time()
                    - discovery_started
                    > 5
                ):
                    break

            await asyncio.sleep(1)

        await inspect_page_for_hls(
            page,
            discovered,
        )

        if not discovered:
            log(
                "[HLS] No HLS playlist discovered."
            )

            await send_message(
                "❌ لم يتم اكتشاف رابط HLS للبث."
            )

            await browser.close()
            return 1

        log(
            f"[HLS] Total discovered URLs: "
            f"{len(discovered)}"
        )

        # ----------------------------------------------------
        # Find a usable media playlist USING BROWSER SESSION
        # ----------------------------------------------------

        playlist_url, playlist = (
            await find_live_playlist(
                context,
                discovered,
            )
        )

        if not playlist_url or not playlist:
            log(
                "[HLS] Discovered URLs were not usable "
                "media playlists."
            )

            await send_message(
                "❌ تم اكتشاف HLS لكن تعذر الوصول "
                "إلى قائمة المقاطع الخاصة بالبث."
            )

            await browser.close()
            return 1

        log(
            f"[HLS] Recording playlist: "
            f"{playlist_url}"
        )

        # ----------------------------------------------------
        # Build browser session headers
        # ----------------------------------------------------

        headers = await build_browser_session(
            page,
            context,
        )

        # ----------------------------------------------------
        # Record
        # ----------------------------------------------------

        segment_files = await record_hls(
            context,
            page,
            playlist_url,
            playlist,
            headers,
        )

        await browser.close()

    # --------------------------------------------------------
    # Finalize
    # --------------------------------------------------------

    log(
        "Stopping recorder and preparing final MP4..."
    )

    if not segment_files:
        log(
            "[MUX] No recorded segments."
        )

        await send_message(
            "❌ لم يتم تسجيل أي جزء من البث."
        )

        return 0

    output_file = mux_segments(
        segment_files
    )

    if not output_file:
        await send_message(
            "❌ فشل إنشاء ملف MP4 بعد انتهاء التسجيل."
        )

        return 1

    await send_video(
        output_file
    )

    await send_message(
        f"✅ انتهى التسجيل #{RECORD_ID}."
    )

    return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(
            main()
        )

        sys.exit(exit_code)

    except KeyboardInterrupt:
        log(
            "[STOP] Keyboard interrupt."
        )
        sys.exit(0)

    except Exception as exc:
        log(
            f"[FATAL] {type(exc).__name__}: {exc}"
        )

        try:
            asyncio.run(
                send_message(
                    "❌ حدث خطأ غير متوقع أثناء التسجيل."
                )
            )
        except Exception:
            pass

        sys.exit(1)
