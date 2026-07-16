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
        # Previously MEETINGBAAS_WAITING_ROOM_TIMEOUT was only used as our
        # own local polling deadline in meeting_agent.join() -- it was never
        # actually told to Meeting BaaS, so the bot itself had no
        # automatic_leave configured and just sat on whatever platform
        # default applies. Sending it here makes Meeting BaaS's own bot
        # enforce the same timeout we're polling against, so it leaves the
        # waiting room on its own even if our polling loop never gets to.
        "automatic_leave": {
            "waiting_room_timeout": config.MEETINGBAAS_WAITING_ROOM_TIMEOUT,
        },
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


_TERMINAL_GONE_STATUSES = ("ended", "left", "call_ended", "completed", "failed", None)


def _confirm_bot_left(bot_id: str, timeout: float = 20.0, poll_interval: float = 2.0) -> bool:
    """Polls get_bot_status() until Meeting BaaS actually reports the bot as
    out of the call, instead of trusting a 200 from DELETE at face value.

    This mirrors join()'s reasoning exactly, just in reverse: join() learned
    the hard way that a connected websocket isn't proof of admission because
    joining is asynchronous on Meeting BaaS's side -- only a status poll
    reporting in_call/joined is. Leaving is asynchronous the same way: the
    DELETE just requests removal, and the bot can take a few seconds to
    actually exit the call afterward. Treating the 200 itself as "gone" (the
    previous behavior) is what let the caller mark bot_status="left" and move
    on while the bot was, in reality, still sitting in the meeting.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status_data = get_bot_status(bot_id)
        except MeetingBaaSError:
            # Status lookup itself failing (e.g. 404, bot record gone) is
            # confirmation the bot is no longer a live participant.
            logger.info("Meeting BaaS bot %s: status lookup failed while confirming departure; treating as gone.", bot_id)
            return True
        status = status_data.get("status") or status_data.get("data", {}).get("status")
        if status in _TERMINAL_GONE_STATUSES:
            logger.info("Meeting BaaS bot %s confirmed out of the call (status=%s).", bot_id, status)
            return True
        logger.debug("Meeting BaaS bot %s still shows status=%s; waiting for it to actually leave.", bot_id, status)
        time.sleep(poll_interval)

    logger.error(
        "Meeting BaaS bot %s: removal was accepted but the bot still hadn't left the call "
        "after %.0fs of polling — it may still be in the meeting.", bot_id, timeout,
    )
    return False


def remove_bot(bot_id: str, retries: int = 2, confirm_timeout: float = 20.0) -> bool:
    """Removes/ends a bot's participation in its meeting.

    Returns True if the bot is confirmed gone (Meeting BaaS reports it's
    already gone/ended, or a status poll after the DELETE confirms it
    actually left within confirm_timeout), False otherwise. Callers should
    treat False as "the bot may still be in the meeting" rather than
    assuming success, and should not mark the bot as left in that case.
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
                # Meeting BaaS's general Bots API docs describe removal as
                # DELETE /bots/{bot_id} -- no /v2/ prefix -- while create_bot
                # and get_bot_status both work fine under /v2/bots/... (the
                # v2 prefix clearly IS live for those). It's possible the
                # v2 prefix was just never wired up for delete specifically,
                # which would produce exactly this symptom: an immediate,
                # unconditional 404 even seconds after a status poll on the
                # same bot_id confirmed it was live. Try the unprefixed path
                # before concluding the bot record is actually gone.
                fallback_resp = requests.delete(
                    f"{config.MEETINGBAAS_BASE_URL}/bots/{bot_id}",
                    headers=_headers(include_content_type=False),
                    timeout=30,
                )
                if fallback_resp.status_code != 404:
                    logger.info(
                        "Meeting BaaS bot %s: /v2/bots/%s 404'd, but the unprefixed "
                        "/bots/%s endpoint returned %s -- using that result instead.",
                        bot_id, bot_id, bot_id, fallback_resp.status_code,
                    )
                    resp = fallback_resp
                    if resp.status_code < 400:
                        return _confirm_bot_left(bot_id, timeout=confirm_timeout)
                    resp.raise_for_status()
                else:
                    # Both paths 404 -- this is as confident a "gone" signal
                    # as the API can give us, but it's still not direct
                    # proof the browser tab closed. Give it a beat and log
                    # loudly so a recurrence is diagnosable.
                    logger.warning(
                        "Meeting BaaS bot %s: DELETE 404'd on both /v2/bots/%s and "
                        "/bots/%s. This does not by itself confirm the browser has "
                        "left the call -- if it's still visibly in the meeting after "
                        "this, that's a Meeting BaaS-side inconsistency worth "
                        "reporting to their support with this bot_id and timestamp.",
                        bot_id, bot_id, bot_id,
                    )
                    time.sleep(3.0)
                    return True

            resp.raise_for_status()
            logger.info(
                "Meeting BaaS bot %s removal request accepted; confirming it actually "
                "leaves the call before reporting success...", bot_id,
            )
            # The DELETE being accepted only means Meeting BaaS *started*
            # removing the bot, not that it's out of the call yet -- confirm
            # via status poll the same way join() confirms admission, rather
            # than returning True immediately.
            return _confirm_bot_left(bot_id, timeout=confirm_timeout)
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
    # A 5xx/network failure on DELETE doesn't mean the bot is still in the
    # call -- it just means we couldn't confirm removal that way. Before
    # reporting failure, check the bot's actual status once, the same way
    # the 4xx branch above does, rather than assuming worst-case.
    try:
        status_data = get_bot_status(bot_id)
        status = status_data.get("status") or status_data.get("data", {}).get("status")
        if status in _TERMINAL_GONE_STATUSES:
            logger.info(
                "Meeting BaaS bot %s is actually already out of the call (status=%s) "
                "despite remove_bot's DELETE calls failing; treating as removed.",
                bot_id, status,
            )
            return True
        logger.error(
            "Meeting BaaS bot %s still shows status=%s after remove_bot exhausted retries — "
            "it may still be in the meeting.", bot_id, status,
        )
    except MeetingBaaSError:
        pass
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
