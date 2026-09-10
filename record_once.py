import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright
from telegram import Bot


BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["ADMIN_USER_ID"]

PAGE_URL = sys.argv[1]

OUTPUT_DIR = Path("recordings")
OUTPUT_DIR.mkdir(exist_ok=True)

SEGMENT_TIME = 120  # دقيقتان لكل جزء


async def find_stream(page):
    found = None

    def handle_response(response):
        nonlocal found
        url = response.url

        if ".m3u8" in url.lower() and found is None:
            found = url
            print("M3U8 FOUND:", url)

    page.on("response", handle_response)

    await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)

    # إعطاء الصفحة وقتًا لاكتشاف البث
    for _ in range(30):
        if found:
            break
        await asyncio.sleep(1)

    return found


async def main():
    print("Opening stream page...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox"]
        )

        page = await browser.new_page()

        m3u8_url = await find_stream(page)

        if not m3u8_url:
            print("ERROR: m3u8 not found")
            await browser.close()
            return

        user_agent = await page.evaluate("navigator.userAgent")

        cookies = await page.context.cookies()
        cookie_header = "; ".join(
            f"{c['name']}={c['value']}" for c in cookies
        )

        await browser.close()

    print("Starting recording...")

    output_pattern = str(
        OUTPUT_DIR / "part_%03d.mp4"
    )

    headers = (
        f"User-Agent: {user_agent}\r\n"
        f"Referer: {PAGE_URL}\r\n"
    )

    if cookie_header:
        headers += f"Cookie: {cookie_header}\r\n"

    command = [
        "ffmpeg",
        "-y",

        "-headers",
        headers,

        "-i",
        m3u8_url,

        "-c",
        "copy",

        "-f",
        "segment",
        "-segment_time",
        str(SEGMENT_TIME),
        "-reset_timestamps",
        "1",

        output_pattern,
    ]

    process = subprocess.Popen(command)

    bot = Bot(BOT_TOKEN)

    sent_files = set()

    try:
        while process.poll() is None:

            for file in sorted(OUTPUT_DIR.glob("part_*.mp4")):

                if file.name in sent_files:
                    continue

                # نتأكد أن FFmpeg انتهى من كتابة الملف
                size1 = file.stat().st_size
                await asyncio.sleep(2)

                if not file.exists():
                    continue

                size2 = file.stat().st_size

                if size1 != size2:
                    continue

                size_mb = size2 / (1024 * 1024)

                if size_mb > 49:
                    print(
                        f"Skipping {file.name}: "
                        f"{size_mb:.1f} MB"
                    )
                    continue

                print(
                    f"Sending {file.name} "
                    f"({size_mb:.1f} MB)"
                )

                try:
                    with open(file, "rb") as video:
                        await bot.send_document(
                            chat_id=CHAT_ID,
                            document=video,
                            caption=f"🎥 جزء من التسجيل: {file.name}"
                        )

                    sent_files.add(file.name)

                    file.unlink(missing_ok=True)

                except Exception as e:
                    print(
                        "Telegram upload error:",
                        e
                    )

            await asyncio.sleep(5)

    except KeyboardInterrupt:
        process.terminate()

    finally:
        if process.poll() is None:
            process.terminate()

        process.wait()

    # إرسال أي جزء أخير
    for file in sorted(OUTPUT_DIR.glob("part_*.mp4")):

        if file.name in sent_files:
            continue

        size_mb = file.stat().st_size / (1024 * 1024)

        if size_mb > 49:
            continue

        try:
            with open(file, "rb") as video:
                await bot.send_document(
                    chat_id=CHAT_ID,
                    document=video,
                    caption=f"🎥 جزء من التسجيل: {file.name}"
                )

            file.unlink(missing_ok=True)

        except Exception as e:
            print("Final upload error:", e)

    await bot.send_message(
        chat_id=CHAT_ID,
        text="✅ انتهى التسجيل."
    )


if __name__ == "__main__":
    asyncio.run(main())
