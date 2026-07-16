"""
routes/webhook_routes.py
Receives Meeting BaaS v2 webhook events (bot.status_change, bot.completed,
bot.failed) and updates the matching interview session accordingly.
"""
from flask import Blueprint, jsonify, request

from interview.session import session_store
from utils.logger import get_logger

logger = get_logger("api")

webhook_bp = Blueprint("webhooks", __name__, url_prefix="/api/webhooks")

# bot_id -> session_id, populated by browser/meet_bot.py when a bot is created.
BOT_TO_SESSION: dict = {}


@webhook_bp.route("/meetingbaas", methods=["POST"])
def meetingbaas_webhook():
    payload = request.get_json(silent=True) or {}
    event = payload.get("event")
    data = payload.get("data", {})
    bot_id = payload.get("bot_id") or data.get("bot_id")

    logger.info("Meeting BaaS webhook: event=%s bot_id=%s", event, bot_id)

    session_id = BOT_TO_SESSION.get(bot_id)
    if not session_id:
        return jsonify({"status": "ignored", "reason": "unknown bot_id"}), 200

    # v2 events only: bot.status_change (live), bot.completed, bot.failed.
    # (v1's unprefixed "complete"/"failed" event names are intentionally
    # NOT handled here -- this app is v2-only.)
    if event == "bot.status_change":
        status = data.get("status") or payload.get("status")
        # Meeting BaaS nests the actual status code under data.code for
        # bot.status_change (e.g. {"code": "call_ended", "sub_code": "call_ended_by_host"}).
        status = status or data.get("code")
        if status in ("in_call", "in_call_recording", "joined"):
            session_store.update(session_id, meeting_joined=True, bot_status="joined")
        elif status in ("waiting_room", "joining"):
            session_store.update(session_id, bot_status="joining")
        elif status in ("failed", "ended", "left", "call_ended"):
            session_store.update(session_id, bot_status="left" if status == "call_ended" else status)
            # The call itself has ended on Meeting BaaS's side (most often
            # because the candidate left/ended the Meet call) -- the bot is
            # already gone from the call whether our interview loop knows it
            # yet or not. Without this, the background orchestrator thread
            # has no way to find out mid-loop and just keeps asking
            # questions into an empty call until it happens to hit its
            # question limit, so the graceful "leave after the interview is
            # done" wrap-up (report generation, final leave() call) never
            # runs promptly -- it only ever appears to happen "after the
            # user leaves" because that's coincidentally when the loop
            # finally gives up, long after the fact. Signalling cancel_event
            # here makes the loop notice within its next check (it's
            # polled throughout _conduct_interview) and immediately run
            # wrap-up instead of waiting the interview out.
            session = session_store.get(session_id)
            if session and session.status in ("joining", "in_progress") and not session.cancel_event.is_set():
                logger.info(
                    "[%s] Meeting BaaS reports the call ended (status=%s); "
                    "signalling the interview loop to wrap up now.", session_id, status,
                )
                session.cancel_event.set()
                session_store.update(session_id, status="ended")

    elif event == "bot.failed":
        session_store.update(
            session_id,
            status="failed",
            bot_status="failed",
            error_message=data.get("error") or data.get("message") or payload.get("error", "Meeting BaaS bot failed to join."),
        )

    elif event == "bot.completed":
        session_store.update(session_id, bot_status="left")

    return jsonify({"status": "ok"}), 200
