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
GITHUB_OWNER = os.environ.get(
    "GITHUB_OWNER",
    "kalausr8",
)
GITHUB_REPO = os.environ.get(
    "GITHUB_REPO",
    "telegram-hls-recorder",
)

RECORD_ID = os.environ.get(
    "RECORD_ID",
    "UNKNOWN",
)

PAGE_URL = (
    sys.argv[1].strip()
    if len(sys.argv) > 1
    else ""
)

# Telegram Bot API has a practical upload limit around 50 MB
# for sendVideo. We intentionally stay below it.
TELEGRAM_MAX_MB = 49

# Use a little extra safety margin so that the final MP4
# remains comfortably below Telegram's limit.
PART_TARGET_MB = 47

PART_TARGET_BYTES = (
    PART_TARGET_MB
    * 1024
    * 1024
)

DISCOVERY_TIMEOUT = 30
PLAYLIST_TIMEOUT = 20
SEGMENT_TIMEOUT = 30

STOP_CHECK_INTERVAL = 2.0
PLAYLIST_POLL_INTERVAL = 2.0

STOP_FILE = (
    f".recorder/stop/{RECORD_ID}"
)

WORK_DIR = Path("recording_work")
SEGMENTS_DIR = WORK_DIR / "segments"
OUTPUT_DIR = WORK_DIR / "output"
PARTS_DIR = OUTPUT_DIR / "parts"

stop_requested = False


# ============================================================
# Logging
# ============================================================

def log(message):
    print(
        message,
        flush=True,
    )


# ============================================================
# Signal handling
# ============================================================

def request_stop(signum, frame):
    global stop_requested

    if not stop_requested:
        stop_requested = True

        log(
            f"[STOP] Received signal {signum}. "
            "Stopping recording safely..."
        )


signal.signal(
    signal.SIGTERM,
    request_stop,
)

signal.signal(
    signal.SIGINT,
    request_stop,
)


# ============================================================
# Telegram
# ============================================================

async def telegram_request(
    method,
    data=None,
    file_data=None,
    file_name=None,
):
    if not BOT_TOKEN:
        log(
            "[TELEGRAM] BOT_TOKEN is missing."
        )
        return None

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )

    timeout = aiohttp.ClientTimeout(
        total=180,
        connect=20,
        sock_read=150,
    )

    try:
        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            if file_data is not None:
                form = aiohttp.FormData()

                for key, value in (
                    data or {}
                ).items():

                    form.add_field(
                        key,
                        str(value),
                    )

                form.add_field(
                    "video",
                    file_data,
                    filename=(
                        file_name
                        or f"{RECORD_ID}.mp4"
                    ),
                    content_type="video/mp4",
                )

                async with session.post(
                    url,
                    data=form,
                ) as response:

                    text = await response.text()

                    log(
                        f"[TELEGRAM] {method}: "
                        f"HTTP {response.status}"
                    )

                    if response.status != 200:
                        log(
                            f"[TELEGRAM] Response: "
                            f"{text[:3000]}"
                        )

                    return (
                        response.status,
                        text,
                    )

            async with session.post(
                url,
                json=data or {},
            ) as response:

                text = await response.text()

                log(
                    f"[TELEGRAM] {method}: "
                    f"HTTP {response.status}"
                )

                if response.status != 200:
                    log(
                        f"[TELEGRAM] Response: "
                        f"{text[:3000]}"
                    )

                return (
                    response.status,
                    text,
                )

    except Exception as exc:
        log(
            f"[TELEGRAM] Error: {exc}"
        )

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
# GitHub stop signal
# ============================================================

