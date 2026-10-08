# auto_monitor.py - Robust Auto Monitor
#
# Main goals:
# - Check every non-active watchlist user.
# - Classify stream state before triggering.
# - Never exceed MAX_ACTIVE_RECORDINGS.
# - Reserve an active lease before repository_dispatch.
# - Remove stale/broken leases conservatively.
# - Treat GitHub/API failures as "unknown", never as "zero active recordings".

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone

import aiohttp
from playwright.async_api import async_playwright


# =============================================================================
# CONFIGURATION
# =============================================================================

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TANGO_URL = os.getenv("TANGO_URL", "")
TANGO_USERNAME = os.getenv("TANGO_USERNAME", "")
TANGO_PASSWORD = os.getenv("TANGO_PASSWORD", "")

MAX_ACTIVE_RECORDINGS = 5

# أقصى عمر مطلق للـ recording lease.
MAX_RECORDING_AGE_HOURS = 6

# إذا كان lease في حالة starting ولم يصل heartbeat بعد،
# نعطيه مهلة كافية للإقلاع.
STARTING_LEASE_GRACE_MINUTES = 10

# بعد هذه المدة من دون heartbeat/status موثوق يعتبر lease stale.
RECORDING_LEASE_STALE_MINUTES = 15

# heartbeat/status خلال هذه المدة يعتبر fresh.
MAX_STATUS_STALE_MINUTES = 10

# lease تالف/ناقص البيانات لا نحذفه مباشرة.
# نعطيه 30 دقيقة إذا كان timestamp حديثًا.
MALFORMED_LEASE_GRACE_MINUTES = 30

WATCHLIST_PATH = "watchlist.json"

ACTIVE_DIR = ".recorder/active"
STATUS_DIR = ".recorder/status"

POLL_SECONDS = 60

DIAGNOSTIC_MODE = os.getenv("DIAGNOSTIC_MODE", "0") == "1"


# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("auto_monitor")


# =============================================================================
# GENERIC HELPERS
# =============================================================================

def utc_now():
    return datetime.now(timezone.utc)


def parse_dt(value):
    """
    Convert ISO timestamp / unix timestamp to timezone-aware UTC datetime.
    Return None if invalid.
    """

    if not value:
        return None

    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except Exception:
            return None

    if not isinstance(value, str):
        return None

    value = value.strip()

    if not value:
        return None

    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        return None


def age_minutes(dt):
    if not dt:
        return None

    seconds = (utc_now() - dt).total_seconds()

    return max(0.0, seconds / 60.0)


def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_contents_url(path):
    return (
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/contents/{path}?ref={GITHUB_BRANCH}"
    )


def github_tree_url():
    return (
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/git/trees/{GITHUB_BRANCH}?recursive=1"
    )


# =============================================================================
# TELEGRAM
# =============================================================================

async def telegram_send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    try:
        async with aiohttp.ClientSession() as session:

            async with session.post(
                url,
                json=payload,
                timeout=20,
            ) as response:

                if response.status != 200:
                    log.warning(
                        "Telegram send failed: HTTP %s",
                        response.status,
                    )
                    return False

                return True

    except Exception as exc:
        log.warning(
            "Telegram send error: %s",
            exc,
        )

        return False


# =============================================================================
# GITHUB - FILE STATE
# =============================================================================

