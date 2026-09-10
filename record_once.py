import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
from playwright.async_api import async_playwright
from telegram import Bot


BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["ADMIN_USER_ID"]

PAGE_URL = sys.argv[1]

WORK_DIR = Path("recording_work")
SEGMENT_DIR = WORK_DIR / "segments"
CHUNK_DIR = WORK_DIR / "chunks"

SEGMENT_DIR.mkdir(parents=True, exist_ok=True)
CHUNK_DIR.mkdir(parents=True, exist_ok=True)

# نرسل جزءًا كل 60 ثانية تقريبًا
TARGET_CHUNK_SECONDS = 60

# فحص HLS كل ثانيتين
POLL_SECONDS = 2

# محاولات تحميل segment
SEGMENT_RETRIES = 3

# بعد هذا العدد من أخطاء 401 نحاول إعادة تحميل صفحة البث
MAX_401_BEFORE_RELOAD = 2

# إذا لم نستطع الوصول للمصدر لمدة 5 دقائق
SOURCE_TIMEOUT_SECONDS = 300


def parse_attributes(text):
    result = {}

    pattern = re.compile(r'([A-Z0-9-]+)=("([^"]*)"|[^,]*)')

    for match in pattern.finditer(text):
        key = match.group(1)
        value = match.group(3)

        if value is None:
            value = match.group(2)

        result[key] = value

    return result


def parse_hls(text, base_url):
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    if not lines or not lines[0].startswith("#EXTM3U"):
        raise RuntimeError("Invalid HLS playlist")

    is_master = any(
        line.startswith("#EXT-X-STREAM-INF")
        for line in lines
    )

    if is_master:
        variants = []

        for i, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF"):
                continue

            attrs = parse_attributes(
                line.split(":", 1)[1]
            )

            variant_url = None

            for j in range(i + 1, len(lines)):
                candidate = lines[j]

                if candidate.startswith("#"):
                    continue

                variant_url = urljoin(
                    base_url,
                    candidate
                )
                break

            if not variant_url:
                continue

            resolution = attrs.get("RESOLUTION", "")

            width = None
            height = None

            if "x" in resolution:
                try:
                    width, height = map(
                        int,
                        resolution.split("x", 1)
                    )
                except Exception:
                    pass

            bandwidth = int(
                attrs.get(
                    "BANDWIDTH",
                    attrs.get(
                        "AVERAGE-BANDWIDTH",
                        "0"
                    )
                )
                or 0
            )

            variants.append({
                "url": variant_url,
                "bandwidth": bandwidth,
                "width": width,
                "height": height,
            })

        variants.sort(
            key=lambda x: x["bandwidth"],
            reverse=True
        )

        return {
            "type": "master",
            "variants": variants
        }

    segments = []
    init_map = None
    target_duration = 6
    pending_duration = 0
    pending_range = None

    for line in lines:

        if line.startswith("#EXT-X-TARGETDURATION"):
            try:
                target_duration = int(
                    line.split(":", 1)[1]
                )
            except Exception:
                pass

        elif line.startswith("#EXT-X-MAP"):
            attrs = parse_attributes(
                line.split(":", 1)[1]
            )

            uri = attrs.get("URI", "").strip('"')

            if uri:
                init_map = {
                    "url": urljoin(
                        base_url,
                        uri
                    ),
                    "range": attrs.get("BYTERANGE")
                }

        elif line.startswith("#EXTINF"):
            value = line.split(":", 1)[1]

            try:
                pending_duration = float(
                    value.split(",", 1)[0]
                )
            except Exception:
                pending_duration = 0

        elif line.startswith("#EXT-X-BYTERANGE"):
            pending_range = (
                line.split(":", 1)[1]
                .strip()
            )

        elif not line.startswith("#"):

            segment_url = urljoin(
                base_url,
                line
            )

            segments.append({
                "url": segment_url,
                "duration": pending_duration,
                "range": pending_range
            })

            pending_duration = 0
            pending_range = None

    return {
        "type": "media",
        "segments": segments,
        "init": init_map,
        "target_duration": target_duration,
        "live": "#EXT-X-ENDLIST" not in text
    }


def range_header(value):
    if not value:
        return None

    try:
        parts = value.split("@")

        length = int(parts[0])

        if len(parts) == 2:
            offset = int(parts[1])
        else:
            offset = 0

        return (
            f"bytes={offset}-"
            f"{offset + length - 1}"
        )

    except Exception:
        return None


async def download_bytes(
    session,
    url,
    headers,
    range_value=None
):
    request_headers = dict(headers)

    r = range_header(range_value)

    if r:
        request_headers["Range"] = r

    last_error = None

    for attempt in range(SEGMENT_RETRIES):

        try:
            async with session.get(
                url,
                headers=request_headers,
                timeout=aiohttp.ClientTimeout(
                    total=60
                )
            ) as response:

                if response.status not in (200, 206):

                    if response.status == 401:
                        raise PermissionError(
                            "HTTP 401"
                        )

                    raise RuntimeError(
                        f"HTTP {response.status}"
                    )

                return (
                    await response.read(),
                    response.status
                )

        except Exception as error:

            last_error = error

            print(
                f"Download failed "
                f"(attempt {attempt + 1}): "
                f"{url} -> {error}"
            )

            if attempt + 1 < SEGMENT_RETRIES:
                await asyncio.sleep(1)

    if isinstance(last_error, PermissionError):
        return None, 401

    return None, None


