"""
routes/interview_routes.py
REST API endpoints for starting, monitoring, ending, and retrieving
reports for interview sessions.
"""
import random
import threading

from flask import Blueprint, jsonify, request, send_file

from agents.orchestrator_agent import OrchestratorAgent
from api.meetingbaas_client import remove_bot
from config import config
from interview.session import session_store
from utils.helpers import now_iso
from utils.logger import get_logger
from utils.validators import sanitize_text, validate_start_payload

logger = get_logger("api")

interview_bp = Blueprint("interview", __name__, url_prefix="/api")


@interview_bp.route("/start", methods=["POST"])
def start_interview():
    data = request.get_json(silent=True) or {}

    is_valid, error = validate_start_payload(data)
    if not is_valid:
        return jsonify({"error": error}), 400

    meet_link = data["meet_link"].strip()
    if session_store.exists_active_for_meet_link(meet_link):
        return jsonify({"error": "An interview session is already running for this Meet link."}), 409

    # Randomize the question count per session (within MIN/MAX_QUESTIONS)
    # instead of always running the same fixed number of questions.
    question_limit = random.randint(config.MIN_QUESTIONS, config.MAX_QUESTIONS)

    try:
        session = session_store.create(
            name=sanitize_text(data["name"], 100),
            email=sanitize_text(data["email"], 254),
            meet_link=meet_link,
            question_limit=question_limit,
            role=sanitize_text(data["role"], 100) if data.get("role") else None,
            experience_level=sanitize_text(data["experience_level"], 20) if data.get("experience_level") else None,
        )
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 429

    def _run():
        manager = OrchestratorAgent(session)
        manager.run()

    thread = threading.Thread(target=_run, daemon=True, name=f"interview-{session.session_id}")
    thread.start()

    logger.info("Started interview session %s for %s <%s>", session.session_id, session.name, session.email)
    return jsonify({"session_id": session.session_id, "status": session.status}), 201


@interview_bp.route("/status/<session_id>", methods=["GET"])
def get_status(session_id: str):
    status = session_store.status_dict(session_id)
    if not status:
        return jsonify({"error": "Session not found."}), 404
    return jsonify(status), 200


@interview_bp.route("/end", methods=["POST"])
def end_interview():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    session = session_store.get(session_id) if session_id else None
    if not session:
        return jsonify({"error": "Session not found."}), 404

    if session.status in ("completed", "failed", "ended"):
        return jsonify({"error": f"Session already {session.status}."}), 409

    # Signal the background interview loop to stop cooperatively. It checks
    # this between steps (and while waiting for a spoken answer) and will
    # break out, leave the meeting, and finish cleanup (report, email) on
    # its own. This can take a little while if it's mid TTS/STT call, so
    # we don't rely on it alone below.
    session.cancel_event.set()
    session_store.update(session_id, status="ended", end_time=now_iso())

    # Also remove the bot from the Google Meet call directly, right now,
    # instead of only waiting for the background thread to notice
    # cancellation on its next check (which may be up to ~30-45s away if
    # it's mid TTS/STT call). This is what makes the bot actually leave
    # the meeting promptly when the user clicks "End interview".
    if session.bot_id:
        try:
            removed = remove_bot(session.bot_id)
            if removed:
                session_store.update(session_id, bot_status="left")
                logger.info("Session %s: bot %s removed from meeting immediately.", session_id, session.bot_id)
            else:
                logger.warning(
                    "Session %s: immediate bot removal for %s was not confirmed "
                    "(background cleanup will retry when the interview loop unwinds).",
                    session_id, session.bot_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Session %s: immediate bot removal failed (background cleanup will retry): %s", session_id, exc)

    logger.info("Session %s end requested by user; signaled cancellation.", session_id)
    return jsonify({"session_id": session_id, "status": "ended"}), 200


@interview_bp.route("/report/<session_id>", methods=["GET"])
def get_report(session_id: str):
    report_json, report_path, status = session_store.report_dict(session_id)
    if report_json is None and status is None:
        return jsonify({"error": "Session not found."}), 404

    if not report_json:
        return jsonify({"error": "Report not yet available.", "status": status}), 202

    fmt = request.args.get("format", "json").lower()
    if fmt == "pdf":
        if not report_path:
            return jsonify({"error": "PDF report not available (only the JSON report survives a restart or a different worker process)."}), 404
        return send_file(
            report_path,
            as_attachment=True,
            download_name=f"interview_report_{report_json.get('candidate_name', session_id).replace(' ', '_')}.pdf",
            mimetype="application/pdf",
        )

    return jsonify(report_json), 200


@interview_bp.errorhandler(Exception)
def handle_unexpected_error(exc):  # noqa: ANN001
    logger.exception("Unhandled error in interview routes")
    return jsonify({"error": "An unexpected server error occurred."}), 500
