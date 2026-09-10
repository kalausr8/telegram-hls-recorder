import asyncio
import os
import re
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

# إرسال جزء كل 60 ثانية تقريبًا
TARGET_CHUNK_SECONDS = 60

# فحص HLS كل ثانيتين
POLL_SECONDS = 2

# عدد محاولات تحميل segment
SEGMENT_RETRIES = 3

# بعد هذا العدد من 401 نعيد تحميل صفحة البث
MAX_401_BEFORE_RELOAD = 2

# إذا لم نستطع الوصول للمصدر لمدة 5 دقائق
SOURCE_TIMEOUT_SECONDS = 300


def parse_attributes(text):
    result = {}

    pattern = re.compile(
        r'([A-Z0-9-]+)=("([^"]*)"|[^,]*)'
    )

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

            resolution = attrs.get(
                "RESOLUTION",
                ""
            )

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

        if line.startswith(
            "#EXT-X-TARGETDURATION"
        ):
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

            uri = attrs.get(
                "URI",
                ""
            ).strip('"')

            if uri:
                init_map = {
                    "url": urljoin(
                        base_url,
                        uri
                    ),
                    "range": attrs.get(
                        "BYTERANGE"
                    )
                }

        elif line.startswith("#EXTINF"):

            value = line.split(
                ":",
                1
            )[1]

            try:
                pending_duration = float(
                    value.split(",", 1)[0]
                )
            except Exception:
                pending_duration = 0

        elif line.startswith(
            "#EXT-X-BYTERANGE"
        ):

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

    for attempt in range(
        SEGMENT_RETRIES
    ):

        try:

            async with session.get(
                url,
                headers=request_headers,
                timeout=aiohttp.ClientTimeout(
                    total=60
                )
            ) as response:

                if response.status not in (
                    200,
                    206
                ):

                    if response.status == 401:
                        raise PermissionError(
                            "HTTP 401"
                        )

                    raise RuntimeError(
                        f"HTTP {response.status}"
                    )

                data = await response.read()

                return data, response.status

        except Exception as error:

            last_error = error

            print(
                f"Download failed "
                f"(attempt {attempt + 1}): "
                f"{url} -> {error}"
            )

            if attempt + 1 < SEGMENT_RETRIES:
                await asyncio.sleep(1)

    if isinstance(
        last_error,
        PermissionError
    ):
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

            if response.status == 401:
                return None, 401

            if response.status != 200:

                print(
                    f"Playlist HTTP {response.status}"
                )

                return None, response.status

            text = await response.text()

            return text, 200

    except aiohttp.ClientError as error:

        print(
            "Playlist network error:",
            error
        )

        return None, None


async def build_headers(
    page,
    page_url
):
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
    print(
        "Refreshing stream page "
        "to renew HLS session..."
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
                "New HLS manifest discovered:"
            )

            print(
                new_manifest
            )

        return new_manifest

    return current_manifest


