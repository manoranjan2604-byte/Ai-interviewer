"""
utils/redis_store.py
Thin, best-effort wrapper around a Redis client, used by
interview/session.py to mirror session status + JSON reports so they
survive a process restart / are readable from a different request than
the one that created them.

Deliberately NOT a general-purpose cache layer and NOT what makes the
interview loop itself multi-worker-safe — see the REDIS_URL comment in
config.py for what this does and doesn't solve. If REDIS_URL isn't set,
every function here is a silent no-op so the app runs exactly as it did
before (pure in-memory), matching this project's existing "never let an
optional integration take down the interview flow" pattern (see
agents/email_agent.py for the same philosophy applied to email).
"""
import json
from typing import Any, Dict, Optional

from config import config
from utils.logger import get_logger

logger = get_logger("redis_store")

_client = None
_init_attempted = False


def _get_client():
    global _client, _init_attempted
    if _init_attempted:
        return _client
    _init_attempted = True
    if not config.REDIS_URL:
        return None
    try:
        import redis

        _client = redis.from_url(config.REDIS_URL, decode_responses=True, socket_timeout=5)
        _client.ping()
        logger.info("Connected to Redis for session/report durability.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis configured but unavailable (%s) — falling back to in-memory only.", exc)
        _client = None
    return _client


def is_enabled() -> bool:
    return _get_client() is not None


def save_session(session_id: str, data: Dict[str, Any]) -> None:
    """Best-effort mirror of a session's public status/report data. Never
    raises — a Redis outage should degrade to in-memory-only, not break
    the interview."""
    client = _get_client()
    if not client:
        return
    try:
        client.set(
            f"session:{session_id}",
            json.dumps(data, default=str),
            ex=config.REDIS_SESSION_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to mirror session %s to Redis: %s", session_id, exc)


def load_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Best-effort read of a mirrored session. Returns None on any miss
    or Redis error, exactly like an in-memory cache miss."""
    client = _get_client()
    if not client:
        return None
    try:
        raw = client.get(f"session:{session_id}")
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read session %s from Redis: %s", session_id, exc)
        return None
