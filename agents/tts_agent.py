"""
agents/tts_agent.py
TTS Agent: text-to-speech provider abstraction. Supports fully-local
pyttsx3 (zero setup, robotic voice) and Edge-TTS (free, unlimited,
Microsoft's Edge read-aloud voices, no API key/account needed — much
better quality than pyttsx3 for very little extra setup).

Trimmed 2026-07: removed ElevenLabs, Google Cloud TTS, and Azure Speech
support — none were ever configured in this project's .env (all three
API keys were blank), and they pulled in heavyweight/paid SDKs
(google-cloud-texttospeech, azure-cognitiveservices-speech) for code
that never ran. Added EdgeTTSProvider instead as a genuinely free,
no-signup, no-quota upgrade over pyttsx3's voice quality.
"""
import asyncio
import os
import uuid
from abc import ABC, abstractmethod
from typing import Optional

from config import config
from utils.helpers import NESTED_LOOP_EXECUTOR
from utils.logger import get_logger

logger = get_logger("tts")

TTS_TIMEOUT_SECONDS = 30


class TTSError(Exception):
    """Raised when text-to-speech synthesis fails."""


class BaseTTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str, output_path: str) -> str:
        """Synthesize speech for `text`, write it to `output_path`, return the path."""


class LocalTTSProvider(BaseTTSProvider):
    """
    Fully local, offline TTS using pyttsx3 — no API key, no account
    signup, no usage limits, no internet connection required. On Windows
    this uses the built-in SAPI5 voices (works out of the box); on macOS
    it uses NSSpeechSynthesizer (also built in); on Linux it needs
    `espeak` installed (`sudo apt-get install espeak`). Voice quality is
    noticeably robotic — use TTS_PROVIDER=edge for something that sounds
    much more natural while still being free.
    """

    async def synthesize(self, text: str, output_path: str) -> str:
        import pyttsx3

        # pyttsx3 reliably writes WAV regardless of the requested filename's
        # extension, so force .wav here — the Meeting Agent's audio
        # conversion step (agents/meeting_agent.py) handles WAV or any
        # ffmpeg-readable format either way.
        wav_path = os.path.splitext(output_path)[0] + ".wav"
        # A fresh engine per call rather than a cached one — pyttsx3's
        # Windows backend (SAPI5) is COM-based and expects to stay on the
        # thread that created it, and this runs inside a thread-pool
        # executor where that isn't guaranteed call to call, so reusing one
        # engine instance across calls risks silently hanging.
        engine = pyttsx3.init()
        # pyttsx3's default rate (~200 wpm) reads as rushed/unnatural for a
        # spoken interview; ~165 wpm is closer to normal conversational
        # pace and easier to follow.
        try:
            default_rate = engine.getProperty("rate") or 200
            engine.setProperty("rate", min(default_rate, 165))
            engine.setProperty("volume", 1.0)
        except Exception:  # noqa: BLE001
            pass
        engine.save_to_file(text, wav_path)
        engine.runAndWait()
        return wav_path


class EdgeTTSProvider(BaseTTSProvider):
    """
    Microsoft Edge's "Read Aloud" neural voices via the unofficial
    `edge-tts` package — free, unlimited, no API key or account needed
    (it talks to the same public endpoint the Edge browser uses).
    Noticeably more natural than pyttsx3. Being unofficial, Microsoft
    could change/rate-limit that endpoint without notice, so it's not as
    rock-solid a guarantee as a paid vendor SLA — but it's a strong
    free default. Requires: pip install edge-tts
    """

    DEFAULT_VOICE = "en-US-GuyNeural"

    async def synthesize(self, text: str, output_path: str) -> str:
        import edge_tts

        mp3_path = os.path.splitext(output_path)[0] + ".mp3"
        communicate = edge_tts.Communicate(text, self.DEFAULT_VOICE)
        await communicate.save(mp3_path)
        return mp3_path


_PROVIDERS = {
    "local": LocalTTSProvider,
    "edge": EdgeTTSProvider,
}


class TTSAgent:
    """Facade used by the rest of the app. Picks the provider from config."""

    def __init__(self, provider: Optional[str] = None):
        provider_name = (provider or config.TTS_PROVIDER).lower()
        if provider_name not in _PROVIDERS:
            raise TTSError(
                f"Unsupported TTS_PROVIDER: {provider_name}. This build only supports: "
                f"{', '.join(_PROVIDERS)}."
            )
        self.provider_name = provider_name
        self._impl = _PROVIDERS[provider_name]()

    async def synthesize(self, text: str, filename: Optional[str] = None) -> str:
        filename = filename or f"{uuid.uuid4().hex}.mp3"
        output_path = os.path.join(config.AUDIO_DIR, filename)

        async def _run_async() -> str:
            return await self._impl.synthesize(text, output_path)

        def _run_sync() -> str:
            # pyttsx3 is synchronous under the hood despite the async def
            # wrapper (no real internal await points); edge-tts is truly
            # async. Either way, running through a thread executor lets
            # asyncio.wait_for's timeout actually take effect — otherwise a
            # hung call would block the whole interview loop indefinitely
            # with no way to cancel it.
            return asyncio.run(_run_async())

        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(NESTED_LOOP_EXECUTOR, _run_sync), timeout=TTS_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            logger.error("TTS (%s) timed out after %ds", self.provider_name, TTS_TIMEOUT_SECONDS)
            raise TTSError(f"TTS call timed out after {TTS_TIMEOUT_SECONDS}s") from exc
        except Exception as exc:  # noqa: BLE001
            logger.error("TTS (%s) failed: %s", self.provider_name, exc)
            raise TTSError(str(exc)) from exc
