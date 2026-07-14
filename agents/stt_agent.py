"""
agents/stt_agent.py
STT Agent: speech-to-text provider abstraction. Supports local Whisper
(no API key, CPU-bound and slow) and Groq's hosted Whisper endpoint
(free tier, GPU-fast, needs an API key you likely already have for the
LLM fallback provider) behind a single interface so the provider can be
swapped via config.STT_PROVIDER without touching callers.

Trimmed 2026-07: removed Google Speech-to-Text, Deepgram, and AssemblyAI
support — this project never actually configured any of them, and they
pulled in three extra heavyweight SDKs (google-cloud-speech,
deepgram-sdk, assemblyai) that added multiple GB to the install for
code that never ran. Added GroqWhisperProvider instead, since it uses
a key you likely already have (GROQ_API_KEY) and is dramatically
faster than local CPU Whisper for interview-length clips.
"""
import asyncio
import wave
from abc import ABC, abstractmethod
from typing import Optional

from config import config
from utils.helpers import NESTED_LOOP_EXECUTOR
from utils.logger import get_logger

logger = get_logger("speech")


class STTError(Exception):
    """Raised when speech-to-text transcription fails."""


def _audio_duration_seconds(audio_path: str) -> Optional[float]:
    """Best-effort WAV duration lookup, used only to size the transcription
    timeout. Returns None (caller falls back to the timeout floor) for
    anything that isn't a readable PCM WAV file."""
    try:
        with wave.open(audio_path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return frames / float(rate) if rate else None
    except Exception:  # noqa: BLE001
        return None


class BaseSTTProvider(ABC):
    @abstractmethod
    async def transcribe(self, audio_path: str) -> str:
        """Transcribe an audio file on disk to text."""


class WhisperProvider(BaseSTTProvider):
    """Local transcription using openai-whisper. No network call, no API
    key — but CPU-bound, so a ~60-90s answer can take well over a minute
    to transcribe on a slow machine (see WHISPER_MODEL sizing in config.py)."""

    def __init__(self):
        self._model = None

    def _load(self):
        if self._model is None:
            import whisper

            logger.info("Loading local Whisper model '%s'...", config.WHISPER_MODEL)
            self._model = whisper.load_model(config.WHISPER_MODEL)
        return self._model

    async def transcribe(self, audio_path: str) -> str:
        model = self._load()
        result = model.transcribe(
            audio_path,
            # Skip per-clip language auto-detection: it's unreliable on
            # short answer clips and was a source of garbled/wrong-language
            # transcriptions. Interviews are conducted in English; change
            # this if you need another language.
            language="en",
            # We're explicitly on CPU (see the FP16 warning this used to
            # print every call) — request FP32 directly instead of letting
            # Whisper fall back to it silently.
            fp16=False,
            # Each answer is an isolated clip with no real relationship to
            # the previous one; conditioning on prior text encourages
            # Whisper to repeat/hallucinate phrases across turns instead of
            # transcribing what was actually said this turn.
            condition_on_previous_text=False,
            # Nudges Whisper's decoder toward interview/technical
            # vocabulary (data structures, frameworks, acronyms) that it
            # otherwise tends to mis-transcribe as similar-sounding common
            # words on short, accented, or noisy clips.
            initial_prompt=(
                "This is a spoken answer in a professional job interview. "
                "It may include technical terms about software engineering, "
                "programming languages, frameworks, system design, and data structures."
            ),
        )
        return result.get("text", "").strip()


class GroqWhisperProvider(BaseSTTProvider):
    """Groq's hosted whisper-large-v3-turbo endpoint. Free tier as of
    writing, OpenAI-compatible audio API, and runs on Groq's LPUs — far
    faster than CPU-bound local Whisper for interview-length clips, at
    the cost of a network round trip and needing GROQ_API_KEY set.
    Get a key at https://console.groq.com/keys (same key already used
    for LLM_FALLBACK_PROVIDER=groq, if you have that configured)."""

    def __init__(self):
        self._client = None

    def _load(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=config.GROQ_API_KEY, base_url=config.GROQ_BASE_URL)
        return self._client

    async def transcribe(self, audio_path: str) -> str:
        if not config.GROQ_API_KEY:
            raise STTError("STT_PROVIDER=groq but GROQ_API_KEY is not set.")
        client = self._load()
        with open(audio_path, "rb") as f:
            transcription = client.audio.transcriptions.create(
                file=f,
                model="whisper-large-v3-turbo",
                language="en",
            )
        return (transcription.text or "").strip()


_PROVIDERS = {
    "whisper": WhisperProvider,
    "groq": GroqWhisperProvider,
}


class STTAgent:
    """Facade used by the rest of the app. Picks the provider from config."""

    def __init__(self, provider: Optional[str] = None):
        provider_name = (provider or config.STT_PROVIDER).lower()
        if provider_name not in _PROVIDERS:
            raise STTError(
                f"Unsupported STT_PROVIDER: {provider_name}. This build only supports: "
                f"{', '.join(_PROVIDERS)}."
            )
        self.provider_name = provider_name
        self._impl = _PROVIDERS[provider_name]()

    async def transcribe(self, audio_path: str) -> str:
        def _run_sync() -> str:
            # Provider implementations are synchronous under the hood, so
            # run in a thread so the timeout can actually take effect
            # rather than only reporting after the blocking call eventually
            # finishes on its own.
            return asyncio.run(self._impl.transcribe(audio_path))

        duration = _audio_duration_seconds(audio_path)
        if duration:
            timeout = min(
                config.STT_TIMEOUT_MAX_SECONDS,
                max(config.STT_TIMEOUT_MIN_SECONDS, duration * config.STT_TIMEOUT_SECONDS_PER_AUDIO_SECOND),
            )
        else:
            timeout = config.STT_TIMEOUT_MIN_SECONDS

        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(NESTED_LOOP_EXECUTOR, _run_sync), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            logger.error(
                "STT (%s) timed out after %.0fs (audio duration=%s)",
                self.provider_name, timeout, f"{duration:.1f}s" if duration else "unknown",
            )
            raise STTError(f"STT call timed out after {timeout:.0f}s") from exc
        except Exception as exc:  # noqa: BLE001
            logger.error("STT (%s) failed: %s", self.provider_name, exc)
            raise STTError(str(exc)) from exc