async def github_stop_requested():
    """
    Checks:

        .recorder/stop/{RECORD_ID}

    Accepted stop-file formats:

    1. JSON:
       {
         "record_id": "ABC123",
         "stop": true
       }

    2. Tolerant plain text.

    3. Any non-empty record-specific stop file.
    """

    if not GITHUB_TOKEN:
        return False

    if not RECORD_ID:
        return False

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}/"
        f"contents/"
        f"{STOP_FILE}"
    )

    headers = {
        "Authorization": (
            f"Bearer {GITHUB_TOKEN}"
        ),
        "Accept": (
            "application/vnd.github+json"
        ),
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": (
            "telegram-hls-recorder"
        ),
    }

    timeout = aiohttp.ClientTimeout(
        total=10,
        connect=5,
        sock_read=8,
    )

    try:
        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.get(
                url,
                headers=headers,
            ) as response:

                if response.status == 404:
                    return False

                if response.status != 200:
                    log(
                        "[STOP] GitHub stop check "
                        f"HTTP {response.status}"
                    )
                    return False

                data = await response.json()

                encoded = data.get(
                    "content"
                )

                if not encoded:
                    return False

                try:
                    raw = base64.b64decode(
                        encoded.replace(
                            "\n",
                            "",
                        )
                    ).decode(
                        "utf-8",
                        errors="replace",
                    )
                except Exception as exc:
                    log(
                        "[STOP] Base64 decode failed: "
                        f"{exc}"
                    )
                    return False

                raw_stripped = raw.strip()

                # ------------------------------------------------
                # JSON
                # ------------------------------------------------

                try:
                    obj = json.loads(
                        raw_stripped
                    )

                    record_matches = (
                        str(
                            obj.get(
                                "record_id",
                                "",
                            )
                        )
                        == str(RECORD_ID)
                    )

                    stop_value = obj.get(
                        "stop"
                    )

                    if (
                        record_matches
                        and stop_value is True
                    ):
                        log(
                            "[STOP] Valid JSON "
                            "stop signal detected."
                        )
                        return True

                except Exception:
                    pass

                # ------------------------------------------------
                # Plain text
                # ------------------------------------------------

                lower = raw_stripped.lower()

                record_matches = (
                    str(RECORD_ID).lower()
                    in lower
                )

                stop_matches = (
                    "stop" in lower
                    or "true" in lower
                    or lower == "1"
                )

                if (
                    record_matches
                    and stop_matches
                ):
                    log(
                        "[STOP] Valid text "
                        "stop signal detected."
                    )
                    return True

                # ------------------------------------------------
                # Existing non-empty record-specific file
                # ------------------------------------------------

                if raw_stripped:
                    log(
                        "[STOP] Stop file exists for "
                        f"{RECORD_ID}; treating it "
                        "as a stop signal."
                    )
                    return True

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        log(
            "[STOP] GitHub check failed: "
            f"{exc}"
        )

    return False


async def check_stop():
    global stop_requested

    if stop_requested:
        return True

    if await github_stop_requested():
        stop_requested = True

        log(
            "[STOP] Cooperative stop signal "
            "detected."
        )

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

    return (
        ".m3u8"
        in url.lower()
    )


def safe_filename(value):
    value = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        value,
    )

    return value[:180]


def origin_from_url(url):
    parsed = urlparse(url)

    if (
        not parsed.scheme
        or not parsed.netloc
    ):
        return ""

    return (
        f"{parsed.scheme}://"
        f"{parsed.netloc}"
    )


# ============================================================
# Browser session headers
# ============================================================

async def build_browser_session(
    page,
    context,
):
    user_agent = await page.evaluate(
        "() => navigator.userAgent"
    )

    cookies = await context.cookies()

    cookie_header = "; ".join(
        f"{cookie['name']}={cookie['value']}"
        for cookie in cookies
    )

    origin = origin_from_url(
        PAGE_URL
    )

    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": (
            "en-US,en;q=0.9"
        ),
        "Referer": PAGE_URL,
    }

    if origin:
        headers["Origin"] = origin

    if cookie_header:
        headers["Cookie"] = (
            cookie_header
        )

    log(
        "[SESSION] Browser cookies: "
        f"{len(cookies)}"
    )

    return headers


# ============================================================
# HLS discovery
# ============================================================

