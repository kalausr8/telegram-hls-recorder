import os
import re
import signal
import asyncio
import subprocess
from pathlib import Path
from dataclasses import dataclass

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from playwright.async_api import async_playwright


BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])

MAX_RECORDINGS = 5
WORK_DIR = Path("/tmp/hls-recordings")
WORK_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Recording:
    number: int
    page_url: str
    m3u8_url: str
    process: asyncio.subprocess.Process
    output: Path


recordings: dict[int, Recording] = {}
next_number = 1


def authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_USER_ID)


async def deny(update: Update):
    if update.message:
        await update.message.reply_text("⛔ غير مصرح لك باستخدام هذا البوت.")


async def find_m3u8(page_url: str):
    found = []
    headers = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path="/usr/bin/chromium",
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
                        headers.update({
                            "User-Agent": request.headers.get(
                                "user-agent", ""
                            ),
                            "Referer": request.headers.get(
                                "referer", page_url
                            ),
                            "Origin": request.headers.get(
                                "origin", ""
                            ),
                        })

        page.on("request", on_request)

        await page.goto(
            page_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        # Give the player time to start requesting the HLS playlist.
        await page.wait_for_timeout(10000)

        # Try common HTML5/video player play mechanisms.
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
        return None, None, None

    # Prefer master playlists when several playlists were detected.
    selected = found[0]

    for url in found:
        if "master" in url.lower():
            selected = url
            break

    cookie_header = "; ".join(
        f"{c['name']}={c['value']}" for c in cookies
    )

    if cookie_header:
        headers["Cookie"] = cookie_header

    return selected, headers, cookies


def build_ffmpeg_command(
    m3u8_url: str,
    headers: dict,
    output: Path,
):
    header_lines = []

    for key in ("User-Agent", "Referer", "Origin", "Cookie"):
        value = headers.get(key)

        if value:
            header_lines.append(f"{key}: {value}")

    command = [
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
        "".join(f"{line}\r\n" for line in header_lines),

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

    return command


async def send_finished_file(
    context: ContextTypes.DEFAULT_TYPE,
    recording: Recording,
):
    if not recording.output.exists():
        return

    size = recording.output.stat().st_size

    # Telegram Bot API currently has a 50 MB limit for bot file uploads.
    if size > 49 * 1024 * 1024:
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=(
                f"⚠️ التسجيل #{recording.number} انتهى.\n"
                f"حجم الملف: {size / 1024 / 1024:.1f} MB\n"
                "الملف أكبر من الحد المسموح به للإرسال المباشر عبر Bot API."
            ),
        )
        return

    await context.bot.send_message(
        chat_id=ADMIN_USER_ID,
        text=f"📤 إرسال التسجيل #{recording.number}..."
    )

    with recording.output.open("rb") as video:
        await context.bot.send_document(
            chat_id=ADMIN_USER_ID,
            document=video,
            filename=recording.output.name,
        )

    try:
        recording.output.unlink()
    except Exception:
        pass


async def monitor_recording(
    context: ContextTypes.DEFAULT_TYPE,
    recording: Recording,
):
    process = recording.process

    await process.wait()

    recordings.pop(recording.number, None)

    if process.returncode == 0:
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"✅ انتهى التسجيل #{recording.number}."
        )

        await send_finished_file(context, recording)

    else:
        if recording.output.exists() and recording.output.stat().st_size > 0:
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=(
                    f"⚠️ توقف التسجيل #{recording.number}، "
                    "وسأحاول إرسال الملف الناتج."
                ),
            )
            await send_finished_file(context, recording)
        else:
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=f"❌ فشل التسجيل #{recording.number}."
            )