async def github_get_file_state(path):
    """
    Tri-state GitHub file reader.

    Returns:

        (True, data, True)
            File exists and JSON parsed.

        (True, None, True)
            File exists but JSON is malformed.

        (False, None, True)
            Genuine HTTP 404.

        (False, None, False)
            GitHub/API/network/authentication failure.

    IMPORTANT:
    A GitHub API error must NEVER be treated as a missing file.
    """

    if not GITHUB_TOKEN:
        log.error(
            "GitHub token missing while reading %s",
            path,
        )

        return False, None, False

    try:

        async with aiohttp.ClientSession() as session:

            async with session.get(
                github_contents_url(path),
                headers=github_headers(),
                timeout=30,
            ) as response:

                # ---------------------------------------------------------
                # Genuine 404
                # ---------------------------------------------------------

                if response.status == 404:
                    return False, None, True

                # ---------------------------------------------------------
                # Any other HTTP failure
                # ---------------------------------------------------------

                if response.status != 200:

                    body = await response.text()

                    log.error(
                        "GitHub read failed for %s: HTTP %s | %s",
                        path,
                        response.status,
                        body[:300],
                    )

                    return False, None, False

                # ---------------------------------------------------------
                # Parse GitHub response
                # ---------------------------------------------------------

                payload = await response.json(
                    content_type=None
                )

                content = payload.get("content")

                if not content:

                    log.error(
                        "GitHub file %s has no content field",
                        path,
                    )

                    return False, None, False

                # ---------------------------------------------------------
                # Decode JSON
                # ---------------------------------------------------------

                try:

                    raw = base64.b64decode(
                        content.replace("\n", "").encode("utf-8")
                    )

                    parsed = json.loads(
                        raw.decode("utf-8")
                    )

                except Exception as exc:

                    log.warning(
                        "GitHub file %s exists but JSON is malformed: %s",
                        path,
                        exc,
                    )

                    # الملف موجود بالفعل.
                    # لذلك لا نعتبره 404.
                    return True, None, True

                return True, parsed, True

    except Exception as exc:

        log.error(
            "GitHub read exception for %s: %s",
            path,
            exc,
        )

        return False, None, False


# =============================================================================
# GITHUB - SIMPLE FILE GET
# =============================================================================

async def github_get_file(path):

    found, data, readable = await github_get_file_state(path)

    if not readable:
        return None

    if not found:
        return None

    return data


# =============================================================================
# GITHUB - LAST COMMIT TIME
# =============================================================================

async def github_get_file_last_commit_time(path):

    if not GITHUB_TOKEN:
        return None

    try:

        url = (
            f"https://api.github.com/repos/"
            f"{GITHUB_OWNER}/{GITHUB_REPO}"
            f"/commits?path={path}"
            f"&sha={GITHUB_BRANCH}"
            f"&per_page=1"
        )

        async with aiohttp.ClientSession() as session:

            async with session.get(
                url,
                headers=github_headers(),
                timeout=30,
            ) as response:

                if response.status != 200:

                    log.warning(
                        "GitHub commit lookup failed for %s: HTTP %s",
                        path,
                        response.status,
                    )

                    return None

                data = await response.json(
                    content_type=None
                )

                if not data:
                    return None

                commit = data[0].get(
                    "commit",
                    {}
                )

                author = commit.get(
                    "author",
                    {}
                )

                return parse_dt(
                    author.get("date")
                )

    except Exception as exc:

        log.warning(
            "GitHub commit lookup exception for %s: %s",
            path,
            exc,
        )

        return None


# =============================================================================
# GITHUB - DELETE FILE
# =============================================================================

async def github_delete_file(
    path,
    reason="",
):

    if not GITHUB_TOKEN:

        log.error(
            "Cannot delete %s: GitHub token missing",
            path,
        )

        return False

    try:

        # -------------------------------------------------------------
        # First get SHA
        # -------------------------------------------------------------

        async with aiohttp.ClientSession() as session:

            async with session.get(
                github_contents_url(path),
                headers=github_headers(),
                timeout=30,
            ) as response:

                # الملف غير موجود أصلاً.
                if response.status == 404:
                    return True

                if response.status != 200:

                    log.warning(
                        "Cannot get SHA before deleting %s: HTTP %s",
                        path,
                        response.status,
                    )

                    return False

                payload = await response.json(
                    content_type=None
                )

                sha = payload.get("sha")

                if not sha:
                    return False

            # ---------------------------------------------------------
            # Delete
            # ---------------------------------------------------------

            delete_url = (
                f"https://api.github.com/repos/"
                f"{GITHUB_OWNER}/{GITHUB_REPO}"
                f"/contents/{path}"
            )

            body = {
                "message": (
                    f"Remove stale recording lease {path}"
                    + (
                        f" ({reason})"
                        if reason
                        else ""
                    )
                ),
                "sha": sha,
                "branch": GITHUB_BRANCH,
            }

            async with session.delete(
                delete_url,
                headers=github_headers(),
                json=body,
                timeout=30,
            ) as response:

                if response.status in (200, 204):

                    log.info(
                        "Deleted GitHub file %s",
                        path,
                    )

                    return True

                text = await response.text()

                log.warning(
                    "Failed deleting %s: HTTP %s | %s",
                    path,
                    response.status,
                    text[:300],
                )

                return False

    except Exception as exc:

        log.warning(
            "GitHub delete exception for %s: %s",
            path,
            exc,
        )

        return False


