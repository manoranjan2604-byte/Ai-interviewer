"""
agents/audio_agent.py
Audio Agent: owns the real-time audio channels bridging Meeting BaaS's
WebSocket streams with the rest of the pipeline. One channel per active
bot, holding:

  - `to_meeting`  queue: raw PCM16/16kHz bytes we want played into the call
                  (synthesized speech). The input WebSocket handler
                  (routes/audio_ws_routes.py) drains this and forwards it
                  to Meeting BaaS.
  - `from_meeting` queue: raw PCM16/16kHz bytes received from the call
                  (participants' voices). The output WebSocket handler
                  fills this as Meeting BaaS streams audio to us.
  - per-participant speaking state, from Meeting BaaS's speaker-state
    messages — lets the Meeting Agent detect when the *candidate
    specifically* stops talking, rather than only when the whole mixed
    stream goes quiet (which may never happen if other participants are
    present and talking).
"""
import queue
import threading
from typing import Dict, List, Optional


class BotAudioChannel:
    def __init__(self):
        self.to_meeting: "queue.Queue[bytes]" = queue.Queue()
        self.from_meeting: "queue.Queue[bytes]" = queue.Queue()
        self.connected_event = threading.Event()
        self.speaker_speaking: Dict[str, bool] = {}
        self.speaker_lock = threading.Lock()

    def update_speaker_states(self, speakers: List[dict]) -> None:
        with self.speaker_lock:
            for entry in speakers:
                name = entry.get("name")
                if name:
                    self.speaker_speaking[name] = bool(entry.get("isSpeaking"))

    def is_speaking(self, name: str) -> Optional[bool]:
        """Returns True/False if we have data for this participant name, None if unknown."""
        with self.speaker_lock:
            name_lower = (name or "").strip().lower()
            for known_name, speaking in self.speaker_speaking.items():
                known_lower = known_name.strip().lower()
                if known_lower == name_lower or name_lower in known_lower:
                    return speaking
            return None

    def anyone_else_speaking(self, exclude_name: str) -> bool:
        """True if any participant other than exclude_name is currently speaking."""
        with self.speaker_lock:
            exclude_lower = (exclude_name or "").strip().lower()
            return any(
                speaking
                for name, speaking in self.speaker_speaking.items()
                if exclude_lower not in name.strip().lower()
            )

    def has_any_speaker_data(self) -> bool:
        """True once we've received at least one speaker-state message at
        all, regardless of who it's about. Used to tell "nobody's talking
        right now" apart from "Meeting BaaS never sent us this data" -- the
        latter needs a different (audio-energy-based) fallback."""
        with self.speaker_lock:
            return bool(self.speaker_speaking)


class AudioAgent:
    """Registry of active per-bot audio channels."""

    def __init__(self):
        self._channels: Dict[str, BotAudioChannel] = {}
        self._lock = threading.RLock()

    def register(self, stream_id: str) -> BotAudioChannel:
        with self._lock:
            channel = BotAudioChannel()
            self._channels[stream_id] = channel
            return channel

    def get(self, stream_id: str) -> Optional[BotAudioChannel]:
        with self._lock:
            return self._channels.get(stream_id)

    def unregister(self, stream_id: str) -> None:
        with self._lock:
            self._channels.pop(stream_id, None)


audio_agent = AudioAgent()
