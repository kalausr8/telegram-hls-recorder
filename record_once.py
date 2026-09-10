import asyncio
import os
import sys
import signal
import time
import json
import subprocess
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
from playwright.async_api import async_playwright


# =========================================================
# Configuration
# =========================================================

PAGE_URL = sys.argv[1]

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["ADMIN_USER_ID"]

WORK_DIR = Path("recording_work")
SEGMENTS_DIR = WORK_DIR / "segments"
OUTPUT_DIR = WORK_DIR / "output"

FULL_VIDEO = OUTPUT_DIR / "recording_full.mp4"
CONCAT_FILE = WORK_DIR / "segments.txt"

TELEGRAM_MAX_MB = 49

SEGMENT_TIMEOUT = 60
PAGE_TIMEOUT = 60

MAX_SOURCE_FAILURE_SECONDS = 300

POLL_SECONDS = 1.0


# =========================================================
# Global stop state
# =========================================================

stop_requested = False


def request_stop(signum, frame):
    global stop_requested

    if not stop_requested:
        stop_requested = True
        print(f"[STOP] Received signal {signum}. Stopping recording safely...")
        sys.stdout.flush()


signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)


# =========================================================
# Helpers
# =========================================================

def log(message):
    print(message, flush=True)


def safe_filename(index):
    return SEGMENTS_DIR / f"segment_{index:06d}.ts"


def get_size_mb(path):
    try:
        return path.stat().st_size / 1024 / 1024
    except Exception:
        return 0


# =========================================================
# Telegram
# =========================================================