# =============================================================================
# GITHUB - WRITE FILE
# =============================================================================

async def github_put_file(
    path,
    data,
    message,
):

    if not GITHUB_TOKEN:

        log.error(
            "Cannot write %s: GitHub token missing",
            path,
        )

        return False

    try:

        raw = json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

        encoded = base64.b64encode(
            raw
        ).decode("ascii")

        sha = None

        async with aiohttp.ClientSession() as session:

            # ---------------------------------------------------------
            # Check whether file already exists
            # ---------------------------------------------------------

            async with session.get(
                github_contents_url(path),
                headers=github_headers(),
                timeout=30,
            ) as response:

                if response.status == 200:

                    payload = await response.json(
                        content_type=None
                    )

                    sha = payload.get("sha")

                elif response.status != 404:

                    log.warning(
                        "GitHub existing-file lookup failed for %s: HTTP %s",
                        path,
                        response.status,
                    )

                    return False

            # ---------------------------------------------------------
            # PUT
            # ---------------------------------------------------------

            url = (
                f"https://api.github.com/repos/"
                f"{GITHUB_OWNER}/{GITHUB_REPO}"
                f"/contents/{path}"
            )

            body = {
                "message": message,
                "content": encoded,
                "branch": GITHUB_BRANCH,
            }

            if sha:
                body["sha"] = sha

            async with session.put(
                url,
                headers=github_headers(),
                json=body,
                timeout=30,
            ) as response:

                if response.status in (200, 201):
                    return True

                text = await response.text()

                log.warning(
                    "GitHub write failed for %s: HTTP %s | %s",
                    path,
                    response.status,
                    text[:300],
                )

                return False

    except Exception as exc:

        log.warning(
            "GitHub write exception for %s: %s",
            path,
            exc,
        )

        return False


# =============================================================================
# WATCHLIST
# =============================================================================

async def github_get_watchlist():

    data = await github_get_file(
        WATCHLIST_PATH
    )

    if isinstance(data, list):
        return data

    if isinstance(data, dict):

        users = data.get(
            "users"
        )

        if isinstance(users, list):
            return users

    return []


# =============================================================================
# ACTIVE RECORDINGS
# =============================================================================

