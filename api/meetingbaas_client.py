"""
api/meetingbaas_client.py
Thin client for the Meeting BaaS Bots API (https://docs.meetingbaas.com).
Meeting BaaS runs the actual bot infrastructure that reliably joins
Google Meet/Zoom/Teams calls, so this replaces the Playwright-based guest
join for Google Meet, which Google's client actively blocks.
"""
import time
from typing import Any, Dict, Optional

import requests

from config import config
from utils.logger import get_logger

logger = get_logger("browser")


class MeetingBaaSError(Exception):
    """Raised when a Meeting BaaS API call fails."""


def _headers(include_content_type: bool = True) -> Dict[str, str]:
    if not config.MEETINGBAAS_API_KEY:
        raise MeetingBaaSError("MEETINGBAAS_API_KEY is not set.")
    headers = {"x-meeting-baas-api-key": config.MEETINGBAAS_API_KEY}
    if include_content_type:
        # Only set this when we're actually sending a JSON body (POST/PUT).
        # Meeting BaaS's backend (Fastify) rejects any request that declares
        # Content-Type: application/json but sends no body — a bodyless
        # DELETE or GET with this header set gets a 400
        # FST_ERR_CTP_EMPTY_JSON_BODY instead of doing anything, which is
        # exactly what was silently leaving bots stuck in the call.
        headers["Content-Type"] = "application/json"
    return headers


def create_bot(
    meeting_url: str,
    bot_name: str,
    webhook_url: Optional[str] = None,
    input_stream_url: Optional[str] = None,
    output_stream_url: Optional[str] = None,
    entry_message: Optional[str] = None,
) -> str:
    """
    Deploys a bot into the given meeting via the Meeting BaaS v2 API.
    Returns the bot_id.

    input_stream_url / output_stream_url are WebSocket (wss://) endpoints
    on this server: Meeting BaaS reads audio to play into the call from
    input_stream_url's connection, and pushes the meeting's audio to us
    over output_stream_url. See routes/audio_ws_routes.py.
    """
    payload: Dict[str, Any] = {
        "meeting_url": meeting_url,
        "bot_name": bot_name,
        "recording_mode": "audio_only",
        "transcription_enabled": True,
        "transcription_config": {"provider": "gladia"},
    }
    if entry_message:
        payload["entry_message"] = entry_message
    if webhook_url:
        payload["webhook_url"] = webhook_url
    if input_stream_url and output_stream_url:
        # Per Meeting BaaS v2's streaming reference: streaming_enabled=true plus
        # a nested streaming_config object with output_url/input_url/audio_frequency
        # (integer Hz). Using the same audio_frequency your PCM conversion targets
        # (see browser/meet_bot.py SAMPLE_RATE) avoids sample-rate mismatches.
        payload["streaming_enabled"] = True
        payload["streaming_config"] = {
            "output_url": output_stream_url,
            "input_url": input_stream_url,
            "audio_frequency": 16000,
        }

    try:
        logger.debug("Meeting BaaS create_bot payload: %s", payload)
        resp = requests.post(
            f"{config.MEETINGBAAS_BASE_URL}/v2/bots",
            headers=_headers(),
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.debug("Meeting BaaS create_bot response: %s", data)
        bot_id = data.get("bot_id") or data.get("data", {}).get("bot_id") or data.get("id")
        if not bot_id:
            raise MeetingBaaSError(f"No bot_id in response: {data}")
        logger.info("Meeting BaaS bot %s created for %s", bot_id, meeting_url)
        return bot_id
    except requests.RequestException as exc:
        logger.error("Meeting BaaS create_bot failed: %s", exc)
        raise MeetingBaaSError(str(exc)) from exc


def remove_bot(bot_id: str, retries: int = 2) -> bool:
    """Removes/ends a bot's participation in its meeting.

    Returns True if the bot is confirmed gone (the DELETE succeeded, or
    Meeting BaaS reports it's already gone/ended — both mean "not in the
    call anymore"), False otherwise. Callers should treat False as "the
    bot may still be in the meeting" rather than assuming success, and
    should not mark the bot as left in that case.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.delete(
                f"{config.MEETINGBAAS_BASE_URL}/v2/bots/{bot_id}",
                headers=_headers(include_content_type=False),
                timeout=30,
            )
            if resp.status_code == 404:
                # Already removed/ended — nothing left to do, this is a
                # success from the caller's point of view.
                logger.info("Meeting BaaS bot %s was already gone (404).", bot_id)
                return True

            resp.raise_for_status()
            logger.info("Meeting BaaS bot %s removed.", bot_id)
            return True
        except requests.RequestException as exc:
            last_exc = exc
            body = getattr(exc.response, "text", "") if getattr(exc, "response", None) is not None else ""
            status_code = getattr(exc.response, "status_code", None) if getattr(exc, "response", None) is not None else None
            if status_code and 400 <= status_code < 500 and status_code != 429:
                # Client-side rejection (bad bot_id, bot already left the
                # call on its own, etc.) — retrying the same request won't
                # help, so check the bot's actual status once instead of
                # burning retries, and stop.
                logger.warning(
                    "Meeting BaaS remove_bot rejected for %s (status=%s, attempt %d/%d): %s",
                    bot_id, status_code, attempt, retries, body or exc,
                )
                try:
                    status_data = get_bot_status(bot_id)
                    status = status_data.get("status") or status_data.get("data", {}).get("status")
                    if status in ("ended", "left", "call_ended", "completed", "failed", None):
                        logger.info(
                            "Meeting BaaS bot %s is already out of the call (status=%s); "
                            "treating remove_bot as successful despite the %s response.",
                            bot_id, status, status_code,
                        )
                        return True
                    logger.error(
                        "Meeting BaaS bot %s still shows status=%s after remove_bot was rejected — "
                        "it may still be in the meeting.", bot_id, status,
                    )
                except MeetingBaaSError:
                    pass
                return False

            logger.warning(
                "Meeting BaaS remove_bot failed for %s (attempt %d/%d): %s",
                bot_id, attempt, retries, exc,
            )
            if attempt < retries:
                time.sleep(1.5 * attempt)

    logger.error("Meeting BaaS remove_bot exhausted retries for %s: %s", bot_id, last_exc)
    return False


def get_bot_status(bot_id: str) -> Dict[str, Any]:
    """Fetches current status/metadata for a bot."""
    try:
        resp = requests.get(
            f"{config.MEETINGBAAS_BASE_URL}/v2/bots/{bot_id}",
            headers=_headers(include_content_type=False),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        logger.error("Meeting BaaS get_bot_status failed for %s: %s", bot_id, exc)
        raise MeetingBaaSError(str(exc)) from exc