async def record_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global next_number

    if not authorized(update):
        await deny(update)
        return

    if not context.args:
        await update.message.reply_text(
            "الاستخدام:\n/record https://example.com/stream/123"
        )
        return

    if len(recordings) >= MAX_RECORDINGS:
        await update.message.reply_text(
            f"⚠️ وصلت إلى الحد الأقصى: {MAX_RECORDINGS} تسجيلات."
        )
        return

    page_url = context.args[0].strip()

    if not re.match(r"^https?://", page_url, re.I):
        await update.message.reply_text("❌ الرابط غير صالح.")
        return

    number = next_number
    next_number += 1

    await update.message.reply_text(
        f"🔎 التسجيل #{number}\n"
        "جارٍ فتح صفحة البث واكتشاف HLS..."
    )

    try:
        m3u8_url, headers, _ = await find_m3u8(page_url)

    except Exception as exc:
        await update.message.reply_text(
            f"❌ تعذر فتح الصفحة أو اكتشاف البث.\n"
            f"{type(exc).__name__}"
        )
        return

    if not m3u8_url:
        await update.message.reply_text(
            "❌ لم يتم العثور على رابط HLS/m3u8 في الصفحة."
        )
        return

    output = WORK_DIR / f"recording_{number}.mp4"

    command = build_ffmpeg_command(
        m3u8_url,
        headers or {},
        output,
    )

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:
        await update.message.reply_text(
            f"❌ تعذر تشغيل FFmpeg.\n{type(exc).__name__}"
        )
        return

    recording = Recording(
        number=number,
        page_url=page_url,
        m3u8_url=m3u8_url,
        process=process,
        output=output,
    )

    recordings[number] = recording

    await update.message.reply_text(
        f"🔴 بدأ التسجيل #{number}\n\n"
        f"يمكنك إيقافه بواسطة:\n"
        f"/stop {number}"
    )

    asyncio.create_task(
        monitor_recording(context, recording)
    )


async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not authorized(update):
        await deny(update)
        return

    if not context.args:
        await update.message.reply_text(
            "الاستخدام:\n/stop 1"
        )
        return

    try:
        number = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ رقم تسجيل غير صالح.")
        return

    recording = recordings.get(number)

    if not recording:
        await update.message.reply_text(
            f"❌ لا يوجد تسجيل نشط برقم {number}."
        )
        return

    await update.message.reply_text(
        f"⏹️ إيقاف التسجيل #{number} وإنهاء ملف MP4..."
    )

    try:
        recording.process.send_signal(signal.SIGINT)
    except Exception:
        try:
            recording.process.terminate()
        except Exception:
            pass


async def stop_all_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not authorized(update):
        await deny(update)
        return

    if not recordings:
        await update.message.reply_text(
            "لا توجد تسجيلات نشطة."
        )
        return

    count = len(recordings)

    for recording in list(recordings.values()):
        try:
            recording.process.send_signal(signal.SIGINT)
        except Exception:
            try:
                recording.process.terminate()
            except Exception:
                pass

    await update.message.reply_text(
        f"⏹️ جارٍ إيقاف {count} تسجيلات وإنهاء ملفات MP4..."
    )


async def list_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not authorized(update):
        await deny(update)
        return

    if not recordings:
        await update.message.reply_text(
            "📭 لا توجد تسجيلات نشطة."
        )
        return

    lines = ["🔴 التسجيلات النشطة:\n"]

    for number, recording in recordings.items():
        lines.append(
            f"#{number} — /stop {number}\n"
            f"{recording.page_url}"
        )

    await update.message.reply_text("\n\n".join(lines))


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not authorized(update):
        await deny(update)
        return

    await update.message.reply_text(
        "🎥 HLS Recorder جاهز.\n\n"
        "بدء تسجيل:\n"
        "/record رابط_صفحة_البث\n\n"
        "عرض التسجيلات:\n"
        "/list\n\n"
        "إيقاف تسجيل:\n"
        "/stop رقم\n\n"
        "إيقاف الجميع:\n"
        "/stopall"
    )


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("record", record_command)
    )

    application.add_handler(
        CommandHandler("stop", stop_command)
    )

    application.add_handler(
        CommandHandler("stopall", stop_all_command)
    )

    application.add_handler(
        CommandHandler("list", list_command)
    )

    print("HLS Telegram Recorder started.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