async def github_get_active_recordings():
    """
    Return active recordings.

    IMPORTANT:
        None = GitHub state could not be inspected reliably.

        [] = GitHub state was inspected successfully and there are
             genuinely no active recordings.
    """

    if not GITHUB_TOKEN:

        log.error(
            "Cannot inspect active recordings: GitHub token missing"
        )

        return None

    try:

        # -------------------------------------------------------------
        # Get complete repository tree
        # -------------------------------------------------------------

        async with aiohttp.ClientSession() as session:

            async with session.get(
                github_tree_url(),
                headers=github_headers(),
                timeout=30,
            ) as response:

                if response.status != 200:

                    text = await response.text()

                    log.error(
                        "GitHub tree read failed: HTTP %s | %s",
                        response.status,
                        text[:300],
                    )

                    return None

                payload = await response.json(
                    content_type=None
                )

        tree = payload.get(
            "tree"
        )

        if not isinstance(tree, list):

            log.error(
                "GitHub tree response is malformed"
            )

            return None

        active_recordings = []

        # -------------------------------------------------------------
        # Inspect every active lease
        # -------------------------------------------------------------

        for item in tree:

            path = item.get(
                "path"
            )

            if not isinstance(path, str):
                continue

            if not path.startswith(
                ACTIVE_DIR + "/"
            ):
                continue

            if not path.endswith(
                ".json"
            ):
                continue

            record_id = os.path.splitext(
                os.path.basename(path)
            )[0]

            # ---------------------------------------------------------
            # IMPORTANT:
            #
            # Do not use github_get_file() here because it cannot
            # distinguish:
            #
            #   404
            #   API failure
            #
            # ---------------------------------------------------------

            found, record_data, readable = (
                await github_get_file_state(path)
            )

            # ---------------------------------------------------------
            # GitHub/API failure:
            #
            # Conservatively count as active.
            # This prevents the cap from being exceeded.
            # ---------------------------------------------------------

            if not readable:

                log.warning(
                    "Cannot read active lease %s; "
                    "counting it as active conservatively",
                    path,
                )

                active_recordings.append(
                    {
                        "username": f"unknown:{record_id}",
                        "record_id": record_id,
                        "path": path,
                        "unknown": True,
                    }
                )

                continue

            # ---------------------------------------------------------
            # Genuine 404:
            #
            # The lease disappeared between tree scan and file read.
            #
            # Do NOT count it.
            # ---------------------------------------------------------

            if not found:

                log.info(
                    "Active lease %s disappeared between tree scan "
                    "and content read; ignoring it",
                    path,
                )

                continue

            # ---------------------------------------------------------
            # File exists but JSON is malformed.
            # ---------------------------------------------------------

            if not isinstance(record_data, dict):

                commit_time = (
                    await github_get_file_last_commit_time(
                        path
                    )
                )

                # -----------------------------------------------------
                # Cannot prove it is old.
                # Keep it active.
                # -----------------------------------------------------

                if commit_time is None:

                    log.warning(
                        "Malformed active lease %s and commit age "
                        "cannot be verified; keeping it active "
                        "conservatively",
                        path,
                    )

                    active_recordings.append(
                        {
                            "username": f"unknown:{record_id}",
                            "record_id": record_id,
                            "path": path,
                            "unknown": True,
                        }
                    )

                    continue

                commit_age = age_minutes(
                    commit_time
                )

                # -----------------------------------------------------
                # Old malformed lease -> remove.
                # -----------------------------------------------------

                if (
                    commit_age
                    > MALFORMED_LEASE_GRACE_MINUTES
                ):

                    log.warning(
                        "Removing malformed active lease %s: "
                        "commit age %.1f min > %d min",
                        path,
                        commit_age,
                        MALFORMED_LEASE_GRACE_MINUTES,
                    )

                    await github_delete_file(
                        path,
                        reason=(
                            "malformed lease older than grace period"
                        ),
                    )

                    await github_delete_file(
                        f"{STATUS_DIR}/{record_id}.json",
                        reason=(
                            "malformed active lease cleanup"
                        ),
                    )

                else:

                    log.warning(
                        "Keeping recent malformed active lease %s: "
                        "commit age %.1f min",
                        path,
                        commit_age,
                    )

                    active_recordings.append(
                        {
                            "username": f"unknown:{record_id}",
                            "record_id": record_id,
                            "path": path,
                            "unknown": True,
                        }
                    )

                continue

            # ---------------------------------------------------------
            # Normal lease verification
            # ---------------------------------------------------------

            alive = await is_recording_actually_alive(
                record_data
            )

            if alive:

                username = (
                    record_data.get("username")
                    or f"unknown:{record_id}"
                )

                active_recordings.append(
                    {
                        "username": username,
                        "record_id": record_id,
                        "path": path,
                        "data": record_data,
                    }
                )

            else:

                log.info(
                    "Removing stale active lease %s",
                    path,
                )

                await github_delete_file(
                    path,
                    reason="stale recording lease",
                )

                await github_delete_file(
                    f"{STATUS_DIR}/{record_id}.json",
                    reason="stale recording lease cleanup",
                )

        return active_recordings

    except Exception as exc:

        log.exception(
            "Failed to inspect active recordings: %s",
            exc,
        )

        return None


