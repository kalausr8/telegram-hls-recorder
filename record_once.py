import asyncio
import os
import re
import shutil
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

# نُبقي الأجزاء صغيرة بما يكفي لـ Telegram
TARGET_CHUNK_SECONDS = 60

# نعيد فحص HLS بسرعة حتى لا تضيع segments
POLL_SECONDS = 2

# عدد محاولات تحميل segment الفاشل
SEGMENT_RETRIES = 3


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
    """
    يرجع:
      master -> variants
      media  -> segments + init map + target duration
    """

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

            url = None

            for j in range(i + 1, len(lines)):
                candidate = lines[j]

                if candidate.startswith("#"):
                    continue

                url = urljoin(base_url, candidate)
                break

            if not url:
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
                    attrs.get("AVERAGE-BANDWIDTH", "0")
                )
                or 0
            )

            variants.append({
                "url": url,
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
                    "url": urljoin(base_url, uri),
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

        return f"bytes={offset}-{offset + length - 1}"

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
                    raise RuntimeError(
                        f"HTTP {response.status}"
                    )

                return await response.read()

        except Exception as error:

            print(
                f"Download failed "
                f"(attempt {attempt + 1}): "
                f"{url} -> {error}"
            )

            if attempt + 1 < SEGMENT_RETRIES:
                await asyncio.sleep(2)

    return None


async def discover_hls():
    """
    Playwright يفتح صفحة البث ويأخذ أول m3u8
    يحمّله الـplayer فعليًا.
    """

    async with async_playwright() as playwright:

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

            raise RuntimeError(
                "Could not discover HLS m3u8"
            )

        # نأخذ آخر/أول manifest مكتشف
        manifest_url = discovered[-1]

        user_agent = await page.evaluate(
            "navigator.userAgent"
        )

        cookies = await context.cookies()

        cookie_header = "; ".join(
            f"{cookie['name']}={cookie['value']}"
            for cookie in cookies
        )

        page_origin = await page.evaluate(
            "() => location.origin"
        )

        await browser.close()

        headers = {
            "User-Agent": user_agent,
            "Referer": PAGE_URL,
            "Origin": page_origin,
            "Accept": "*/*",
        }

        if cookie_header:
            headers["Cookie"] = cookie_header

        return manifest_url, headers


async def fetch_playlist(
    session,
    url,
    headers
):
    async with session.get(
        url,
        headers=headers,
        timeout=aiohttp.ClientTimeout(
            total=30
        )
    ) as response:

        if response.status != 200:
            raise RuntimeError(
                f"Playlist HTTP {response.status}"
            )

        return await response.text()


async def mux_chunk(
    files,
    output_file,
    headers
):
    """
    نجمع segments الموجودة على القرص
    بواسطة FFmpeg.

    هذا لا يجعل FFmpeg مسؤولًا عن Live HLS؛
    هو فقط يقوم بعملية mux بعد جمع segments.
    """

    concat_file = output_file.with_suffix(
        ".txt"
    )

    with open(
        concat_file,
        "w",
        encoding="utf-8"
    ) as file:

        for segment in files:
            file.write(
                "file "
                + "'"
                + str(segment.resolve())
                    .replace("'", "'\\''")
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

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    _, stderr = await process.communicate()

    concat_file.unlink(
        missing_ok=True
    )

    if process.returncode != 0:

        print(
            stderr.decode(
                errors="ignore"
            )[-4000:]
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

    # Telegram Bot API limit safety margin
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
        "Starting HLS segment recorder..."
    )

    manifest_url, headers = (
        await discover_hls()
    )

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

        # -------------------------------------------------
        # تحديد هل الرابط Master أم Media Playlist
        # -------------------------------------------------

        playlist_text = await fetch_playlist(
            session,
            manifest_url,
            headers
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

            playlist_text = await fetch_playlist(
                session,
                manifest_url,
                headers
            )

            parsed = parse_hls(
                playlist_text,
                manifest_url
            )

        if parsed["type"] != "media":
            raise RuntimeError(
                "Could not obtain media playlist"
            )

        # -------------------------------------------------
        # حالة التسجيل
        # -------------------------------------------------

        known_segments = set()

        current_files = []
        current_duration = 0

        chunk_number = 0

        total_duration = 0
        total_segments = 0

        init_downloaded = False
        init_file = None

        last_successful_poll = time.time()

        print(
            "Live HLS recorder started."
        )

        try:

            while True:

                try:

                    playlist_text = (
                        await fetch_playlist(
                            session,
                            manifest_url,
                            headers
                        )
                    )

                    parsed = parse_hls(
                        playlist_text,
                        manifest_url
                    )

                    if parsed["type"] != "media":
                        raise RuntimeError(
                            "Media playlist expected"
                        )

                    # -----------------------------------------
                    # تحميل EXT-X-MAP إذا كان البث fMP4
                    # -----------------------------------------

                    if (
                        parsed["init"]
                        and not init_downloaded
                    ):

                        init_data = (
                            await download_bytes(
                                session,
                                parsed["init"]["url"],
                                headers,
                                parsed["init"].get("range")
                            )
                        )

                        if init_data:

                            init_file = (
                                SEGMENT_DIR
                                / "init.mp4"
                            )

                            init_file.write_bytes(
                                init_data
                            )

                            init_downloaded = True

                            print(
                                "HLS init segment saved:",
                                len(init_data),
                                "bytes"
                            )

                    # -----------------------------------------
                    # اكتشاف segments الجديدة
                    # -----------------------------------------

                    new_segments = []

                    for segment in parsed["segments"]:

                        url = segment["url"]

                        if url in known_segments:
                            continue

                        known_segments.add(url)

                        new_segments.append(
                            segment
                        )

                    # -----------------------------------------
                    # تحميل segments
                    # -----------------------------------------

                    for segment in new_segments:

                        data = await download_bytes(
                            session,
                            segment["url"],
                            headers,
                            segment.get("range")
                        )

                        if not data:
                            print(
                                "Skipping failed segment:",
                                segment["url"]
                            )

                            continue

                        segment_index = total_segments

                        segment_file = (
                            SEGMENT_DIR
                            / f"{segment_index:08d}.seg"
                        )

                        segment_file.write_bytes(
                            data
                        )

                        current_files.append(
                            segment_file
                        )

                        duration = (
                            segment.get(
                                "duration",
                                0
                            )
                            or 0
                        )

                        current_duration += duration
                        total_duration += duration
                        total_segments += 1

                        print(
                            f"Segment {total_segments} "
                            f"+{duration:.2f}s "
                            f"total={total_duration:.1f}s"
                        )

                    last_successful_poll = (
                        time.time()
                    )

                    # -----------------------------------------
                    # إنشاء Chunk عند الوصول للمدة المطلوبة
                    # -----------------------------------------

                    if (
                        current_duration
                        >= TARGET_CHUNK_SECONDS
                        and current_files
                    ):

                        chunk_number += 1

                        chunk_file = (
                            CHUNK_DIR
                            / f"part_{chunk_number:04d}.mp4"
                        )

                        mux_files = list(
                            current_files
                        )

                        # fMP4 يحتاج init في بداية الجزء
                        if init_file:

                            mux_files = [
                                init_file,
                                *mux_files
                            ]

                        try:

                            await mux_chunk(
                                mux_files,
                                chunk_file,
                                headers
                            )

                            sent = await send_chunk(
                                bot,
                                chunk_file,
                                chunk_number
                            )

                            if sent:

                                chunk_file.unlink(
                                    missing_ok=True
                                )

                                # حذف segments التي أصبحت جزءًا
                                for file in current_files:
                                    file.unlink(
                                        missing_ok=True
                                    )

                                current_files.clear()
                                current_duration = 0

                        except Exception as error:

                            print(
                                "Chunk error:",
                                error
                            )

                            # لا نحذف segments عند الفشل
                            # حتى لا نخسر التسجيل

                    # -----------------------------------------
                    # إذا كان VOD وانتهى بـ ENDLIST
                    # -----------------------------------------

                    if not parsed["live"]:

                        print(
                            "HLS playlist has ENDLIST."
                        )

                        break

                    target = max(
                        2,
                        parsed.get(
                            "target_duration",
                            6
                        ) / 2
                    )

                    await asyncio.sleep(
                        min(
                            POLL_SECONDS,
                            target
                        )
                    )

                except Exception as error:

                    print(
                        "Polling error:",
                        error
                    )

                    # لا ننهي التسجيل بسبب خطأ مؤقت
                    await asyncio.sleep(3)

                    # إذا لم نستطع الوصول للمصدر
                    # لمدة طويلة جدًا نخرج
                    if (
                        time.time()
                        - last_successful_poll
                        > 300
                    ):

                        raise RuntimeError(
                            "HLS source unavailable "
                            "for more than 5 minutes"
                        )

        finally:

            # ---------------------------------------------
            # إرسال الجزء الأخير
            # ---------------------------------------------

            if current_files:

                chunk_number += 1

                final_file = (
                    CHUNK_DIR
                    / f"part_{chunk_number:04d}.mp4"
                )

                mux_files = list(
                    current_files
                )

                if init_file:

                    mux_files = [
                        init_file,
                        *mux_files
                    ]

                try:

                    await mux_chunk(
                        mux_files,
                        final_file,
                        headers
                    )

                    sent = await send_chunk(
                        bot,
                        final_file,
                        chunk_number
                    )

                    if sent:
                        final_file.unlink(
                            missing_ok=True
                        )

                except Exception as error:

                    print(
                        "Final chunk error:",
                        error
                    )

            await bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    "✅ انتهى التسجيل.\n"
                    f"⏱ المدة التقريبية: "
                    f"{total_duration / 60:.1f} دقيقة\n"
                    f"📦 الأجزاء: {chunk_number}"
                )
            )

    print(
        "Recorder finished."
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except Exception as error:

        print(
            "FATAL ERROR:",
            error
        )

        raise