async def inspect_page_for_hls(
    page,
    discovered,
):
    # --------------------------------------------------------
    # HTML
    # --------------------------------------------------------

    try:
        html = await page.content()

        found = re.findall(
            r'https?://[^"\']+?\.m3u8[^"\']*',
            html,
            flags=re.IGNORECASE,
        )

        for url in found:

            url = url.replace(
                "&amp;",
                "&",
            )

            if url not in discovered:

                discovered.append(
                    url
                )

                log(
                    "[HLS] Discovered "
                    "from HTML: "
                    f"{url}"
                )

    except Exception as exc:
        log(
            "[HLS] HTML inspection "
            f"failed: {exc}"
        )

    # --------------------------------------------------------
    # Performance API
    # --------------------------------------------------------

    try:
        performance_urls = (
            await page.evaluate(
                """
                () => performance
                    .getEntriesByType('resource')
                    .map(e => e.name)
                    .filter(
                        n => n.includes('.m3u8')
                    )
                """
            )
        )

        for url in performance_urls:

            if url not in discovered:

                discovered.append(
                    url
                )

                log(
                    "[HLS] Discovered "
                    "from performance: "
                    f"{url}"
                )

    except Exception as exc:
        log(
            "[HLS] Performance inspection "
            f"failed: {exc}"
        )


# ============================================================
# Playlist parsing
# ============================================================

def parse_playlist(
    text,
    playlist_url,
):
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

    for index, line in enumerate(
        lines
    ):

        # ----------------------------------------------------
        # Master playlist
        # ----------------------------------------------------

        if line.startswith(
            "#EXT-X-STREAM-INF"
        ):

            is_master = True

            bandwidth = 0
            resolution = ""

            match = re.search(
                r"BANDWIDTH=(\d+)",
                line,
                flags=re.IGNORECASE,
            )

            if match:
                bandwidth = int(
                    match.group(1)
                )

            match = re.search(
                r"RESOLUTION=([0-9]+x[0-9]+)",
                line,
                flags=re.IGNORECASE,
            )

            if match:
                resolution = (
                    match.group(1)
                )

            if (
                index + 1
                < len(lines)
            ):

                next_line = (
                    lines[index + 1]
                )

                if not next_line.startswith(
                    "#"
                ):

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

        # ----------------------------------------------------
        # Media playlist
        # ----------------------------------------------------

        elif line.startswith(
            "#EXTINF:"
        ):

            if (
                index + 1
                < len(lines)
            ):

                segment_url = (
                    lines[index + 1]
                )

                if not segment_url.startswith(
                    "#"
                ):

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

async def browser_get_playlist(
    context,
    url,
):
    log(
        f"[PLAYLIST] Browser GET: {url}"
    )

    try:
        request = (
            await context.request.get(
                url,
                headers={
                    "Accept": (
                        "application/vnd.apple."
                        "mpegurl,"
                        "application/x-mpegURL,"
                        "*/*"
                    ),
                    "Referer": PAGE_URL,
                },
                timeout=(
                    PLAYLIST_TIMEOUT * 1000
                ),
            )
        )

        status = request.status

        log(
            "[PLAYLIST] HTTP "
            f"{status}: {url}"
        )

        if (
            status < 200
            or status >= 300
        ):
            return None, status

        text = await request.text()

        log(
            "[PLAYLIST] Received "
            f"{len(text)} bytes"
        )

        return text, status

    except Exception as exc:
        log(
            "[PLAYLIST] Browser request "
            f"failed: {exc}"
        )

        return None, None


# ============================================================
# Find live media playlist
# ============================================================