# =============================================================================
# ACTIVE USERNAMES
# =============================================================================

async def github_get_active_usernames():

    active_recordings = (
        await github_get_active_recordings()
    )

    # مهم جداً:
    # لا نحول فشل GitHub إلى [].
    if active_recordings is None:
        return None

    return [
        item.get("username")
        for item in active_recordings
        if item.get("username")
    ]


# =============================================================================
# RECORDING LEASE VERIFICATION
# =============================================================================

async def is_recording_actually_alive(record_data):
    """
    Strong conservative lease verification.

    Rules:

    1. Invalid/missing identity:
       - recent timestamp <= 30m -> keep
       - old timestamp > 30m -> stale
       - no usable timestamp -> keep conservatively

    2. Valid lease:
       - started_at > 6h -> stale

    3. starting:
       - heartbeat <= 10m -> alive
       - heartbeat > 10m -> stale

    4. Normal:
       - heartbeat <= 10m -> alive

    5. Stale heartbeat:
       - inspect recorder status.

    6. GitHub/API failure:
       - keep alive conservatively.

    7. Genuine status 404:
       - heartbeat > 15m -> stale
       - otherwise keep.

    8. Status exists:
       - fresh status <= 10m -> alive
       - heartbeat AND status > 15m -> stale
       - otherwise keep conservatively.
    """

    if not isinstance(
        record_data,
        dict,
    ):
        return True

    record_id = record_data.get(
        "record_id"
    )

    username = record_data.get(
        "username"
    )

    # =========================================================================
    # MALFORMED IDENTITY
    # =========================================================================

    if not record_id or not username:

        timestamp = (
            record_data.get("heartbeat_at")
            or record_data.get("started_at")
            or record_data.get("claimed_at")
            or record_data.get("created_at")
        )

        dt = parse_dt(
            timestamp
        )

        if dt is None:

            log.warning(
                "Malformed lease has no usable timestamp; "
                "keeping active conservatively"
            )

            return True

        age = age_minutes(
            dt
        )

        if (
            age
            <= MALFORMED_LEASE_GRACE_MINUTES
        ):

            log.warning(
                "Malformed lease is recent (%.1f min); "
                "keeping active",
                age,
            )

            return True

        log.warning(
            "Malformed lease is stale (%.1f min); "
            "marking inactive",
            age,
        )

        return False

    # =========================================================================
    # MAXIMUM RECORDING AGE
    # =========================================================================

    started_at = parse_dt(
        record_data.get("started_at")
    )

    if started_at:

        started_age = age_minutes(
            started_at
        )

        if (
            started_age
            > MAX_RECORDING_AGE_HOURS * 60
        ):

            log.warning(
                "Lease %s exceeded maximum age: %.1f min",
                record_id,
                started_age,
            )

            return False

    # =========================================================================
    # HEARTBEAT
    # =========================================================================

    heartbeat_at = parse_dt(
        record_data.get("heartbeat_at")
    )

    # Missing/invalid heartbeat alone is NOT proof of death.
    if heartbeat_at is None:

        log.warning(
            "Lease %s has no valid heartbeat; "
            "keeping active conservatively",
            record_id,
        )

        return True

    heartbeat_age = age_minutes(
        heartbeat_at
    )

    # =========================================================================
    # STARTING
    # =========================================================================

    state = str(
        record_data.get(
            "state",
            "",
        )
    ).strip().lower()

    if state == "starting":

        if (
            heartbeat_age
            <= STARTING_LEASE_GRACE_MINUTES
        ):
            return True

        log.warning(
            "Starting lease %s exceeded startup grace: %.1f min",
            record_id,
            heartbeat_age,
        )

        return False

    # =========================================================================
    # FRESH HEARTBEAT
    # =========================================================================

    if (
        heartbeat_age
        <= MAX_STATUS_STALE_MINUTES
    ):
        return True

    # =========================================================================
    # STALE HEARTBEAT -> CHECK STATUS
    # =========================================================================

    status_path = (
        f"{STATUS_DIR}/{record_id}.json"
    )

    found, status_data, readable = (
        await github_get_file_state(
            status_path
        )
    )

    # =========================================================================
    # GITHUB/API FAILURE
    # =========================================================================

    if not readable:

        log.warning(
            "Cannot read status for lease %s; "
            "keeping lease active conservatively",
            record_id,
        )

        return True

    # =========================================================================
    # TRUE 404
    # =========================================================================

    if not found:

        if (
            heartbeat_age
            > RECORDING_LEASE_STALE_MINUTES
        ):

            log.warning(
                "Lease %s has stale heartbeat %.1f min "
                "and missing status; marking inactive",
                record_id,
                heartbeat_age,
            )

            return False

        return True

    # =========================================================================
    # MALFORMED STATUS
    # =========================================================================

    if not isinstance(
        status_data,
        dict,
    ):

        status_commit_time = (
            await github_get_file_last_commit_time(
                status_path
            )
        )

        if status_commit_time is None:

            log.warning(
                "Status for lease %s is malformed and "
                "commit age is unverifiable; keeping active",
                record_id,
            )

            return True

        status_age = age_minutes(
            status_commit_time
        )

        if (
            heartbeat_age
            > RECORDING_LEASE_STALE_MINUTES
            and
            status_age
            > RECORDING_LEASE_STALE_MINUTES
        ):

            log.warning(
                "Lease %s has stale heartbeat %.1f min "
                "and malformed status %.1f min old; "
                "marking inactive",
                record_id,
                heartbeat_age,
                status_age,
            )

            return False

        return True

    # =========================================================================
    # STATUS TIMESTAMP
    # =========================================================================

    status_time = (
        parse_dt(
            status_data.get("heartbeat_at")
        )
        or
        parse_dt(
            status_data.get("updated_at")
        )
        or
        parse_dt(
            status_data.get("started_at")
        )
        or
        parse_dt(
            status_data.get("created_at")
        )
    )

    # إذا لم يوجد timestamp داخل status،
    # نستخدم آخر commit كاحتياط.
    if status_time is None:

        status_time = (
            await github_get_file_last_commit_time(
                status_path
            )
        )

    # لا نستطيع إثبات أن recorder مات.
    if status_time is None:

        log.warning(
            "Status for lease %s has no usable timestamp; "
            "keeping active",
            record_id,
        )

        return True

    status_age = age_minutes(
        status_time
    )

    # =========================================================================
    # FRESH STATUS
    # =========================================================================

    if (
        status_age
        <= MAX_STATUS_STALE_MINUTES
    ):
        return True

    # =========================================================================
    # BOTH HEARTBEAT AND STATUS STALE
    # =========================================================================

    if (
        heartbeat_age
        > RECORDING_LEASE_STALE_MINUTES
        and
        status_age
        > RECORDING_LEASE_STALE_MINUTES
    ):

        log.warning(
            "Lease %s is stale: heartbeat %.1f min, "
            "status %.1f min",
            record_id,
            heartbeat_age,
            status_age,
        )

        return False

    # =========================================================================
    # CONFLICTING/BORDERLINE EVIDENCE
    # =========================================================================

    return True


