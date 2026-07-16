"""
utils/startup_validation.py
Fail-fast startup checks (bugs #5, #28).

Without this, misconfiguration only surfaces minutes into a real interview
(the Cerebras model_not_found 404 was found this way — bug #2). This module
validates env vars, provider credentials/models, and required local
binaries/models *before* the Flask app starts accepting sessions, and
reports every problem at once instead of one crash at a time.

Two severities:
  - errors   -> the server should not start (missing required credentials,
                unusable meeting/report pipeline, etc.)
  - warnings -> the server can start, but a feature will degrade/fail
                (e.g. optional SMTP not configured, so report emails won't
                send; fallback provider missing, so a primary outage has no
                safety net)

Call validate_startup(strict=True) from app.py. Set STARTUP_VALIDATION_STRICT=0
in .env to downgrade errors to warnings for local/dev iteration.
"""
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import List

from config import config
from utils.logger import get_logger

logger = get_logger("startup")


@dataclass
class ValidationResult:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors


def _check_meetingbaas(result: ValidationResult) -> None:
    if not config.MEETINGBAAS_API_KEY:
        result.error("MEETINGBAAS_API_KEY is not set — the bot cannot join any Google Meet call.")
    if not config.PUBLIC_BASE_URL:
        result.error(
            "PUBLIC_BASE_URL is not set — Meeting BaaS has no address to send webhooks/audio "
            "back to (e.g. an ngrok URL in dev, or your real domain in production)."
        )
    elif not config.PUBLIC_BASE_URL.startswith("https://") and not config.DEBUG:
        result.warn("PUBLIC_BASE_URL does not start with https:// — Meeting BaaS webhooks may be rejected.")


def _check_llm(result: ValidationResult) -> None:
    provider = (config.LLM_PROVIDER or "").lower()
    fallback = (config.LLM_FALLBACK_PROVIDER or "").lower() or None

    def has_key(p: str) -> bool:
        return bool(
            {
                "groq": config.GROQ_API_KEY,
                "cerebras": config.CEREBRAS_API_KEY,
            }.get(p)
        )

    if not has_key(provider):
        result.error(f"LLM_PROVIDER is '{provider}' but its API key is not set — no interview questions can be generated.")
    elif provider == "cerebras" and config.CEREBRAS_MODEL not in config.CEREBRAS_KNOWN_MODELS:
        result.warn(
            f"CEREBRAS_MODEL='{config.CEREBRAS_MODEL}' is not in the known-good list "
            f"({', '.join(config.CEREBRAS_KNOWN_MODELS)}). Cerebras periodically retires "
            "models (this is exactly how the old 'llama-3.3-70b' default started 404ing "
            "with model_not_found) — verify at https://inference-docs.cerebras.ai/models/overview."
        )

    if not fallback:
        result.warn("LLM_FALLBACK_PROVIDER is not set — a primary-provider outage/quota exhaustion has no automatic fallback.")
    elif not has_key(fallback):
        result.warn(f"LLM_FALLBACK_PROVIDER is '{fallback}' but its API key is not set — fallback will not actually work.")


def _check_stt(result: ValidationResult) -> None:
    if config.STT_PROVIDER == "whisper":
        try:
            import whisper  # noqa: F401
        except ImportError:
            result.error("STT_PROVIDER=whisper but the 'openai-whisper' package is not installed.")
        if shutil.which("ffmpeg") is None:
            result.error(
                "ffmpeg was not found on PATH — Whisper cannot decode audio without it. "
                "Install ffmpeg and make sure it's on PATH (this is the #1 cause of "
                "silent transcription failures on Windows)."
            )
    elif config.STT_PROVIDER == "groq" and not config.GROQ_API_KEY:
        result.error("STT_PROVIDER=groq but GROQ_API_KEY is not set.")