async def fetch_playlist(
    session,
    url,
    headers
):
    try:

        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=30
            )
        ) as response:

            if response.status != 200:

                if response.status == 401:
                    return None, 401

                raise RuntimeError(
                    f"Playlist HTTP {response.status}"
                )

            return (
                await response.text(),
                200
            )

    except aiohttp.ClientError as error:

        print(
            "Playlist network error:",
            error
        )

        return None, None


async def build_headers(page, page_url):
    user_agent = await page.evaluate(
        "() => navigator.userAgent"
    )

    cookies = await page.context.cookies()

    cookie_header = "; ".join(
        f"{cookie['name']}={cookie['value']}"
        for cookie in cookies
    )

    page_origin = await page.evaluate(
        "() => location.origin"
    )

    headers = {
        "User-Agent": user_agent,
        "Referer": page_url,
        "Origin": page_origin,
        "Accept": "*/*",
    }

    if cookie_header:
        headers["Cookie"] = cookie_header

    return headers


async def discover_and_refresh(
    page,
    discovered,
    current_manifest
):
    """
    نعيد تحميل صفحة البث عند الحاجة.
    الصفحة نفسها تعيد طلب HLS جديدًا،
    ونلتقط أحدث manifest من network.
    """

    print(
        "Refreshing stream page to renew HLS session..."
    )

    before_count = len(discovered)

    try:

        await page.reload(
            wait_until="domcontentloaded",
            timeout=60000
        )

    except Exception as error:

        print(
            "Page reload warning:",
            error
        )

    # نعطي player وقتًا لإعادة الاتصال
    for _ in range(20):

        await asyncio.sleep(1)

        if len(discovered) > before_count:
            break

    if discovered:

        # نفضّل آخر رابط live.m3u8
        live_urls = [
            url
            for url in discovered
            if "live.m3u8" in url.lower()
        ]

        if live_urls:
            new_manifest = live_urls[-1]
        else:
            new_manifest = discovered[-1]

        if new_manifest != current_manifest:

            print(
                "New HLS manifest discovered:",
                new_manifest
            )

        return new_manifest

    return current_manifest


async def discover_hls():
    """
    يفتح صفحة البث ويبقي Playwright مفتوحًا.
    يرجع:
      playwright
      browser
      context
      page
      manifest_url
      headers
      discovered
    """

    playwright = await async_playwright().start()

    browser = await playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox"]
    )

    context = await browser.new_context()

    page = await context.new_page()

    discovered = []

    async def response_handler(response):

        url = response.url

        if ".m3u8" not in url.lower():
            return

        if url not in discovered:

            discovered.append(url)

            print(
                "HLS discovered:",
                url
            )

    page.on(
        "response",
        response_handler
    )

    print(
        "Opening stream page..."
    )

    await page.goto(
        PAGE_URL,
        wait_until="domcontentloaded",
        timeout=60000
    )

    for _ in range(30):

        if discovered:
            break

        await asyncio.sleep(1)

    if not discovered:

        await browser.close()
        await playwright.stop()

        raise RuntimeError(
            "Could not discover HLS m3u8"
        )

    live_urls = [
        url
        for url in discovered
        if "live.m3u8" in url.lower()
    ]

    if live_urls:
        manifest_url = live_urls[-1]
    else:
        manifest_url = discovered[-1]

    headers = await build_headers(
        page,
        PAGE_URL
    )

    return (
        playwright,
        browser,
        context,
        page,
        manifest_url,
        headers,
        discovered
    )


