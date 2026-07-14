"""
utils/helpers.py
Small shared helper functions used across the app.
"""
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# Dedicated thread pool for agents (STT, TTS) that need to spin up their own
# nested event loop via asyncio.run() inside a worker thread (whisper/pyttsx3
# are sync, edge-tts is genuinely async, so a nested loop is the simplest way
# to run either behind a uniform timeout). This MUST be a separate pool from
# the orchestrator event loop's own default executor: that default executor
# is also used by api/gemini_api.py (LLM calls) and agents/meeting_agent.py
# (status polling + bot removal via asyncio.to_thread). Running asyncio.run()
# inside a thread borrowed from that shared default pool has been observed to
# leave it unusable ("cannot schedule new futures after shutdown") for every
# later call on it for the rest of the session -- including the bot-removal
# call in MeetingAgent.leave(), which then fails silently and leaves the bot
# stuck in the meeting even though the session gets marked "completed".
# Giving STT/TTS their own pool means their nested loops can never take the
# LLM/meeting-control pool down with them.
NESTED_LOOP_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="nested-loop")


def new_session_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().isoformat()


def seconds_between(start_iso: str, end_iso: str) -> float:
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    return (end - start).total_seconds()


def clamp(value, low, high):
    return max(low, min(high, value))
