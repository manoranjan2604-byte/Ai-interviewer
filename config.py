"""
config.py
Centralized application configuration loaded from environment variables.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Config:
    # --- Flask ---
    SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me-in-production")
    DEBUG: bool = _bool("FLASK_DEBUG", False)
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "5000"))

    # --- CORS ---
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "*")

    # --- LLM provider ---
    # Trimmed 2026-07: this project only ever ran on Cerebras (primary) +
    # Groq (fallback), so Gemini/OpenAI support was removed as dead code.
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "cerebras")  # cerebras | groq
    # Cerebras: fast LPU inference, OpenAI-compatible API, generous free
    # tier — the default primary provider (replaces Gemini, whose
    # free-tier quota was getting exhausted mid-interview). Get a key at
    # https://cloud.cerebras.ai/
    CEREBRAS_API_KEY: str = os.getenv("CEREBRAS_API_KEY", "")
    # "llama-3.3-70b" / "llama3.1-70b" (the previous defaults) 404 with
    # model_not_found: Cerebras has retired the Llama family from its
    # public inference endpoints. Current model catalog (see
    # https://inference-docs.cerebras.ai/models/overview):
    #   gpt-oss-120b   - production, most stable, used as default below
    #   gemma-4-31b    - preview
    #   zai-glm-4.7    - preview, largest/most capable, but preview models
    #                    "may be discontinued on short notice" per Cerebras
    # Preview models are fine to experiment with, but don't build the
    # production default on one - hence gpt-oss-120b here.
    CEREBRAS_MODEL: str = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")
    CEREBRAS_BASE_URL: str = os.getenv("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")
    # Known-good model IDs as of writing, used by startup validation to
    # catch typos/deprecated names before the first real interview call
    # instead of 404-ing mid-session. Not exhaustive - Cerebras adds/retires
    # models over time - so validation treats this as a warning list, not
    # a hard allowlist.
    CEREBRAS_KNOWN_MODELS: tuple = ("gpt-oss-120b", "gemma-4-31b", "zai-glm-4.7")
    # Groq: free tier, OpenAI-compatible API, very fast (LPU inference) —
    # kept as an optional fallback so if Cerebras' quota is exhausted the
    # app doesn't stall/flatten a whole interview. Get a key at
    # https://console.groq.com/keys
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    GROQ_BASE_URL: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    # If set (e.g. "groq"), automatically retried when LLM_PROVIDER's call
    # fails, before falling back to the canned/neutral static responses.
    LLM_FALLBACK_PROVIDER: str = os.getenv("LLM_FALLBACK_PROVIDER", "groq")
    # Per-call timeout. 30s was long enough that two sequential failed
    # attempts against an exhausted-quota provider (see generate_question's
    # retry loop) could burn a real amount of wall-clock time every single
    # turn before ever reaching the fallback question/evaluation. Lowered
    # default; override in .env if your provider is genuinely slow rather
    # than failing.
    LLM_TIMEOUT_SECONDS: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "15"))
    # Once a provider fails with a quota/rate-limit error this many times in
    # a row, stop calling it for this many seconds and go straight to the
    # fallback provider (or canned/neutral responses) instead of re-trying a
    # call that's almost certain to fail again — this is what actually saves
    # the wasted time, rather than the per-call timeout alone.
    LLM_QUOTA_BREAKER_THRESHOLD: int = int(os.getenv("LLM_QUOTA_BREAKER_THRESHOLD", "2"))
    LLM_QUOTA_BREAKER_SECONDS: int = int(os.getenv("LLM_QUOTA_BREAKER_SECONDS", "300"))

    # --- Speech-to-text provider ---
    # Trimmed 2026-07: Google STT / Deepgram / AssemblyAI support removed
    # (never configured, unused dependencies). "groq" uses Groq's hosted
    # Whisper endpoint — far faster than local CPU whisper, same
    # GROQ_API_KEY as the LLM fallback provider above.
    STT_PROVIDER: str = os.getenv("STT_PROVIDER", "whisper")  # whisper | groq
    # Whisper model size vs. accuracy/speed tradeoff (tiny < base < small <
    # medium < large). "small" is a noticeably more accurate transcriber
    # than "base" for accented/noisy speech and technical vocabulary,
    # while still being CPU-feasible for interview-length clips; drop back
    # to "base" in .env if your CPU is slow and you need faster turnaround.
    # "base" is the default here: on CPU, "small" can
    # take well over a minute to transcribe a ~60-90s answer, which is what
    # mostly drives the "bot seems stuck between questions" symptom — the
    # interview loop *is* progressing, it's just waiting on a slow local
    # transcription. Drop to "tiny" for near-real-time turnaround if
    # accuracy on technical vocabulary is acceptable; go back up to
    # "small"/"medium" only if you have a GPU or don't mind the wait.
    WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "base")
    # Base timeout floor for a transcription call, plus how many seconds of
    # allowance per second of audio (CPU Whisper is slower than realtime,
    # especially on "small"+), capped by STT_TIMEOUT_MAX_SECONDS so a
    # corrupt/oversized clip can't hang the interview loop indefinitely.
    STT_TIMEOUT_MIN_SECONDS: int = int(os.getenv("STT_TIMEOUT_MIN_SECONDS", "45"))
    STT_TIMEOUT_SECONDS_PER_AUDIO_SECOND: float = float(os.getenv("STT_TIMEOUT_SECONDS_PER_AUDIO_SECOND", "3.0"))
    STT_TIMEOUT_MAX_SECONDS: int = int(os.getenv("STT_TIMEOUT_MAX_SECONDS", "180"))

    # --- Text-to-speech provider ---
    # Trimmed 2026-07: ElevenLabs / Google TTS / Azure Speech support
    # removed (never configured, unused paid-SDK dependencies). "edge"
    # uses the free, unlimited, no-key Edge-TTS voices — a straightforward
    # quality upgrade over "local" (pyttsx3) with no paid signup required.
    TTS_PROVIDER: str = os.getenv("TTS_PROVIDER", "local")  # local | edge

    # --- Meeting BaaS (Google Meet join + audio streaming) ---
    MEETINGBAAS_API_KEY: str = os.getenv("MEETINGBAAS_API_KEY", "")
    MEETINGBAAS_BASE_URL: str = os.getenv("MEETINGBAAS_BASE_URL", "https://api.meetingbaas.com")
    MEETINGBAAS_WAITING_ROOM_TIMEOUT: int = int(os.getenv("MEETINGBAAS_WAITING_ROOM_TIMEOUT", "600"))
    # Public base URL this Flask app is reachable at, e.g. https://your-app.ngrok-free.dev
    # or https://your-domain.com in production. Required so Meeting BaaS can send
    # webhooks and stream audio back to this server.
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "")

    # remove_bot() can fail if Meeting BaaS's DELETE endpoint is itself
    # erroring (5xx) rather than the bot genuinely being stuck -- in that
    # case neither the immediate /api/end call nor the background
    # interview loop's own leave() call can do anything more than what's
    # already baked into remove_bot()'s retries. BOT_CLEANUP_SWEEP_*
    # configures a periodic sweep (see utils/bot_cleanup_sweeper.py) that
    # keeps retrying removal for any session left in bot_status ==
    # "leave_failed", so the bot still gets kicked out once Meeting BaaS's
    # API recovers, without needing a new process restart or manual fix.
    BOT_CLEANUP_SWEEP_INTERVAL_SECONDS: int = int(os.getenv("BOT_CLEANUP_SWEEP_INTERVAL_SECONDS", "30"))
    BOT_CLEANUP_SWEEP_MAX_AGE_SECONDS: int = int(os.getenv("BOT_CLEANUP_SWEEP_MAX_AGE_SECONDS", "900"))

    # --- Interview behavior ---
    # No fixed duration: the interview runs until MAX_QUESTIONS is reached or the
    # candidate/interviewer naturally concludes it. MAX_QUESTIONS is a safety cap,
    # not a target.
    # A session's question count is chosen at random within this range
    # (inclusive) rather than always hitting the same fixed number, so
    # interview length varies session to session.
    MIN_QUESTIONS: int = int(os.getenv("MIN_QUESTIONS", "12"))
    MAX_QUESTIONS: int = int(os.getenv("MAX_QUESTIONS", "30"))
    MAX_SESSIONS: int = int(os.getenv("MAX_SESSIONS", "20"))
    MAX_ANSWER_SECONDS: int = int(os.getenv("MAX_ANSWER_SECONDS", "120"))

    # --- Email (sending the report to the candidate) ---
    # smtp  = any standard SMTP provider (Gmail, SES, company mail server...)
    # brevo = Brevo's transactional email REST API (https://www.brevo.com) —
    #         free tier is 300 emails/day, no SMTP App Password fiddling,
    #         just one API key.
    EMAIL_PROVIDER: str = os.getenv("EMAIL_PROVIDER", "smtp")  # smtp | brevo | resend | mailjet
    BREVO_API_KEY: str = os.getenv("BREVO_API_KEY", "")
    # Resend: genuinely free tier (3,000 emails/month, 100/day, no credit
    # card required) sent over a plain HTTPS API call -- unlike SMTP, this
    # isn't affected by Render's free-tier block on outbound SMTP ports
    # 25/465/587. NOTE: without a verified domain, Resend's sandbox sender
    # (onboarding@resend.dev) can only deliver to the Resend account's own
    # registered email -- not arbitrary recipients. Fine for testing,
    # not for sending real candidate reports without owning a domain.
    RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
    # Mailjet: free tier (6,000/month, 200/day), no credit card required.
    # Unlike Resend's sandbox, Mailjet auto-validates your signup email as
    # a sender and lets you send to any recipient without owning/verifying
    # a domain -- a better fit than Resend when you don't have a domain.
    MAILJET_API_KEY: str = os.getenv("MAILJET_API_KEY", "")
    MAILJET_API_SECRET: str = os.getenv("MAILJET_API_SECRET", "")
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_USE_TLS: bool = _bool("SMTP_USE_TLS", True)
    EMAIL_FROM: str = os.getenv("EMAIL_FROM", "")
    EMAIL_FROM_NAME: str = os.getenv("EMAIL_FROM_NAME", "Aperture AI Interviews")

    # --- Paths ---
    BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
    REPORTS_DIR: str = os.path.join(BASE_DIR, "reports")
    AUDIO_DIR: str = os.path.join(BASE_DIR, "audio")
    LOGS_DIR: str = os.path.join(BASE_DIR, "logs")

    # --- Redis (optional session/report durability layer) ---
    # If set, session status + JSON report are mirrored to Redis on every
    # update, so GET /api/status and GET /api/report (JSON) keep working
    # after a process restart or if the request lands on a different
    # worker than the one running the interview. This does NOT make the
    # live interview loop itself multi-worker-safe — the orchestrator's
    # background thread, the audio WebSocket bridge, and cancel_event are
    # still process-local, so /api/start and /api/end for a given session
    # must hit the same worker that's actually running it. Run a single
    # worker process (see README) rather than relying on this to paper
    # over multiple workers.
    # Free tier: Upstash Redis (https://upstash.com) gives 500K commands/
    # month and a standard rediss:// URL that works with any Redis client.
    REDIS_URL: str = os.getenv("REDIS_URL", "")
    REDIS_SESSION_TTL_SECONDS: int = int(os.getenv("REDIS_SESSION_TTL_SECONDS", "86400"))

    # --- Rate limiting ---
    RATE_LIMIT: str = os.getenv("RATE_LIMIT", "30/minute")

    # --- Startup validation (bugs #5, #28) ---
    # If true (default), the server refuses to start when required config
    # (provider keys, ffmpeg, etc.) is missing or clearly broken, instead of
    # discovering it mid-interview. Set to 0 for local iteration where you
    # want to boot with partial config.
    STARTUP_VALIDATION_STRICT: bool = _bool("STARTUP_VALIDATION_STRICT", True)


config = Config()

for _dir in (config.REPORTS_DIR, config.AUDIO_DIR, config.LOGS_DIR):
    os.makedirs(_dir, exist_ok=True)