# =============================================================================
# TRIGGER RECORDING
# =============================================================================

async def trigger_recording(user):

    if not isinstance(
        user,
        dict,
    ):
        return False

    username = user.get(
        "username"
    )

    if not username:
        return False

    record_id = (
        str(
            user.get(
                "record_id"
            )
        )
        if user.get("record_id")
        else
        username.replace(
            "/",
            "_",
        ).replace(
            " ",
            "_",
        )
    )

    active_path = (
        f"{ACTIVE_DIR}/{record_id}.json"
    )

    now = utc_now().isoformat()

    lease = {
        "record_id": record_id,
        "username": username,
        "state": "starting",
        "claimed_at": now,
        "started_at": now,
        "heartbeat_at": now,
    }

    # =========================================================================
    # RESERVE FIRST
    # =========================================================================

    if not await github_put_file(
        active_path,
        lease,
        f"Reserve recording lease for {username}",
    ):

        log.error(
            "Could not reserve recording lease for %s",
            username,
        )

        return False

    # =========================================================================
    # DISPATCH
    # =========================================================================

    try:

        url = (
            f"https://api.github.com/repos/"
            f"{GITHUB_OWNER}/{GITHUB_REPO}"
            f"/dispatches"
        )

        payload = {
            "event_type": "record_stream",
            "client_payload": {
                "username": username,
                "record_id": record_id,
            },
        }

        async with aiohttp.ClientSession() as session:

            async with session.post(
                url,
                headers=github_headers(),
                json=payload,
                timeout=30,
            ) as response:

                if response.status not in (
                    200,
                    204,
                ):

                    text = await response.text()

                    log.error(
                        "repository_dispatch failed for %s: "
                        "HTTP %s | %s",
                        username,
                        response.status,
                        text[:300],
                    )

                    # فشل dispatch => تحرير الحجز.
                    await github_delete_file(
                        active_path,
                        reason="dispatch failed",
                    )

                    return False

        log.info(
            "Recording dispatched for %s",
            username,
        )

        return True

    except Exception as exc:

        log.exception(
            "Exception dispatching recording for %s: %s",
            username,
            exc,
        )

        # أي exception بعد الحجز => تحرير الحجز.
        await github_delete_file(
            active_path,
            reason="dispatch exception",
        )

        return False


