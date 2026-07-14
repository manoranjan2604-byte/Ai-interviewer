"""
agents/monitor_agent.py
Monitor Agent: watches the health of the live call and audio pipeline
while an interview is running, and surfaces actionable warnings — e.g.
no audio ever received (streaming likely broken), TTS/STT failing
repeatedly, or extended cross-talk from non-candidate participants that
could distort turn-detection.

This is intentionally lightweight (no external calls) — it just tracks
counters the rest of the pipeline reports in, and exposes checks other
agents can query. Warnings are logged and also attached to the session
record so they're visible in status polling / the final report.
"""
from dataclasses import dataclass, field
from typing import List

from utils.logger import get_logger

logger = get_logger("interview")

MIN_BYTES_FOR_HEALTHY_TURN = 3200  # ~100ms of PCM16/16kHz audio


@dataclass
class MonitorState:
    session_id: str
    bytes_played_total: int = 0
    bytes_received_total: int = 0
    tts_failures: int = 0
    stt_failures: int = 0
    zero_audio_turns: int = 0
    cross_talk_turns: int = 0
    warnings: List[str] = field(default_factory=list)


class MonitorAgent:
    def __init__(self):
        self._states = {}

    def _state(self, session_id: str) -> MonitorState:
        if session_id not in self._states:
            self._states[session_id] = MonitorState(session_id=session_id)
        return self._states[session_id]

    def record_playback(self, session_id: str, bytes_played: int) -> None:
        state = self._state(session_id)
        state.bytes_played_total += bytes_played

    def record_capture(self, session_id: str, bytes_received: int, had_cross_talk: bool = False) -> None:
        state = self._state(session_id)
        if bytes_received < MIN_BYTES_FOR_HEALTHY_TURN:
            state.zero_audio_turns += 1
            if state.zero_audio_turns >= 3:
                self._warn(
                    session_id,
                    "No meaningful audio captured for several consecutive questions — "
                    "check the Meeting BaaS output stream connection.",
                )
        else:
            state.bytes_received_total += bytes_received
        if had_cross_talk:
            state.cross_talk_turns += 1

    def record_tts_failure(self, session_id: str) -> None:
        state = self._state(session_id)
        state.tts_failures += 1
        if state.tts_failures >= 3:
            self._warn(session_id, "TTS has failed repeatedly — the interview may be running text-only.")

    def record_stt_failure(self, session_id: str) -> None:
        state = self._state(session_id)
        state.stt_failures += 1
        if state.stt_failures >= 3:
            self._warn(session_id, "STT has failed repeatedly — candidate answers may not be transcribed.")

    def _warn(self, session_id: str, message: str) -> None:
        state = self._state(session_id)
        if message not in state.warnings:
            state.warnings.append(message)
            logger.warning("[monitor:%s] %s", session_id, message)

    def get_warnings(self, session_id: str) -> List[str]:
        return list(self._state(session_id).warnings)

    def summary(self, session_id: str) -> dict:
        state = self._state(session_id)
        return {
            "bytes_played_total": state.bytes_played_total,
            "bytes_received_total": state.bytes_received_total,
            "tts_failures": state.tts_failures,
            "stt_failures": state.stt_failures,
            "zero_audio_turns": state.zero_audio_turns,
            "cross_talk_turns": state.cross_talk_turns,
            "warnings": state.warnings,
        }


monitor_agent = MonitorAgent()