async def discover_hls():

    print(
        "Opening stream page..."
    )

    playwright = (
        await async_playwright().start()
    )

    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox"
        ]
    )

    context = await browser.new_context()

    page = await context.new_page()

    discovered = []

    async def response_handler(
        response
    ):
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

    try:

        await page.goto(
            PAGE_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

    except Exception:

        await browser.close()
        await playwright.stop()

        raise

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
    concat_file = (
        output_file.with_suffix(
            ".txt"
        )
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
                "file '"
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

    _, stderr = (
        await process.communicate()
    )

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

    # هامش أمان
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

    bot = Bot(
        BOT_TOKEN
    )

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

            # =============================================
            # الحصول على أول Playlist
            # =============================================

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

            # =============================================
            # إذا كان Master Playlist
            # =============================================

            if parsed["type"] == "master":

                variants = parsed["variants"]

                if not variants:

                    raise RuntimeError(
                        "No HLS variants found"
                    )

                # نختار أعلى جودة متاحة
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

            # =============================================
            # التسجيل المستمر
            # =============================================

            while True:

                try:

                    # تحديث Cookies وheaders من المتصفح
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

                    # -------------------------------------
                    # التعامل مع 401
                    # -------------------------------------

                    if status == 401:

                        consecutive_401 += 1

                        print(
                            f"Playlist returned 401 "
                            f"({consecutive_401})"
                        )

                        # أولًا نبحث عن أحدث live.m3u8
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

                        # إذا استمر 401 نعيد تحميل الصفحة
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

                    if (
                        status != 200
                        or not playlist_text
                    ):

                        print(
                            "Playlist temporarily "
                            "unavailable."
                        )

                        await asyncio.sleep(2)

                        continue

                    consecutive_401 = 0

                    parsed = parse_hls(
                        playlist_text,
                        manifest_url
                    )

                    # -------------------------------------
                    # إذا عاد Master Playlist
                    # -------------------------------------

                    if parsed["type"] != "media":

                        variants = parsed.get(
                            "variants",
                            []
                        )

                        if variants:

                            selected = variants[0]

                            manifest_url = (
                                selected["url"]
                            )

                            print(
                                "Switched to media "
                                "variant:",
                                manifest_url
                            )

                        await asyncio.sleep(1)

                        continue

                    # =====================================
                    # EXT-X-MAP لـ fMP4
                    # =====================================

                    if (
                        parsed["init"]
                        and not init_downloaded
                    ):

                        init_data, init_status = (
                            await download_bytes(
                                session,
                                parsed["init"]["url"],
                                headers,
                                parsed["init"].get(
                                    "range"
                                )
                            )
                        )

                        if init_status == 401:

                            print(
                                "Init segment returned 401."
                            )

                            consecutive_401 += 1

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

                    # =====================================
                    # اكتشاف segments الجديدة
                    # =====================================

                    new_segments = []

                    for segment in parsed["segments"]:

                        segment_url = segment["url"]

                        if (
                            segment_url
                            in known_segments
                        ):
                            continue

                        new_segments.append(
                            segment
                        )

                    # =====================================
                    # تحميل segments
                    # =====================================

                    segment_401 = False

                    for segment in new_segments:

                        data, download_status = (
                            await download_bytes(
                                session,
                                segment["url"],
                                headers,
                                segment.get("range")
                            )
                        )

                        if download_status == 401:

                            print(
                                "Segment returned 401."
                            )

                            segment_401 = True

                            break

                        if not data:

                            print(
                                "Skipping failed segment:",
                                segment["url"]
                            )

                            continue

                        # نضيفه إلى known فقط بعد نجاح التحميل
                        known_segments.add(
                            segment["url"]
                        )

                        segment_index = (
                            total_segments
                        )

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
                            f"Segment "
                            f"{total_segments} "
                            f"+{duration:.2f}s "
                            f"total="
                            f"{total_duration:.1f}s"
                        )

                    # -------------------------------------
                    # إذا حصل 401 أثناء segment
                    # -------------------------------------

                    if segment_401:

                        consecutive_401 += 1

                        print(
                            f"Segment authentication "
                            f"problem ({consecutive_401})"
                        )

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

                    last_successful_poll = (
                        time.time()
                    )

                    # =====================================
                    # إنشاء Chunk عند 60 ثانية
                    # =====================================

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

                        if init_file:

                            mux_files = [
                                init_file,
                                *mux_files
                            ]

                        try:

                            await mux_chunk(
                                mux_files,
                                chunk_file
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

                            # لا نحذف segments
                            # عند فشل الـmux

                    # =====================================
                    # VOD انتهى فعليًا
                    # =====================================

                    if not parsed["live"]:

                        print(
                            "HLS playlist has ENDLIST."
                        )

                        break

                    target = max(
                        1,
                        parsed.get(
                            "target_duration",
                            2
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

                    await asyncio.sleep(3)

                # =========================================
                # إذا انقطع المصدر أكثر من 5 دقائق
                # =========================================

                if (
                    time.time()
                    - last_successful_poll
                    > SOURCE_TIMEOUT_SECONDS
                ):

                    raise RuntimeError(
                        "HLS source unavailable "
                        "for more than 5 minutes"
                    )

        finally:

            # =============================================
            # إرسال الجزء الأخير
            # =============================================

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
                        final_file
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

            # =============================================
            # رسالة النهاية
            # =============================================

            try:

                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=(
                        "✅ انتهى التسجيل.\n"
                        f"⏱ المدة التقريبية: "
                        f"{total_duration / 60:.1f} دقيقة\n"
                        f"📦 الأجزاء: "
                        f"{chunk_number}"
                    )
                )

            except Exception as error:

                print(
                    "Telegram final message error:",
                    error
                )

            # =============================================
            # إغلاق Playwright
            # =============================================

            try:
                await browser.close()
            except Exception:
                pass

            try:
                await playwright.stop()
            except Exception:
                pass

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