async def mux_chunk(
    files,
    output_file
):
    concat_file = output_file.with_suffix(
        ".txt"
    )

    with open(
        concat_file,
        "w",
        encoding="utf-8"
    ) as file:

        for segment in files:

            path = str(
                segment.resolve()
            ).replace(
                "'",
                "'\\''"
            )

            file.write(
                "file "
                + "'"
                + path
                + "'\n"
            )

    command = [
        "ffmpeg",
        "-y",

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

    print(
        "Muxing:",
        output_file
    )

    process = (
        await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
    )

    _, stderr = await process.communicate()

    concat_file.unlink(
        missing_ok=True
    )

    if process.returncode != 0:

        print(
            stderr.decode(
                errors="ignore"
            )[-5000:]
        )

        raise RuntimeError(
            "FFmpeg mux failed"
        )


async def send_chunk(
    bot,
    file_path,
    chunk_number
):
    size_mb = (
        file_path.stat().st_size
        / (1024 * 1024)
    )

    print(
        f"Sending chunk {chunk_number}: "
        f"{size_mb:.1f} MB"
    )

    # هامش أمان تحت حد Telegram
    if size_mb >= 49:

        print(
            "Chunk is too large for Telegram"
        )

        return False

    with open(
        file_path,
        "rb"
    ) as video:

        await bot.send_document(
            chat_id=CHAT_ID,
            document=video,
            caption=(
                f"🎥 جزء التسجيل "
                f"#{chunk_number}\n"
                f"📦 {size_mb:.1f} MB"
            )
        )

    return True


async def main():

    print(
        "Starting persistent HLS recorder..."
    )

    (
        playwright,
        browser,
        context,
        page,
        manifest_url,
        headers,
        discovered
    ) = await discover_hls()

    print(
        "Initial manifest:",
        manifest_url
    )

    bot = Bot(BOT_TOKEN)

    timeout = aiohttp.ClientTimeout(
        total=90
    )

    connector = aiohttp.TCPConnector(
        limit=16
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector
    ) as session:

        known_segments = set()

        current_files = []
        current_duration = 0

        chunk_number = 0

        total_duration = 0
        total_segments = 0

        init_downloaded = False
        init_file = None

        last_successful_poll = time.time()

        consecutive_401 = 0

        print(
            "Persistent live HLS recorder started."
        )

        try:

            # ---------------------------------------------
            # أولًا: إذا كان manifest Master
            # ---------------------------------------------

            playlist_text, status = (
                await fetch_playlist(
                    session,
                    manifest_url,
                    headers
                )
            )

            if status == 401:

                manifest_url = (
                    await discover_and_refresh(
                        page,
                        discovered,
                        manifest_url
                    )
                )

                headers = await build_headers(
                    page,
                    PAGE_URL
                )

                playlist_text, status = (
                    await fetch_playlist(
                        session,
                        manifest_url,
                        headers
                    )
                )

            if not playlist_text:

                raise RuntimeError(
                    "Could not fetch initial HLS playlist"
                )

            parsed = parse_hls(
                playlist_text,
                manifest_url
            )

            if parsed["type"] == "master":

                variants = parsed["variants"]

                if not variants:

                    raise RuntimeError(
                        "No HLS variants found"
                    )

                selected = variants[0]

                manifest_url = selected["url"]

                print(
                    "Selected quality:",
                    selected["height"],
                    "p",
                    selected["bandwidth"],
                    "bps"
                )

                playlist_text, status = (
                    await fetch_playlist(
                        session,
                        manifest_url,
                        headers
                    )
                )

                if status == 401:

                    manifest_url = (
                        await discover_and_refresh(
                            page,
                            discovered,
                            manifest_url
                        )
                    )

                    headers = await build_headers(
                        page,
                        PAGE_URL
                    )

                    playlist_text, status = (
                        await fetch_playlist(
                            session,
                            manifest_url,
                            headers
                        )
                    )

                if not playlist_text:

                    raise RuntimeError(
                        "Could not obtain media playlist"
                    )

                parsed = parse_hls(
                    playlist_text,
                    manifest_url
                )

            if parsed["type"] != "media":

                raise RuntimeError(
                    "Could not obtain media playlist"
                )

            # ---------------------------------------------
            # التسجيل المستمر
            # ---------------------------------------------

            while True:

                try:

                    # تحديث headers من جلسة Playwright
                    # لأن cookies قد تتغير أثناء البث
                    headers = await build_headers(
                        page,
                        PAGE_URL
                    )

                    playlist_text, status = (
                        await fetch_playlist(
                            session,
                            manifest_url,
                            headers
                        )
                    )

                    # -----------------------------------------
                    # 401 = الرابط القديم لم يعد صالحًا
                    # -----------------------------------------

                    if status == 401:

                        consecutive_401 += 1

                        print(
                            f"Playlist returned 401 "
                            f"({consecutive_401})"
                        )

                        # نحاول أولًا استخدام آخر manifest
                        # اكتشفته Playwright
                        live_urls = [
                            url
                            for url in discovered
                            if "live.m3u8"
                            in url.lower()
                        ]

                        if live_urls:

                            candidate = live_urls[-1]

                            if candidate != manifest_url:

                                print(
                                    "Switching to latest "
                                    "discovered HLS URL:"
                                )

                                print(
                                    candidate
                                )

                                manifest_url = candidate

                        # إذا استمر 401، نجدد صفحة البث
                        if (
                            consecutive_401
                            >= MAX_401_BEFORE_RELOAD
                        ):

                            manifest_url = (
                                await discover_and_refresh(
                                    page,
                                    discovered,
                                    manifest_url
                                )
                            )

                            headers = (
                                await build_headers(
                                    page,
                                    PAGE_URL
                                )
                            )

                            consecutive_401 = 0

                        await asyncio.sleep(1)

                        continue

                    if status != 200 or not playlist_text:

                        print(
                         
