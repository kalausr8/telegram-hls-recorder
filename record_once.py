import os
import re
import sys
import asyncio
import subprocess
from pathlib import Path

from playwright.async_api import async_playwright
from telegram import Bot


BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])

WORK_DIR = Path("/tmp/hls-recordings")
WORK_DIR.mkdir(parents=True, exist_ok=True)


async def find_m3u8(page_url: str):
    found = []
    headers = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        context = await browser.new_context(
            ignore_https_errors=True
        )

        page = await context.new_page()

        async def on_request(request):
            url = request.url.lower()

            if ".m3u8" in url:
                if request.url not in found:
                    found.append(request.url)

                    if not headers:
                        request_headers = request.headers

                        headers.update({
                            "User-Agent": request_headers.get(
                                "user-agent", ""
                            ),
                            "Referer": request_headers.get(
                                "referer", page_url
                            ),
                            "Origin": request_headers.get(
                                "origin", ""
                            ),
                        })

        page.on("request", on_request)

        print("Opening stream page...")

        await page.goto(
            page_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        await page.wait_for_timeout(10000)

        try:
            await page.locator("video").first.evaluate(
                """video => {
                    video.muted = true;
                    video.play().catch(() => {});
                }"""
            )
        except Exception:
            pass

        await page.wait_for_timeout(10000)

        cookies = await context.cookies()

        await browser.close()

    if not found:
        return None, None

    selected = found[0]

    for url in found:
        if "master" in url.lower():
            selected = url
            break

    cookie_header = "; ".join(
        f"{c['name']}={c['value']}"
        for c in cookies
    )

    if cookie_header:
        headers["Cookie"] = cookie_header

    return selected, headers


def build_ffmpeg_command(
    m3u8_url: str,
    headers: dict,
    output: Path,
):
    header_lines = []

    for key in (
        "User-Agent",
        "Referer",
        "Origin",
        "Cookie",
    ):
        value = headers.get(key)

        if value:
            header_lines.append(
                f"{key}: {value}"
            )

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",

        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "10",

        "-headers",
        "".join(
            f"{line}\r\n"
            for line in header_lines
        ),

        "-i",
        m3u8_url,

        "-map",
        "0",
        "-c",
        "copy",

        "-movflags",
        "+faststart",

        str(output),
    ]


async def send_file(output: Path):
    if not output.exists():
        return

    size = output.stat().st_size

    bot = Bot(token=BOT_TOKEN)

    await bot.send_message(
        chat_id=ADMIN_USER_ID,
        text=(
            "🎥 انتهى التسجيل التجريبي.\n"
            f"حجم الملف: {size / 1024 / 1024:.1f} MB"
        ),
    )

    if size <= 49 * 1024 * 1024:
        with output.open("rb") as video:
            await bot.send_document(
                chat_id=ADMIN_USER_ID,
                document=video,
                filename=output.name,
            )

    else:
        await bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=(
                "⚠️ التسجيل أكبر من 49MB، "
                "لذلك لم يتم إرساله في هذه المرحلة."
            ),
        )


async def main():
    if len(sys.argv) < 2:
        print(
            "Usage: python record_once.py "
            "https://example.com/stream/123"
        )
        sys.exit(1)

    page_url = sys.argv[1].strip()

    if not re.match(
        r"^https?://",
        page_url,
        re.I,
    ):
        print("Invalid URL.")
        sys.exit(1)

    print("Searching for HLS playlist...")

    m3u8_url, headers = await find_m3u8(
        page_url
    )

    if not m3u8_url:
        print(
            "ERROR: No m3u8 playlist was found."
        )
        sys.exit(1)

    print("HLS playlist found:")
    print(m3u8_url)

    output = WORK_DIR / "recording.mp4"

    command = build_ffmpeg_command(
        m3u8_url,
        headers or {},
        output,
    )

    print("Starting FFmpeg recording...")

    process = await asyncio.create_subprocess_exec(
        *command
    )

    try:
        await process.wait()

    except KeyboardInterrupt:
        process.send_signal(
            subprocess.signal.SIGINT
        )
        await process.wait()

    print(
        f"FFmpeg finished with code "
        f"{process.returncode}"
    )

    if output.exists() and output.stat().st_size > 0:
        await send_file(output)

    else:
        print("No recording file was created.")


if __name__ == "__main__":
    asyncio.run(main())
