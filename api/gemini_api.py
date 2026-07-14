"""
api/gemini_api.py
Thin LLM client abstraction. Cerebras is the primary provider, Groq is the
automatic fallback — both are OpenAI-compatible APIs with generous free
tiers, so the rest of the app only ever calls `LLMClient`, never a vendor
SDK directly.
(Module kept as gemini_api.py for import-path stability; rename freely if
you'd rather it match the actual provider.)

Trimmed 2026-07: this used to also support Gemini and OpenAI as
selectable providers. Removed to cut dead code + unused dependencies
(google-generativeai, the extra openai-key path) since this project
only ever ran on Cerebras/Groq. If you want Gemini or OpenAI back, the
per-provider methods are simple to re-add.
"""
import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional

from config import config
from utils.logger import get_logger

logger = get_logger("llm")

LLM_TIMEOUT_SECONDS = config.LLM_TIMEOUT_SECONDS

_QUOTA_ERROR_MARKERS = (
    "429", "quota", "rate limit", "rate_limit", "resourceexhausted",
    "resource_exhausted", "exceeded your current quota",
)


def _is_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _QUOTA_ERROR_MARKERS)


class _ProviderBreaker:
    """Module-level (process-wide) circuit breaker per provider.

    Without this, every single question/evaluation call during an
    interview would re-attempt a provider whose daily quota is already
    exhausted, burning LLM_TIMEOUT_SECONDS (or a fast-fail round trip)
    on every turn for no benefit. Once a provider fails with a
    quota-shaped error LLM_QUOTA_BREAKER_THRESHOLD times in a row, calls
    to it are skipped outright (instant LLMError) for
    LLM_QUOTA_BREAKER_SECONDS, so the caller falls straight through to
    the fallback provider or the canned/neutral response instead of
    waiting.
    """

    def __init__(self) -> None:
        self._consecutive_failures: Dict[str, int] = {}
        self._open_until: Dict[str, float] = {}

    def is_open(self, provider: str) -> bool:
        until = self._open_until.get(provider)
        return bool(until and time.monotonic() < until)

    def record_success(self, provider: str) -> None:
        self._consecutive_failures[provider] = 0
        self._open_until.pop(provider, None)

    def record_failure(self, provider: str, exc: Exception) -> None:
        if not _is_quota_error(exc):
            return
        count = self._consecutive_failures.get(provider, 0) + 1
        self._consecutive_failures[provider] = count
        if count >= config.LLM_QUOTA_BREAKER_THRESHOLD:
            self._open_until[provider] = time.monotonic() + config.LLM_QUOTA_BREAKER_SECONDS
            logger.error(
                "LLM provider '%s' hit a quota/rate-limit error %d times in a row; "
                "pausing calls to it for %ds (falling back instead) rather than "
                "continuing to retry a doomed provider.",
                provider, count, config.LLM_QUOTA_BREAKER_SECONDS,
            )


_breaker = _ProviderBreaker()


class LLMError(Exception):
    """Raised when the LLM call fails or returns unusable content."""