async def telegram_request(session, method, data=None, timeout=60):
    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )

    try:
        async with session.post(
            url,
            json=data or {},
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as response:

            text = await response.text()

            log(
                f"[TELEGRAM] {method}: "
                f"HTTP {response.status}"
            )

            if response.status >= 400:
                log(text[:500])

            return response.status, text

    except Exception as exc:
        log(f"[TELEGRAM ERROR] {method}: {exc}")
        return 0, ""


async def send_message(session, text):
    await telegram_request(
        session,
        "sendMessage",
        {
            "chat_id": CHAT_ID,
            "text": text
        }
    )


async def send_video(session, path):
    if not path.exists():
        await send_message(
            session,
            "❌ ملف الفيديو غير موجود."
        )
        return False

    size_mb = get_size_mb(path)

    log(
        f"[TELEGRAM] Final video size: "
        f"{size_mb:.2f} MB"
    )

    if size_mb >= TELEGRAM_MAX_MB:
        await send_message(
            session,
            "⚠️ تم إنشاء الفيديو الكامل، "
            f"لكن حجمه {size_mb:.1f} MB.\n\n"
            "الفيديو أكبر من الحد الآمن للإرسال عبر Telegram "
            "في هذا النظام."
        )
        return False

    try:
        url = (
            f"https://api.telegram.org/"
            f"bot{BOT_TOKEN}/sendVideo"
        )

        timeout = aiohttp.ClientTimeout(
            total=900,
            connect=60,
            sock_read=900,
            sock_connect=60
        )

        form = aiohttp.FormData()

        form.add_field(
            "chat_id",
            str(CHAT_ID)
        )

        form.add_field(
            "supports_streaming",
            "true"
        )

        form.add_field(
            "caption",
            "🎥 التسجيل الكامل\n"
            f"📦 {size_mb:.1f} MB"
        )

        with open(path, "rb") as video_file:
            form.add_field(
                "video",
                video_file,
                filename="recording_full.mp4",
                content_type="video/mp4"
            )

            async with session.post(
                url,
                data=form,
                timeout=timeout
            ) as response:

                text = await response.text()

                log(
                    f"[TELEGRAM] sendVideo: "
                    f"HTTP {response.status}"
                )

                if response.status >= 400:
                    log(text[:1000])
                    return False

                return True

    except Exception as exc:
        log(
            f"[TELEGRAM VIDEO ERROR] {exc}"
        )
        return False


# =========================================================
# Browser headers
# =========================================================

async def build_headers(page, url):
    headers = {}

    try:
        ua = await page.evaluate(
            "() => navigator.userAgent"
        )

        if ua:
            headers["User-Agent"] = ua

    except Exception:
        pass

    headers["Referer"] = url

    return headers


# =========================================================
# HLS discovery
# =========================================================

async def discover_hls(page):
    found = []

    def add_url(value):
        if not value:
            return

        value = str(value).strip()

        if not value:
            return

        if ".m3u8" not in value.lower():
            return

        if value not in found:
            found.append(value)

            log(
                f"[HLS] Discovered: {value}"
            )

    async def handle_response(response):
        try:
            url = response.url

            if ".m3u8" in url.lower():
                add_url(url)

        except Exception:
            pass

    page.on(
        "response",
        handle_response
    )

    # -----------------------------------------------------
    # Also inspect page HTML
    # -----------------------------------------------------

    try:
        html = await page.content()

        for part in html.split('"'):
            if ".m3u8" in part.lower():
                add_url(part)

    except Exception:
        pass

    # -----------------------------------------------------
    # Inspect performance entries
    # -----------------------------------------------------

    try:
        entries = await page.evaluate(
            """
            () => performance
              .getEntriesByType("resource")
              .map(x => x.name)
            """
        )

        for entry in entries:
            add_url(entry)

    except Exception:
        pass

    return found


# =========================================================
# Fetch HLS manifest
# =========================================================

async def fetch_text(
    session,
    url,
    headers=None
):
    timeout = aiohttp.ClientTimeout(
        total=SEGMENT_TIMEOUT
    )

    try:
        async with session.get(
            url,
            headers=headers or {},
            timeout=timeout
        ) as response:

            text = await response.text()

            return (
                response.status,
                text,
                dict(response.headers)
            )

    except Exception as exc:
        log(
            f"[HTTP ERROR] {url}: {exc}"
        )

        return 0, "", {}


# =========================================================
# Download segment
# =========================================================

async def download_segment(
    session,
    url,
    output_path,
    headers
):
    timeout = aiohttp.ClientTimeout(
        total=SEGMENT_TIMEOUT
    )

    try:
        async with session.get(
            url,
            headers=headers,
            timeout=timeout
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

            output_path.write_bytes(data)

            return True

    except Exception as exc:
        log(
            f"[SEGMENT ERROR] {exc}"
        )
        return False


# =========================================================
# Parse media playlist
# =========================================================

def parse_playlist(
    playlist,
    playlist_url
):
    segments = []

    lines = [
        line.strip()
        for line in playlist.splitlines()
        if line.strip()
    ]

    current_duration = 0.0

    for line in lines:

        if line.startswith("#EXTINF:"):
            try:
                value = line.split(":", 1)[1]
                value = value.split(",", 1)[0]
                current_duration = float(value)
            except Exception:
                current_duration = 0.0

            continue

        if line.startswith("#"):
            continue

        segment_url = urljoin(
            playlist_url,
            line
        )

        segments.append(
            (
                segment_url,
                current_duration
            )
        )

        current_duration = 0.0

    return segments


# =========================================================
# Select best HLS playlist
# =========================================================

async def find_live_playlist(
    session,
    discovered_urls,
    headers
):
    candidates = []

    for url in discovered_urls:
        if url not in candidates:
            candidates.append(url)

    # Try each discovered playlist
    for url in candidates:

        status, text, _ = await fetch_text(
            session,
            url,
            headers
        )

        if status != 200:
            continue

        if "#EXTM3U" not in text:
            continue

        # Master playlist
        if "#EXT-X-STREAM-INF" in text:

            lines = [
                x.strip()
                for x in text.splitlines()
                if x.strip()
            ]

            for i, line in enumerate(lines):

                if line.startswith(
                    "#EXT-X-STREAM-INF"
                ):
                    if i + 1 < len(lines):

                        child = lines[i + 1]

                        if child.startswith("#"):
                            continue

                        child_url = urljoin(
                            url,
                            child
                        )

                        candidates.append(
                            child_url
                        )

        else:
            return url, text

    # Retry candidates created from master playlists
    for url in candidates:

        status, text, _ = await fetch_text(
            session,
            url,
            headers
        )

        if status != 200:
            continue

        if "#EXTM3U" in text and (
            "#EXTINF:" in text or
            "#EXT-X-TARGETDURATION" in text
        ):
            return url, text

    return None, None


# =========================================================
# Build MP4 from downloaded segments
# =========================================================

def build_full_video(segment_paths):
    if not segment_paths:
        return False

    WORK_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    valid_paths = [
        path
        for path in segment_paths
        if path.exists() and path.stat().st_size > 0
    ]

    if not valid_paths:
        return False

    log(
        f"[MUX] Preparing {len(valid_paths)} segments..."
    )

    with open(
        CONCAT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        for path in valid_paths:
            escaped = str(path.resolve()).replace(
                "'",
                "'\\''"
            )

            f.write(
                f"file '{escaped}'\n"
            )

    if FULL_VIDEO.exists():
        try:
            FULL_VIDEO.unlink()
        except Exception:
            pass

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
        str(CONCAT_FILE),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(FULL_VIDEO)
    ]

    log(
        "[MUX] Running FFmpeg..."
    )

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1800
        )

        if result.returncode != 0:
            log(
                "[MUX ERROR]"
            )

            log(
                result.stderr[-3000:]
            )

            return False

        if not FULL_VIDEO.exists():
            return False

        if FULL_VIDEO.stat().st_size <= 0:
            return False

        log(
            f"[MUX] Complete: "
            f"{get_size_mb(FULL_VIDEO):.2f} MB"
        )

        return True

    except subprocess.TimeoutExpired:
        log(
            "[MUX ERROR] FFmpeg timeout."
        )
        return False

    except Exception as exc:
        log(
            f"[MUX ERROR] {exc}"
        )
        return False


# =========================================================
# Main recorder
# =========================================================

async def main():
    global stop_requested

    WORK_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    SEGMENTS_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    log(
        "Starting persistent HLS recorder..."
    )

    log(
        f"Opening stream page: {PAGE_URL}"
    )

    connector = aiohttp.TCPConnector(
        limit=20,
        ssl=False
    )

    async with aiohttp.ClientSession(
        connector=connector
    ) as session:

        browser = None
        playwright = None

        segment_paths = []
        known_segments = set()

        total_duration = 0.0
        last_success = time.monotonic()

        playlist_url = None

        try:
            # -------------------------------------------------
            # Playwright
            # -------------------------------------------------

            playwright = await async_playwright().start()

            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu"
                ]
            )

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 "
                    "(X11; Linux x86_64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/131.0.0.0 "
                    "Safari/537.36"
                )
            )

            page = await context.new_page()

            await page.goto(
                PAGE_URL,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT * 1000
            )

            await page.wait_for_timeout(5000)

            # -------------------------------------------------
            # Discover HLS
            # -------------------------------------------------

            discovered = await discover_hls(page)

            if not discovered:
                log(
                    "[HLS] No playlist found immediately. "
                    "Waiting for network activity..."
                )

                for _ in range(20):

                    if stop_requested:
                        break

                    await page.wait_for_timeout(
                        1000
                    )

                    discovered = await discover_hls(
                        page
                    )

                    if discovered:
                        break

            if stop_requested:
                log(
                    "[STOP] Stop requested before HLS started."
                )
                return

            headers = await build_headers(
                page,
                PAGE_URL
            )

            playlist_url, playlist =
                await find_live_playlist(
                    session,
                    discovered,
                    headers
                )

            if not playlist_url:
                await send_message(
                    session,
                    "❌ لم يتم العثور على HLS/m3u8 للبث."
                )
                return

            log(
                f"[HLS] Initial manifest: "
                f"{playlist_url}"
            )

            log(
                "Persistent live HLS recorder started."
            )

            # -------------------------------------------------
            # Recording loop
            # -------------------------------------------------

            while not stop_requested:

                status, manifest, _ =
                    await fetch_text(
                        session,
                        playlist_url,
                        headers
                    )

                if status == 401:
                    log(
                        "[HLS] 401 Unauthorized. "
                        "Trying to rediscover playlist..."
                    )

                    discovered = await discover_hls(
                        page
                    )

                    new_url, new_manifest =
                        await find_live_playlist(
                            session,
                            discovered,
                            headers
                        )

                    if new_url:
                        playlist_url = new_url
                        manifest = new_manifest
                        status = 200

                if status != 200 or not manifest:
                    elapsed =
                        time.monotonic() - last_success

                    log(
                        f"[HLS] Manifest unavailable. "
                        f"HTTP={status}, "
                        f"failure={elapsed:.0f}s"
                    )

                    if (
                        elapsed >
                        MAX_SOURCE_FAILURE_SECONDS
                    ):
                        await send_message(
                            session,
                            "❌ توقف مصدر البث لأكثر من 5 دقائق."
                        )
                        break

                    await asyncio.sleep(
                        POLL_SECONDS
                    )
                    continue

                last_success = time.monotonic()

                parsed =
                    parse_playlist(
                        manifest,
                        playlist_url
                    )

                # -------------------------------------------------
                # Download new segments
                # -------------------------------------------------

                for segment_url, duration in parsed:

                    if stop_requested:
                        break

                    if segment_url in known_segments:
                        continue

                    index = (
                        len(segment_paths) + 1
                    )

                    output_path =
                        safe_filename(index)

                    success =
                        await download_segment(
                            session,
                            segment_url,
                            output_path,
                            headers
                        )

                    if not success:
                        continue

                    # Only mark as known after successful download
                    known_segments.add(
                        segment_url
                    )

                    segment_paths.append(
                        output_path
                    )

                    if duration > 0:
                        total_duration += duration

                    log(
                        f"Segment {len(segment_paths)} "
                        f"+{duration:.2f}s "
                        f"total={total_duration:.1f}s"
                    )

                # -------------------------------------------------
                # VOD / ended stream
                # -------------------------------------------------

                if "#EXT-X-ENDLIST" in manifest:
                    log(
                        "[HLS] ENDLIST detected. "
                        "Stream finished."
                    )
                    break

                await asyncio.sleep(
                    POLL_SECONDS
                )

        except asyncio.CancelledError:
            stop_requested = True

            log(
                "[STOP] asyncio cancellation received."
            )

        except Exception as exc:
            log(
                f"[RECORDER ERROR] {type(exc).__name__}: {exc}"
            )

            try:
                await send_message(
                    session,
                    "❌ حدث خطأ أثناء التسجيل:\n"
                    f"{type(exc).__name__}: {exc}"
                )
            except Exception:
                pass

        finally:
            # =================================================
            # IMPORTANT:
            # Always finish the recording before exiting.
            # =================================================

            log(
                "Stopping recorder and preparing final MP4..."
            )

            # -------------------------------------------------
            # Close browser first
            # -------------------------------------------------

            try:
                if browser:
                    await browser.close()
            except Exception as exc:
                log(
                    f"[BROWSER CLOSE] {exc}"
                )

            try:
                if playwright:
                    await playwright.stop()
            except Exception as exc:
                log(
                    f"[PLAYWRIGHT CLOSE] {exc}"
                )

            # -------------------------------------------------
            # Build complete MP4
            # -------------------------------------------------

            if not segment_paths:
                log(
                    "[MUX] No recorded segments."
                )

                try:
                    await send_message(
                        session,
                        "❌ لم يتم تسجيل أي جزء من البث."
                    )
                except Exception:
                    pass

                return

            log(
                f"[MUX] Finalizing "
                f"{len(segment_paths)} segments..."
            )

            success =
                build_full_video(
                    segment_paths
                )

            if not success:
                try:
                    await send_message(
                        session,
                        "❌ فشل إنشاء الفيديو الكامل MP4."
                    )
                except Exception:
                    pass

                return

            # -------------------------------------------------
            # Send complete video
            # -------------------------------------------------

            sent =
                await send_video(
                    session,
                    FULL_VIDEO
                )

            if sent:
                minutes =
                    total_duration / 60

                await send_message(
                    session,
                    "✅ اكتمل التسجيل.\n"
                    f"⏱ المدة: {minutes:.1f} دقيقة\n"
                    "🎥 تم إرسال الفيديو كاملًا."
                )

            log(
                "Recorder finished successfully."
            )


# =========================================================
# Entry point
# =========================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        log(
            "[STOP] Keyboard interrupt."
        )

    except Exception as exc:
        log(
            f"[FATAL] {type(exc).__name__}: {exc}"
        )