# =============================================================================
# STREAM CLASSIFICATION
# =============================================================================

async def classify_stream(page):
    """
    Returns:

        live
        offline
        premium
        login_required
        unknown
    """

    try:

        text = (
            await page.locator(
                "body"
            ).inner_text()
        ).lower()

    except Exception as exc:

        log.warning(
            "Could not read Tango page text: %s",
            exc,
        )

        return "unknown"

    # =========================================================================
    # PREMIUM
    # =========================================================================

    premium_markers = (
        "premium",
        "subscriber only",
        "subscribers only",
        "private show",
    )

    if any(
        marker in text
        for marker in premium_markers
    ):
        return "premium"

    # =========================================================================
    # OFFLINE
    # =========================================================================

    offline_markers = (
        "offline",
        "not live",
        "isn't live",
        "is not live",
        "currently unavailable",
    )

    if any(
        marker in text
        for marker in offline_markers
    ):
        return "offline"

    # =========================================================================
    # LOGIN
    # =========================================================================

    login_markers = (
        "log in",
        "login",
        "sign in",
    )

    if any(
        marker in text
        for marker in login_markers
    ):
        return "login_required"

    # =========================================================================
    # LIVE
    # =========================================================================

    live_markers = (
        "live",
        "watch now",
        "viewers",
    )

    if any(
        marker in text
        for marker in live_markers
    ):
        return "live"

    return "unknown"


# =============================================================================
# TANGO MONITOR
# =============================================================================