async def find_live_playlist(
    context,
    discovered,
):
    checked = set()

    candidates = list(
        discovered
    )

    for url in candidates:

        if await check_stop():
            return None, None

        if url in checked:
            continue

        checked.add(url)

        text, status = (
            await browser_get_playlist(
                context,
                url,
            )
        )

        if not text:
            continue

        parsed = parse_playlist(
            text,
            url,
        )

        # ----------------------------------------------------
        # Direct media playlist
        # ----------------------------------------------------

        if (
            parsed["type"] == "media"
            and parsed["segments"]
        ):

            log(
                "[HLS] Usable media "
                "playlist found: "
                f"{url}"
            )

            return (
                url,
                parsed,
            )

        # ----------------------------------------------------
        # Master playlist
        # ----------------------------------------------------

        if (
            parsed["type"] == "master"
        ):

            variants = parsed[
                "variants"
            ]

            variants.sort(
                key=lambda x: x.get(
                    "bandwidth",
                    0,
                ),
                reverse=True,
            )

            log(
                "[HLS] Master playlist "
                f"contains {len(variants)} "
                "variants."
            )

            for variant in variants:

                if await check_stop():
                    return None, None

                variant_url = (
                    variant["url"]
                )

                if (
                    variant_url
                    in checked
                ):
                    continue

                checked.add(
                    variant_url
                )

                log(
                    "[HLS] Trying variant: "
                    f"{variant_url}"
                )

                variant_text, _ = (
                    await browser_get_playlist(
                        context,
                        variant_url,
                    )
                )

                if not variant_text:
                    continue

                variant_parsed = (
                    parse_playlist(
                        variant_text,
                        variant_url,
                    )
                )

                if (
                    variant_parsed[
                        "type"
                    ] == "media"
                    and variant_parsed[
                        "segments"
                    ]
                ):

                    log(
                        "[HLS] Usable variant "
                        "found: "
                        f"{variant_url}"
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
                    "[SEGMENT] HTTP "
                    f"{response.status}: "
                    f"{url}"
                )

                return False

            data = await response.read()

            if not data:
                return False

            path.write_bytes(
                data
            )

            return True

    except asyncio.CancelledError:
        raise

    except Exception as exc:

        log(
            "[SEGMENT] Download failed: "
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
    log(
        "[RECORDER] Starting HLS "
        "recording..."
    )

    SEGMENTS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    downloaded = set()
    segment_files = []

    initial_segments = (
        initial_playlist.get(
            "segments",
            [],
        )
    )

    log(
        "[RECORDER] Initial segments: "
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

            try:

                current_headers = (
                    await build_browser_session(
                        page,
                        context,
                    )
                )

                headers.update(
                    current_headers
                )

            except Exception as exc:

                log(
                    "[SESSION] Could not "
                    "refresh headers: "
                    f"{exc}"
                )

            playlist_text, status = (
                await browser_get_playlist(
                    context,
                    playlist_url,
                )
            )

            if not playlist_text:

                log(
                    "[RECORDER] Playlist "
                    "request failed; retrying..."
                )

                await asyncio.sleep(
                    PLAYLIST_POLL_INTERVAL
                )

                continue

            parsed = parse_playlist(
                playlist_text,
                playlist_url,
            )

            # ------------------------------------------------
            # If master playlist appears again
            # ------------------------------------------------

            if (
                parsed["type"]
                == "master"
            ):

                variants = parsed[
                    "variants"
                ]

                variants.sort(
                    key=lambda x: x.get(
                        "bandwidth",
                        0,
                    ),
                    reverse=True,
                )

                switched = False

                for variant in variants:

                    if await check_stop():
                        break

                    variant_text, _ = (
                        await browser_get_playlist(
                            context,
                            variant["url"],
                        )
                    )

                    if not variant_text:
                        continue

                    variant_parsed = (
                        parse_playlist(
                            variant_text,
                            variant["url"],
                        )
                    )

                    if (
                        variant_parsed[
                            "type"
                        ] == "media"
                        and variant_parsed[
                            "segments"
                        ]
                    ):

                        playlist_url = (
                            variant["url"]
                        )

                        parsed = (
                            variant_parsed
                        )

                        switched = True

                        break

                if stop_requested:
                    break

                if not switched:

                    await asyncio.sleep(
                        PLAYLIST_POLL_INTERVAL
                    )

                    continue

            # ------------------------------------------------
            # New segments
            # ------------------------------------------------

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
                    "[RECORDER] New segments: "
                    f"{len(new_segments)}"
                )

            # ------------------------------------------------
            # Download segments
            # ------------------------------------------------

            for segment_url in (
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

                path = (
                    SEGMENTS_DIR
                    / filename
                )

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

            if await check_stop():
                break

            await asyncio.sleep(
                PLAYLIST_POLL_INTERVAL
            )

    log(
        "[RECORDER] Recording stopped. "
        f"Segments saved: "
        f"{len(segment_files)}"
    )

    return segment_files


# ============================================================
# MP4 muxing
# ============================================================

def create_concat_file(
    segment_files,
    filename="segments.txt",
):
    WORK_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    concat_file = (
        WORK_DIR
        / filename
    )

    with concat_file.open(
        "w",
        encoding="utf-8",
    ) as file:

        for path in segment_files:

            absolute_path = (
                path.resolve()
            )

            escaped = (
                str(absolute_path)
                .replace(
                    "'",
                    "'\\''",
                )
            )

            file.write(
                f"file '{escaped}'\n"
            )

    return concat_file


def mux_segments(
    segment_files,
):
    if not segment_files:

        log(
            "[MUX] No recorded segments."
        )

        return None

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    concat_file = (
        create_concat_file(
            segment_files
        )
    )

    output_file = (
        OUTPUT_DIR
        / f"{safe_filename(RECORD_ID)}.mp4"
    )

    log(
        f"[MUX] Creating MP4: "
        f"{output_file}"
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

            log(
                result.stderr[-5000:]
            )

            return None

        if not output_file.exists():

            log(
                "[MUX] Output file was "
                "not created."
            )

            return None

        size_mb = (
            output_file.stat().st_size
            / 1024
            / 1024
        )

        log(
            "[MUX] MP4 ready: "
            f"{size_mb:.2f} MB"
        )

        return output_file

    except Exception as exc:

        log(
            "[MUX] Exception: "
            f"{exc}"
        )

        return None


# ============================================================
# Split large MP4 into valid MP4 video parts
# ============================================================

def mux_part(
    segment_files,
    part_number,
):
    """
    Creates one valid MP4 from a group of TS segments.

    No video re-encoding is performed.
    """

    if not segment_files:
        return None

    PARTS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    part_file = (
        PARTS_DIR
        / (
            f"{safe_filename(RECORD_ID)}"
            f"_part_{part_number:03d}.mp4"
        )
    )

    concat_name = (
        f"part_{part_number:03d}.txt"
    )

    concat_file = create_concat_file(
        segment_files,
        concat_name,
    )

    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
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
        str(part_file),
    ]

    try:

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:

            log(
                "[PART] FFmpeg failed for "
                f"Part {part_number}:"
            )

            log(
                result.stderr[-3000:]
            )

            try:
                part_file.unlink(
                    missing_ok=True
                )
            except Exception:
                pass

            return None

        if not part_file.exists():
            return None

        return part_file

    except Exception as exc:

        log(
            "[PART] Exception while "
            f"creating Part {part_number}: "
            f"{exc}"
        )

        return None


def split_recording_into_parts(
    segment_files,
):
    """
    Splits the recorded TS segments into valid MP4 files.

    Important:
    We do NOT split an MP4 at arbitrary byte offsets.
    Instead, each part is independently muxed from complete
    HLS segments. This keeps every part playable as a video.

    The video stream is copied with -c copy, so there is no
    video re-encoding or intentional quality loss.
    """

    if not segment_files:
        return []

    total_bytes = sum(
        path.stat().st_size
        for path in segment_files
        if path.exists()
    )

    total_mb = (
        total_bytes
        / 1024
        / 1024
    )

    log(
        "[PARTS] Recorded TS data: "
        f"{total_mb:.2f} MB"
    )

    # If the entire recording is already small,
    # create one MP4 only.
    if total_mb <= PART_TARGET_MB:
        log(
            "[PARTS] Recording is already "
            "small enough for one video."
        )

        return []

    log(
        "[PARTS] Large recording detected. "
        f"Target per part: {PART_TARGET_MB} MB"
    )

    PARTS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Remove old generated parts.
    for old_file in PARTS_DIR.glob(
        "*.mp4"
    ):
        try:
            old_file.unlink()
        except Exception:
            pass

    parts = []

    current_index = 0
    part_number = 1
    total_segments = len(
        segment_files
    )

    while current_index < total_segments:

        if stop_requested:
            log(
                "[PARTS] Stop flag detected "
                "during part generation."
            )

        best_part_file = None
        best_end_index = current_index

        candidate_files = []

        index = current_index

        while index < total_segments:

            candidate_files.append(
                segment_files[index]
            )

            candidate_part = mux_part(
                candidate_files,
                part_number,
            )

            if candidate_part is None:

                # If the candidate could not be muxed,
                # stop this part attempt.
                candidate_files.pop()

                break

            candidate_size = (
                candidate_part.stat().st_size
            )

            candidate_mb = (
                candidate_size
                / 1024
                / 1024
            )

            log(
                "[PARTS] Candidate Part "
                f"{part_number}: "
                f"{candidate_mb:.2f} MB "
                f"({len(candidate_files)} segments)"
            )

            if (
                candidate_size
                <= PART_TARGET_BYTES
            ):

                # This candidate fits.
                if best_part_file is not None:
                    try:
                        best_part_file.unlink(
                            missing_ok=True
                        )
                    except Exception:
                        pass

                best_part_file = (
                    candidate_part
                )

                best_end_index = (
                    index + 1
                )

                index += 1

                continue

            # ------------------------------------------------
            # Candidate is too large.
            # ------------------------------------------------

            try:
                candidate_part.unlink(
                    missing_ok=True
                )
            except Exception:
                pass

            # Remove the segment that made it too large.
            candidate_files.pop()

            break

        # ----------------------------------------------------
        # Safety: one HLS segment itself can theoretically be
        # larger than our target.
        # ----------------------------------------------------

        if best_part_file is None:

            log(
                "[PARTS] A single HLS segment "
                "is larger than the target. "
                "Creating the smallest possible "
                "valid MP4 part."
            )

            single_segment = [
                segment_files[
                    current_index
                ]
            ]

            best_part_file = mux_part(
                single_segment,
                part_number,
            )

            if best_part_file is None:

                log(
                    "[PARTS] Failed to create "
                    f"Part {part_number}."
                )

                return parts

            best_end_index = (
                current_index + 1
            )

            actual_size = (
                best_part_file.stat().st_size
                / 1024
                / 1024
            )

            log(
                "[PARTS] Part "
                f"{part_number}: "
                f"{actual_size:.2f} MB"
            )

        else:

            final_size = (
                best_part_file.stat().st_size
                / 1024
                / 1024
            )

            log(
                "[PARTS] Final Part "
                f"{part_number}: "
                f"{final_size:.2f} MB"
            )

        parts.append(
            best_part_file
        )

        current_index = (
            best_end_index
        )

        part_number += 1

    log(
        "[PARTS] Created "
        f"{len(parts)} video part(s)."
    )

    return parts


# ============================================================
# Telegram upload
# ============================================================

async def send_video_file(
    file_path,
    part_number=None,
    total_parts=None,
):
    if not file_path:
        return False

    if not file_path.exists():
        log(
            "[TELEGRAM] Output file does "
            "not exist."
        )

        return False

    size_mb = (
        file_path.stat().st_size
        / 1024
        / 1024
    )

    log(
        "[TELEGRAM] Video size: "
        f"{size_mb:.2f} MB"
    )

    if size_mb >= TELEGRAM_MAX_MB:

        log(
            "[TELEGRAM] Refusing to upload "
            "a file too close to/over the "
            "Telegram limit: "
            f"{size_mb:.2f} MB"
        )

        return False

    if part_number is not None:

        caption = (
            f"🎥 التسجيل #{RECORD_ID}\n"
            f"Part {part_number}"
        )

        if total_parts is not None:
            caption += (
                f" / {total_parts}"
            )

    else:

        caption = (
            f"🎥 Recording "
            f"#{RECORD_ID}"
        )

    try:

        log(
            "[TELEGRAM] Uploading video: "
            f"{file_path.name}"
        )

        with file_path.open(
            "rb"
        ) as file:

            result = (
                await telegram_request(
                    "sendVideo",
                    data={
                        "chat_id": ADMIN_USER_ID,
                        "caption": caption,
                        "supports_streaming": (
                            "true"
                        ),
                    },
                    file_data=file,
                    file_name=file_path.name,
                )
            )

        if (
            result
            and result[0] == 200
        ):

            log(
                "[TELEGRAM] Video uploaded "
                "successfully: "
                f"{file_path.name}"
            )

            return True

        log(
            "[TELEGRAM] Video upload "
            "failed: "
            f"{file_path.name}"
        )

    except Exception as exc:

        log(
            "[TELEGRAM] Upload failed: "
            f"{exc}"
        )

    return False


async def send_recording(
    output_file,
    segment_files,
):
    """
    Sends the recording to Telegram.

    Small recording:
        one MP4 video.

    Large recording:
        multiple valid MP4 videos,
        each comfortably below 49 MB.
    """

    if not output_file:
        return False

    full_size_mb = (
        output_file.stat().st_size
        / 1024
        / 1024
    )

    log(
        "[TELEGRAM] Final MP4 size: "
        f"{full_size_mb:.2f} MB"
    )

    # --------------------------------------------------------
    # Small enough: send one video
    # --------------------------------------------------------

    if full_size_mb < TELEGRAM_MAX_MB:

        await send_message(
            f"🎬 اكتمل التسجيل #{RECORD_ID}\n"
            f"📦 الحجم: {full_size_mb:.1f} MB\n"
            "📤 جارٍ إرسال الفيديو..."
        )

        uploaded = await send_video_file(
            output_file
        )

        if uploaded:
            return True

        return False

    # --------------------------------------------------------
    # Large recording
    # --------------------------------------------------------

    await send_message(
        f"🎬 اكتمل التسجيل #{RECORD_ID}\n"
        f"📦 الحجم الكامل: {full_size_mb:.1f} MB\n"
        "✂️ الملف كبير، سيتم تقسيمه "
        "إلى عدة فيديوهات وإرسالها "
        "بالترتيب..."
    )

    parts = (
        split_recording_into_parts(
            segment_files
        )
    )

    if not parts:

        await send_message(
            "❌ تعذر تقسيم التسجيل "
            "إلى أجزاء قابلة للإرسال."
        )

        return False

    total_parts = len(parts)

    await send_message(
        f"📤 سيتم إرسال "
        f"{total_parts} فيديوهات "
        "بالترتيب."
    )

    all_uploaded = True

    for index, part_file in enumerate(
        parts,
        start=1,
    ):

        if not part_file.exists():
            all_uploaded = False
            continue

        size_mb = (
            part_file.stat().st_size
            / 1024
            / 1024
        )

        log(
            "[TELEGRAM] Sending Part "
            f"{index}/{total_parts}: "
            f"{size_mb:.2f} MB"
        )

        uploaded = await send_video_file(
            part_file,
            part_number=index,
            total_parts=total_parts,
        )

        if not uploaded:

            all_uploaded = False

            await send_message(
                f"⚠️ تعذر إرسال "
                f"Part {index} من "
                f"{total_parts}."
            )

            # Continue trying the remaining parts.
            continue

    if all_uploaded:

        await send_message(
            f"✅ تم إرسال التسجيل "
            f"#{RECORD_ID} بالكامل "
            f"في {total_parts} أجزاء."
        )

    else:

        await send_message(
            f"⚠️ انتهى إرسال التسجيل "
            f"#{RECORD_ID}، لكن تعذر "
            "إرسال جزء أو أكثر."
        )

    return all_uploaded


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
        f"Opening stream page: "
        f"{PAGE_URL}"
    )

    log(
        f"[SESSION] Record ID: "
        f"{RECORD_ID}"
    )

    log(
        f"[STOP] Watching GitHub file: "
        f"{STOP_FILE}"
    )

    discovered = []

    segment_files = []

    async with async_playwright() as playwright:

        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features="
                "AutomationControlled",
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

        # ----------------------------------------------------
        # HLS network listener
        # ----------------------------------------------------

        async def handle_response(
            response
        ):
            try:

                url = response.url

                if is_m3u8(url):

                    if url not in discovered:

                        discovered.append(
                            url
                        )

                        log(
                            "[HLS] Discovered "
                            "from network response: "
                            f"{url}"
                        )

            except Exception:
                pass

        # Listener MUST exist before goto().
        page.on(
            "response",
            handle_response,
        )

        # ----------------------------------------------------
        # Open stream page
        # ----------------------------------------------------

        try:

            await page.goto(
                PAGE_URL,
                wait_until=(
                    "domcontentloaded"
                ),
                timeout=60000,
            )

        except Exception as exc:

            log(
                f"[PAGE] goto warning: "
                f"{exc}"
            )

        # ----------------------------------------------------
        # HLS discovery period
        # ----------------------------------------------------

        discovery_started = time.time()

        while (
            time.time()
            - discovery_started
            < DISCOVERY_TIMEOUT
        ):

            if await check_stop():
                break

            await inspect_page_for_hls(
                page,
                discovered,
            )

            if discovered:

                if (
                    time.time()
                    - discovery_started
                    > 5
                ):
                    break

            await asyncio.sleep(
                1
            )

        await inspect_page_for_hls(
            page,
            discovered,
        )

        # ----------------------------------------------------
        # Stop before recording
        # ----------------------------------------------------

        if await check_stop():

            log(
                "[STOP] Stop requested "
                "before recording began."
            )

            await browser.close()

            await send_message(
                f"⏹️ تم إيقاف التسجيل "
                f"#{RECORD_ID} قبل بدء "
                "تسجيل المقاطع."
            )

            return 0

        # ----------------------------------------------------
        # No HLS
        # ----------------------------------------------------

        if not discovered:

            log(
                "[HLS] No HLS playlist "
                "discovered."
            )

            await send_message(
                "❌ لم يتم اكتشاف رابط HLS للبث."
            )

            await browser.close()

            return 1

        log(
            "[HLS] Total discovered URLs: "
            f"{len(discovered)}"
        )

        # ----------------------------------------------------
        # Find usable playlist
        # ----------------------------------------------------

        playlist_url, playlist = (
            await find_live_playlist(
                context,
                discovered,
            )
        )

        if (
            not playlist_url
            or not playlist
        ):

            if await check_stop():

                await browser.close()

                await send_message(
                    f"⏹️ تم إيقاف التسجيل "
                    f"#{RECORD_ID}."
                )

                return 0

            log(
                "[HLS] Discovered URLs "
                "were not usable "
                "media playlists."
            )

            await send_message(
                "❌ تم اكتشاف HLS لكن "
                "تعذر الوصول إلى قائمة "
                "المقاطع الخاصة بالبث."
            )

            await browser.close()

            return 1

        log(
            "[HLS] Recording playlist: "
            f"{playlist_url}"
        )

        # ----------------------------------------------------
        # Browser session headers
        # ----------------------------------------------------

        headers = (
            await build_browser_session(
                page,
                context,
            )
        )

        # ----------------------------------------------------
        # Record
        # ----------------------------------------------------

        segment_files = (
            await record_hls(
                context,
                page,
                playlist_url,
                playlist,
                headers,
            )
        )

        # ----------------------------------------------------
        # Close browser
        # ----------------------------------------------------

        try:
            await browser.close()
        except Exception:
            pass

    # ========================================================
    # Finalize
    # ========================================================

    log(
        "Stopping recorder and "
        "preparing final MP4..."
    )

    # --------------------------------------------------------
    # No segments
    # --------------------------------------------------------

    if not segment_files:

        log(
            "[MUX] No recorded segments."
        )

        if stop_requested:

            await send_message(
                f"⏹️ تم إيقاف التسجيل "
                f"#{RECORD_ID}، لكن لم "
                "يتم تسجيل أي مقطع."
            )

        else:

            await send_message(
                "❌ لم يتم تسجيل أي جزء من البث."
            )

        return 0

    # --------------------------------------------------------
    # Create complete MP4
    # --------------------------------------------------------

    output_file = mux_segments(
        segment_files
    )

    if not output_file:

        await send_message(
            "❌ فشل إنشاء ملف MP4 بعد "
            "انتهاء التسجيل."
        )

        return 1

    # --------------------------------------------------------
    # Send complete recording / parts
    # --------------------------------------------------------

    uploaded = await send_recording(
        output_file,
        segment_files,
    )

    if uploaded:

        if stop_requested:

            await send_message(
                f"⏹️ تم إيقاف التسجيل "
                f"#{RECORD_ID} وإرسال "
                "التسجيل بنجاح."
            )

        else:

            await send_message(
                f"✅ انتهى التسجيل "
                f"#{RECORD_ID} وتم إرسال "
                "التسجيل بنجاح."
            )

    else:

        await send_message(
            f"⚠️ انتهى التسجيل "
            f"#{RECORD_ID}، لكن تعذر "
            "إرسال التسجيل بالكامل."
        )

        return 1

    # --------------------------------------------------------
    # GitHub runner cleans workspace
    # --------------------------------------------------------

    return 0


# ============================================================
# Entrypoint
# ============================================================

if __name__ == "__main__":

    try:

        exit_code = asyncio.run(
            main()
        )

        sys.exit(
            exit_code
        )

    except KeyboardInterrupt:

        log(
            "[STOP] Keyboard interrupt."
        )

        sys.exit(0)

    except Exception as exc:

        log(
            "[FATAL] "
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        try:

            asyncio.run(
                send_message(
                    "❌ حدث خطأ غير متوقع "
                    "أثناء التسجيل."
                )
            )

        except Exception:
            pass

        sys.exit(1)
