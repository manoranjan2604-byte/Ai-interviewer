"""
routes/webhook_routes.py
Receives Meeting BaaS webhook events (bot.status_change, complete, failed)
and updates the matching interview session accordingly.
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

    # v2 events: bot.status_change (live), bot.completed, bot.failed
    # v1 events (kept for compatibility): bot.status_change, complete, failed
    if event == "bot.status_change":
        status = data.get("status") or payload.get("status")
        if status in ("in_call", "in_call_recording", "joined"):
            session_store.update(session_id, meeting_joined=True, bot_status="joined")
        elif status in ("waiting_room", "joining"):
            session_store.update(session_id, bot_status="joining")
        elif status in ("failed", "ended", "left"):
            session_store.update(session_id, bot_status=status)

    elif event in ("failed", "bot.failed"):
        session_store.update(
            session_id,
            status="failed",
            bot_status="failed",
            error_message=data.get("error") or payload.get("error", "Meeting BaaS bot failed to join."),
        )

    elif event in ("complete", "bot.completed"):
        session_store.update(session_id, bot_status="left")

    return jsonify({"status": "ok"}), 200