async def monitor_tango():

    if not TANGO_URL:

        log.error(
            "TANGO_URL is not configured"
        )

        return []

    watchlist = (
        await github_get_watchlist()
    )

    if not watchlist:

        log.info(
            "Watchlist is empty"
        )

        return []

    # =========================================================================
    # GET ACTIVE RECORDINGS
    # =========================================================================

    active_recordings = (
        await github_get_active_recordings()
    )

    # =========================================================================
    # CRITICAL:
    #
    # None means UNKNOWN.
    #
    # NEVER continue as if there are zero recordings.
    # =========================================================================

    if active_recordings is None:

        log.error(
            "Active recording state is unknown; "
            "aborting this monitor cycle to protect "
            "the global recording cap"
        )

        return []

    active_count = len(
        active_recordings
    )

    log.info(
        "Active recordings: %d/%d",
        active_count,
        MAX_ACTIVE_RECORDINGS,
    )

    # =========================================================================
    # CAP REACHED
    # =========================================================================

    if (
        active_count
        >= MAX_ACTIVE_RECORDINGS
    ):

        log.info(
            "Recording capacity reached; "
            "no new recordings this cycle"
        )

        return []

    # =========================================================================
    # ACTIVE USERNAMES
    # =========================================================================

    active_usernames = {
        item.get("username")
        for item in active_recordings
        if item.get("username")
    }

    triggered = []

    # =========================================================================
    # PLAYWRIGHT
    # =========================================================================

    async with async_playwright() as playwright:

        browser = await playwright.chromium.launch(
            headless=True
        )

        context = await browser.new_context()

        page = await context.new_page()

        try:

            # -------------------------------------------------------------
            # Initial Tango page
            # -------------------------------------------------------------

            if (
                TANGO_USERNAME
                and
                TANGO_PASSWORD
            ):

                try:

                    await page.goto(
                        TANGO_URL,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )

                    log.info(
                        "Tango page loaded"
                    )

                except Exception as exc:

                    log.warning(
                        "Tango initial page load failed: %s",
                        exc,
                    )

            # -------------------------------------------------------------
            # WATCHLIST
            # -------------------------------------------------------------

            for user in watchlist:

                # ---------------------------------------------------------
                # Hard cap protection.
                # ---------------------------------------------------------

                if (
                    active_count
                    + len(triggered)
                    >= MAX_ACTIVE_RECORDINGS
                ):
                    break

                if not isinstance(
                    user,
                    dict,
                ):
                    continue

                username = user.get(
                    "username"
                )

                if not username:
                    continue

                # ---------------------------------------------------------
                # Already recording.
                # ---------------------------------------------------------

                if username in active_usernames:
                    continue

                # ---------------------------------------------------------
                # Stream URL
                # ---------------------------------------------------------

                stream_url = user.get(
                    "url"
                )

                if not stream_url:

                    stream_url = (
                        f"{TANGO_URL.rstrip('/')}"
                        f"/{username}"
                    )

                try:

                    await page.goto(
                        stream_url,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )

                    await page.wait_for_timeout(
                        1500
                    )

                    # -----------------------------------------------------
                    # Classify FIRST.
                    # -----------------------------------------------------

                    classification = (
                        await classify_stream(
                            page
                        )
                    )

                    log.info(
                        "%s -> %s",
                        username,
                        classification,
                    )

                    # -----------------------------------------------------
                    # Diagnostic mode
                    # -----------------------------------------------------

                    if DIAGNOSTIC_MODE:
                        continue

                    # -----------------------------------------------------
                    # Only LIVE can trigger recording.
                    # -----------------------------------------------------

                    if classification != "live":
                        continue

                    # -----------------------------------------------------
                    # Trigger
                    # -----------------------------------------------------

                    success = await trigger_recording(
                        user
                    )

                    if success:

                        triggered.append(
                            username
                        )

                        active_usernames.add(
                            username
                        )

                except Exception as exc:

                    log.warning(
                        "Monitor error for %s: %s",
                        username,
                        exc,
                    )

        finally:

            await context.close()

            await browser.close()

    return triggered


# =============================================================================
# MAIN LOOP
# =============================================================================

async def main():

    log.info(
        "Robust Auto Monitor started"
    )

    while True:

        try:

            triggered = (
                await monitor_tango()
            )

            if triggered:

                await telegram_send(
                    "Started recordings:\n"
                    + "\n".join(
                        f"- {username}"
                        for username in triggered
                    )
                )

        except Exception as exc:

            log.exception(
                "Main monitor cycle failed: %s",
                exc,
            )

        await asyncio.sleep(
            POLL_SECONDS
        )


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    asyncio.run(main())