class LLMClient:
    """
    Provider-agnostic LLM client (Cerebras primary, Groq fallback).

    Usage:
        client = LLMClient()
        text = await client.generate("Write a question about recursion.")
        data = await client.generate_json("Return JSON with fields a, b.")
    """

    _SUPPORTED = ("cerebras", "groq")

    def __init__(self, provider: Optional[str] = None):
        self.provider = (provider or config.LLM_PROVIDER).lower()
        self.fallback_provider = (config.LLM_FALLBACK_PROVIDER or "").lower() or None
        if self.fallback_provider == self.provider:
            self.fallback_provider = None
        self._groq_client = None
        self._cerebras_client = None
        self._init_client(self.provider)
        if self.fallback_provider:
            self._init_client(self.fallback_provider)

    def _init_client(self, provider: str) -> None:
        if provider == "groq":
            try:
                from openai import OpenAI  # Groq exposes an OpenAI-compatible endpoint

                self._groq_client = (
                    OpenAI(api_key=config.GROQ_API_KEY, base_url=config.GROQ_BASE_URL)
                    if config.GROQ_API_KEY
                    else None
                )
                if not config.GROQ_API_KEY:
                    logger.warning("GROQ_API_KEY is not set. Groq LLM calls will fail until configured.")
            except ImportError:
                logger.error("openai package not installed (required for the Groq client too).")
                self._groq_client = None
        elif provider == "cerebras":
            try:
                from openai import OpenAI  # Cerebras exposes an OpenAI-compatible endpoint

                self._cerebras_client = (
                    OpenAI(api_key=config.CEREBRAS_API_KEY, base_url=config.CEREBRAS_BASE_URL)
                    if config.CEREBRAS_API_KEY
                    else None
                )
                if not config.CEREBRAS_API_KEY:
                    logger.warning("CEREBRAS_API_KEY is not set. Cerebras LLM calls will fail until configured.")
            except ImportError:
                logger.error("openai package not installed (required for the Cerebras client too).")
                self._cerebras_client = None
        else:
            raise LLMError(
                f"Unsupported LLM provider: {provider}. This build only supports: "
                f"{', '.join(self._SUPPORTED)}."
            )

    async def generate(self, prompt: str, system: Optional[str] = None, temperature: float = 0.7) -> str:
        """Generate free-form text from the configured LLM provider, with a hard
        timeout, a per-provider quota circuit breaker, and an automatic
        fallback provider (config.LLM_FALLBACK_PROVIDER) if the primary one
        is currently failing/exhausted."""
        try:
            return await self._generate_via(self.provider, prompt, system, temperature)
        except LLMError as primary_exc:
            if not self.fallback_provider:
                raise
            logger.warning(
                "Primary LLM provider '%s' failed (%s); trying fallback provider '%s'.",
                self.provider, primary_exc, self.fallback_provider,
            )
            return await self._generate_via(self.fallback_provider, prompt, system, temperature)

    async def _generate_via(
        self, provider: str, prompt: str, system: Optional[str], temperature: float
    ) -> str:
        if _breaker.is_open(provider):
            raise LLMError(f"LLM provider '{provider}' is temporarily paused after repeated quota errors.")
        try:
            if provider == "groq":
                text = await asyncio.wait_for(
                    self._generate_groq(prompt, system, temperature), timeout=LLM_TIMEOUT_SECONDS
                )
            elif provider == "cerebras":
                text = await asyncio.wait_for(
                    self._generate_cerebras(prompt, system, temperature), timeout=LLM_TIMEOUT_SECONDS
                )
            else:
                raise LLMError(f"Unsupported LLM provider: {provider}")
            _breaker.record_success(provider)
            return text
        except asyncio.TimeoutError as exc:
            logger.error("LLM generate() via '%s' timed out after %ds", provider, LLM_TIMEOUT_SECONDS)
            err = LLMError(f"LLM call to '{provider}' timed out after {LLM_TIMEOUT_SECONDS}s")
            _breaker.record_failure(provider, err)
            raise err from exc
        except Exception as exc:  # noqa: BLE001
            logger.error("LLM generate() via '%s' failed: %s", provider, exc)
            _breaker.record_failure(provider, exc)
            raise LLMError(str(exc)) from exc

    async def generate_json(self, prompt: str, system: Optional[str] = None, temperature: float = 0.3) -> Dict[str, Any]:
        """Generate a response and parse it as JSON, stripping markdown fences if present."""
        full_prompt = (
            prompt
            + "\n\nRespond with ONLY valid JSON. No markdown fences, no commentary, no preamble."
        )
        raw = await self.generate(full_prompt, system=system, temperature=temperature)
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> Dict[str, Any]:
        cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError as exc:
                    raise LLMError(f"Could not parse LLM JSON output: {exc}") from exc
            raise LLMError("LLM did not return parseable JSON.")

    async def _generate_groq(self, prompt: str, system: Optional[str], temperature: float) -> str:
        if not self._groq_client:
            raise LLMError("Groq client not initialized (missing 'openai' package or GROQ_API_KEY).")
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        def _blocking_call():
            return self._groq_client.chat.completions.create(
                model=config.GROQ_MODEL,
                messages=messages,
                temperature=temperature,
            )

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _blocking_call)
        content = response.choices[0].message.content
        if not content:
            raise LLMError("Empty response from Groq.")
        return content

    async def _generate_cerebras(self, prompt: str, system: Optional[str], temperature: float) -> str:
        if not self._cerebras_client:
            raise LLMError("Cerebras client not initialized (missing 'openai' package or CEREBRAS_API_KEY).")
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        def _blocking_call():
            return self._cerebras_client.chat.completions.create(
                model=config.CEREBRAS_MODEL,
                messages=messages,
                temperature=temperature,
            )

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _blocking_call)
        content = response.choices[0].message.content
        if not content:
            raise LLMError("Empty response from Cerebras.")
        return content