def _check_tts(result: ValidationResult) -> None:
    if config.TTS_PROVIDER == "edge":
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            result.error("TTS_PROVIDER=edge but the 'edge-tts' package is not installed.")
    elif config.TTS_PROVIDER == "local":
        try:
            import pyttsx3  # noqa: F401
        except ImportError:
            result.error("TTS_PROVIDER=local but the 'pyttsx3' package is not installed.")
        if sys.platform.startswith("linux") and shutil.which("espeak") is None:
            result.error(
                "TTS_PROVIDER=local on Linux requires 'espeak' on PATH for pyttsx3 to "
                "produce audio — without it, synthesis silently fails and the interviewer "
                "falls back to text-only (the bot joins/greets in chat but never speaks). "
                "Install it with: sudo apt-get install espeak"
            )


def _check_email(result: ValidationResult) -> None:
    if config.EMAIL_PROVIDER == "brevo":
        if not config.BREVO_API_KEY:
            result.warn("EMAIL_PROVIDER=brevo but BREVO_API_KEY is not set — report emails will not be sent.")
    elif config.EMAIL_PROVIDER == "resend":
        if not config.RESEND_API_KEY:
            result.warn("EMAIL_PROVIDER=resend but RESEND_API_KEY is not set — report emails will not be sent.")
    elif config.EMAIL_PROVIDER == "mailjet":
        if not (config.MAILJET_API_KEY and config.MAILJET_API_SECRET):
            result.warn("EMAIL_PROVIDER=mailjet but MAILJET_API_KEY/MAILJET_API_SECRET are not set — report emails will not be sent.")
    elif config.EMAIL_PROVIDER == "emailjs":
        if not (config.EMAILJS_SERVICE_ID and config.EMAILJS_TEMPLATE_ID and config.EMAILJS_PRIVATE_KEY):
            result.warn("EMAIL_PROVIDER=emailjs but EMAILJS_SERVICE_ID/EMAILJS_TEMPLATE_ID/EMAILJS_PRIVATE_KEY are not set — report emails will not be sent.")
    else:
        smtp_fields = (config.SMTP_HOST, config.SMTP_USERNAME, config.SMTP_PASSWORD, config.EMAIL_FROM)
        if any(smtp_fields) and not all(smtp_fields):
            result.warn("SMTP is partially configured — set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, and EMAIL_FROM together, or leave all blank.")
        elif not any(smtp_fields):
            result.warn("Email is not configured (SMTP or EMAIL_PROVIDER=brevo) — interview report emails will not be sent (bug #21).")


def _check_redis(result: ValidationResult) -> None:
    if not config.REDIS_URL:
        result.warn(
            "REDIS_URL is not set — session status/reports live in-memory only and are "
            "lost on restart, and won't be visible across multiple worker processes. "
            "Fine for a single dev/single-worker deployment; set REDIS_URL (e.g. a free "
            "Upstash database) for anything more durable."
        )
        return
    try:
        import redis  # noqa: F401
    except ImportError:
        result.error("REDIS_URL is set but the 'redis' package is not installed.")


def _check_secret_key(result: ValidationResult) -> None:
    if config.SECRET_KEY == "change-me-in-production" and not config.DEBUG:
        result.error("SECRET_KEY is still the default value — set a real SECRET_KEY before running outside DEBUG mode.")


def run_checks() -> ValidationResult:
    result = ValidationResult()
    _check_secret_key(result)
    _check_meetingbaas(result)
    _check_llm(result)
    _check_stt(result)
    _check_tts(result)
    _check_email(result)
    _check_redis(result)
    return result


def validate_startup(strict: bool = True) -> None:
    """Run all checks and log a single consolidated report. If strict and
    there are errors, exit the process instead of starting a server that
    can't actually run interviews."""
    result = run_checks()

    if result.warnings:
        logger.warning("Startup validation: %d warning(s):", len(result.warnings))
        for w in result.warnings:
            logger.warning("  - %s", w)

    if result.errors:
        logger.error("Startup validation: %d error(s):", len(result.errors))
        for e in result.errors:
            logger.error("  - %s", e)
        if strict:
            logger.error("Refusing to start with the above configuration errors. "
                         "Set STARTUP_VALIDATION_STRICT=0 to start anyway (not recommended).")
            sys.exit(1)

    if not result.warnings and not result.errors:
        logger.info("Startup validation passed with no issues.")
