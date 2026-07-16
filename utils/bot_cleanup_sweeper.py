"""
utils/bot_cleanup_sweeper.py
Background daemon thread that periodically retries Meeting BaaS bot removal
for any session stuck in bot_status == "leave_failed".

Why this exists: remove_bot() already retries a couple of times inline (see
api/meetingbaas_client.py), but if Meeting BaaS's DELETE endpoint itself is
erroring (5xx on both /v2/bots and the unprefixed fallback -- an outage on
their side, not a bad request), those inline retries can't help: the
endpoint is down *right now*, not intermittently within the same second.
Both the immediate /api/end call and the background interview loop's own
leave() call give up and mark the session bot_status="leave_failed", and
without something else picking it back up, the bot is simply abandoned in
the live call until Meeting BaaS's platform-level automatic_leave timeout
(if any) eventually kicks it, or someone removes it manually.

This sweeper closes that gap: it wakes up every
config.BOT_CLEANUP_SWEEP_INTERVAL_SECONDS, finds sessions still marked
leave_failed with a bot_id on record, and retries remove_bot() for each.
Retrying stops once either removal succeeds or
config.BOT_CLEANUP_SWEEP_MAX_AGE_SECONDS has elapsed since the session's
end_time -- past that point Meeting BaaS's own outage/timeout handling is
the more likely path to resolution, and retrying forever would just mean an
ever-growing set of background threads doing pointless work for sessions
that are never coming back.
"""
import threading
import time

from config import config
from utils.helpers import now_iso, seconds_between
from utils.logger import get_logger

logger = get_logger("bot_cleanup_sweeper")

_stop_event = threading.Event()
_thread: "threading.Thread | None" = None


def _sweep_once() -> None:
    # Local imports: avoids a circular import at module load time, same
    # reasoning as app.py's _cleanup_active_bots().
    from api.meetingbaas_client import remove_bot
    from interview.session import session_store

    for session in session_store.all():
        if session.bot_status != "leave_failed" or not session.bot_id:
            continue

        age = seconds_between(session.end_time, now_iso()) if session.end_time else 0
        if age > config.BOT_CLEANUP_SWEEP_MAX_AGE_SECONDS:
            logger.error(
                "Bot cleanup sweep: giving up on bot %s (session %s) after %.0fs -- "
                "Meeting BaaS never confirmed removal. It may need manual removal "
                "from the meeting or a check with Meeting BaaS support.",
                session.bot_id, session.session_id, age,
            )
            session_store.update(session.session_id, bot_status="leave_abandoned")
            continue

        logger.info(
            "Bot cleanup sweep: retrying removal for bot %s (session %s, %.0fs since end).",
            session.bot_id, session.session_id, age,
        )
        try:
            removed = remove_bot(session.bot_id, confirm_timeout=10)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Bot cleanup sweep: remove_bot raised for %s: %s", session.bot_id, exc,
            )
            continue

        if removed:
            session_store.update(session.session_id, bot_status="left")
            logger.info(
                "Bot cleanup sweep: bot %s (session %s) confirmed removed.",
                session.bot_id, session.session_id,
            )
        # else: leave bot_status as "leave_failed" and let the next sweep retry.


def _run() -> None:
    logger.info(
        "Bot cleanup sweeper started (interval=%ds, max_age=%ds).",
        config.BOT_CLEANUP_SWEEP_INTERVAL_SECONDS, config.BOT_CLEANUP_SWEEP_MAX_AGE_SECONDS,
    )
    while not _stop_event.wait(config.BOT_CLEANUP_SWEEP_INTERVAL_SECONDS):
        try:
            _sweep_once()
        except Exception:  # noqa: BLE001
            logger.exception("Bot cleanup sweep iteration failed unexpectedly.")


def start() -> None:
    """Starts the sweeper thread. Safe to call multiple times -- only the
    first call (per process) actually starts a thread."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_run, name="bot-cleanup-sweeper", daemon=True)
    _thread.start()


def stop() -> None:
    """Signals the sweeper thread to stop. Used on graceful shutdown."""
    _stop_event.set()
