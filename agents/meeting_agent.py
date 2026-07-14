"""
agents/meeting_agent.py
Meeting Agent: joins Google Meet via Meeting BaaS, plays synthesized
speech into the call, and captures the candidate's spoken responses.
    join() -> bool
    push_audio_file(path, text=None) -> float  (queues audio, returns playback duration in seconds)
    record_response(max_seconds) -> Optional[str audio path]
    leave() -> bool (True if Meeting BaaS confirmed the bot is out of the call)

Requires MEETINGBAAS_API_KEY and a public PUBLIC_BASE_URL (see README,
"Meeting BaaS setup") so Meeting BaaS can reach this server's webhook and
WebSocket endpoints.

Turn detection uses per-speaker state from the Audio Agent (Meeting BaaS
tells us who is currently talking) rather than only overall stream
silence — this matters because the meeting's audio is a single mixed
stream of every participant, so overall silence never happens if anyone
else in the call is talking.
"""
import array
import asyncio
import os
import threading
import time
import uuid
import wave
from typing import Callable, Optional

from agents.audio_agent import audio_agent
from agents.monitor_agent import monitor_agent
from api.meetingbaas_client import MeetingBaaSError, create_bot, get_bot_status, remove_bot
from config import config
from interview.session import session_store
from routes.webhook_routes import BOT_TO_SESSION
from utils.logger import get_logger

logger = get_logger("browser")

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # bytes (16-bit PCM)

StopCheck = Callable[[], bool]


def _no_stop() -> bool:
    return False


class MeetingAgent:
    def __init__(self, meet_link: str, display_name: str, session_id: Optional[str] = None, candidate_name: Optional[str] = None):
        self.meet_link = meet_link
        self.display_name = display_name
        self.candidate_name = candidate_name or "the candidate"
        self.session_id = session_id
        self.bot_id: Optional[str] = None
        self._stream_id: Optional[str] = None
        self._channel = None
        self._total_bytes_played = 0
        self._total_bytes_received = 0

    def _require_public_url(self) -> str:
        if not config.PUBLIC_BASE_URL:
            raise MeetingBaaSError(
                "PUBLIC_BASE_URL is not set. Meeting BaaS needs a public HTTPS URL "
                "to reach this server's webhook and audio streams (use ngrok for "
                "local dev). See README 'Meeting BaaS setup'."
            )
        return config.PUBLIC_BASE_URL.rstrip("/")

    async def join(self, should_stop: StopCheck = _no_stop) -> bool:
        try:
            base = self._require_public_url()
        except MeetingBaaSError as exc:
            logger.error(str(exc))
            return False

        stream_id = uuid.uuid4().hex
        self._channel = audio_agent.register(stream_id)

        ws_base = base.replace("https://", "wss://").replace("http://", "ws://")

        try:
            bot_id = create_bot(
                meeting_url=self.meet_link,
                bot_name=self.display_name,
                webhook_url=f"{base}/api/webhooks/meetingbaas",
                input_stream_url=f"{ws_base}/ws/audio/in/{stream_id}",
                output_stream_url=f"{ws_base}/ws/audio/out/{stream_id}",
                entry_message=f"Hello, I'm the AI interviewer here to speak with {self.candidate_name}.",
            )
        except MeetingBaaSError as exc:
            logger.error("Meeting BaaS join failed: %s", exc)
            audio_agent.unregister(stream_id)
            return False

        self.bot_id = bot_id
        self._stream_id = stream_id
        if self.session_id:
            BOT_TO_SESSION[bot_id] = self.session_id
            session_store.update(self.session_id, bot_id=bot_id)

        deadline = time.monotonic() + max(config.MEETINGBAAS_WAITING_ROOM_TIMEOUT, 60)
        while time.monotonic() < deadline:
            if should_stop():
                logger.info("Join cancelled for bot %s.", bot_id)
                return False

            try:
                status_data = await asyncio.to_thread(get_bot_status, bot_id)
                status = status_data.get("status") or status_data.get("data", {}).get("status")
                logger.info("Meeting BaaS bot %s status poll: %s (full response logged at DEBUG)", bot_id, status)
                logger.debug("Meeting BaaS bot %s full status payload: %s", bot_id, status_data)
                if status in ("in_call", "in_call_recording", "joined"):
                    logger.info(
                        "Meeting BaaS bot %s confirmed in the call (status=%s).", bot_id, status,
                    )
                    return True
                if status == "in_waiting_room":
                    logger.info("Meeting BaaS bot %s is in the waiting room; still waiting for admission.", bot_id)
                elif status in ("failed", "error", "call_ended", "ended", "removed"):
                    logger.error(
                        "Meeting BaaS bot %s reported status=%s while trying to join; giving up.",
                        bot_id, status,
                    )
                    return False
            except MeetingBaaSError as exc:
                logger.debug("Status poll failed (will retry): %s", exc)

            # NOTE: deliberately NOT using self._channel.connected_event as a
            # "joined" signal here. Meeting BaaS can open the audio WebSocket
            # and send its handshake before the bot is actually admitted into
            # the meeting (e.g. while it's still sitting in the waiting
            # room), so a connected socket is not proof of admission — only
            # the status poll reporting in_call/in_call_recording/joined is.
            # Using the socket as a fallback previously caused the bot to be
            # treated as joined while still stuck in the waiting room, which
            # then ran the entire interview into an empty room and delayed
            # leave() until that whole (pointless) interview timed itself
            # out — see the "connected its audio WebSocket before any
            # waiting-room status was observed" log line this replaces.
            if self._channel.connected_event.is_set():
                logger.debug(
                    "Meeting BaaS bot %s audio WebSocket is connected, but waiting for the "
                    "status poll to confirm actual admission before treating it as joined.",
                    bot_id,
                )

            await asyncio.sleep(3)

        logger.error("Meeting BaaS bot %s did not confirm joining within timeout.", bot_id)
        return False

    def push_audio_file(self, audio_path: str, text: Optional[str] = None) -> float:
        """Queues audio into the call and returns its playback duration in seconds
        (0.0 if nothing was queued), so callers can wait for speech to actually
        finish before starting to listen for a response."""
        if not self._channel:
            logger.warning("No audio channel available; cannot play audio into call.")
            return 0.0
        try:
            pcm_data = self._to_pcm16_16k_mono(audio_path)
            chunk_size = 3200  # ~100ms at 16kHz/16-bit mono
            for i in range(0, len(pcm_data), chunk_size):
                self._channel.to_meeting.put(pcm_data[i : i + chunk_size])
            self._total_bytes_played += len(pcm_data)
            if self.session_id:
                monitor_agent.record_playback(self.session_id, len(pcm_data))
            if text is not None:
                logger.info(
                    "Interviewer speaking (%d chars): queued %d bytes of audio for playback "
                    "(running total this session: %d bytes).",
                    len(text), len(pcm_data), self._total_bytes_played,
                )
            else:
                logger.info(
                    "Queued %d bytes of audio for playback (running total this session: %d bytes).",
                    len(pcm_data), self._total_bytes_played,
                )
            return len(pcm_data) / (SAMPLE_RATE * SAMPLE_WIDTH)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to stream audio file %s into call: %s", audio_path, exc)
            return 0.0

    @staticmethod
    def _to_pcm16_16k_mono(audio_path: str) -> bytes:
        if audio_path.lower().endswith(".wav"):
            try:
                with wave.open(audio_path, "rb") as wf:
                    if wf.getframerate() == SAMPLE_RATE and wf.getnchannels() == 1 and wf.getsampwidth() == SAMPLE_WIDTH:
                        return wf.readframes(wf.getnframes())
            except Exception:  # noqa: BLE001
                pass

        from pydub import AudioSegment  # requires ffmpeg on PATH

        segment = AudioSegment.from_file(audio_path)
        segment = segment.set_frame_rate(SAMPLE_RATE).set_channels(1).set_sample_width(SAMPLE_WIDTH)
        return segment.raw_data

    @staticmethod
    def _chunk_rms(chunk: bytes) -> float:
        """Root-mean-square amplitude of a PCM16 chunk, used as a raw
        energy-based voice-activity signal that doesn't depend on Meeting
        BaaS's speaker-state messages arriving/parsing correctly at all."""
        if not chunk or len(chunk) < 2:
            return 0.0
        samples = array.array("h")  # signed 16-bit
        usable_len = len(chunk) - (len(chunk) % 2)
        samples.frombytes(chunk[:usable_len])
        if not samples:
            return 0.0
        return (sum(s * s for s in samples) / len(samples)) ** 0.5

    async def record_response(self, max_seconds: float, should_stop: StopCheck = _no_stop) -> Optional[str]:
        if not self._channel:
            logger.warning("No audio channel available; cannot record candidate response.")
            return None

        # Minimum time the candidate must be quiet, in real wall-clock
        # seconds, before we treat their turn as over. This MUST be
        # measured against time.monotonic(), not a loop-iteration/poll
        # count: audio chunks stream in roughly every ~100ms whenever the
        # line is open (per Meeting BaaS's protocol), so the loop body can
        # run ~10x/second. A poll-count threshold (e.g. "3 quiet polls")
        # was firing after ~300ms of silence — cutting candidates off
        # mid-sentence on any brief pause. Using elapsed time instead
        # fixes that regardless of how fast chunks arrive.
        # 2.5s was still cutting candidates off mid-answer: it's common to
        # pause 2-3+ seconds mid-answer to think or recall a detail, and
        # the speaking-state signal itself has some lag flipping back to
        # "speaking" once they resume. 4s gives real thinking pauses room.
        SILENCE_CUTOFF_SECONDS = 4.0

        # Energy-based voice-activity floor. Only used when Meeting BaaS's
        # speaker-state messages never give us a usable signal (missing,
        # differently-shaped, or the candidate's name never matches a
        # roster entry) -- without this, a broken speaker-state feed means
        # candidate_ever_spoke never flips True and the loop silently
        # burns the entire max_seconds doing nothing, which looks to the
        # user exactly like the bot having frozen after their answer.
        VAD_RMS_THRESHOLD = 300  # empirically: PCM16 background noise floor is well below this

        frames = bytearray()
        deadline = time.monotonic() + max_seconds
        silence_chunk_count = 0
        had_cross_talk = False
        candidate_ever_spoke = False
        candidate_quiet_since: Optional[float] = None
        speaker_state_ever_seen = False
        vad_loud_recently = False
        vad_quiet_since: Optional[float] = None

        while time.monotonic() < deadline:
            if should_stop():
                logger.info("Recording cancelled (interview ended by user).")
                break

            try:
                chunk = self._channel.from_meeting.get(timeout=1.0)
                frames.extend(chunk)
                silence_chunk_count = 0
                if self._chunk_rms(chunk) >= VAD_RMS_THRESHOLD:
                    vad_loud_recently = True
                    vad_quiet_since = None
                elif vad_loud_recently:
                    now = time.monotonic()
                    if vad_quiet_since is None:
                        vad_quiet_since = now
            except Exception:  # noqa: BLE001 (queue.Empty)
                silence_chunk_count += 1
                if silence_chunk_count > 4 and len(frames) > 0:
                    break

            # Speaker-aware turn detection: if Meeting BaaS is telling us who's
            # talking, use the candidate's own state rather than raw stream
            # silence — the mixed stream may never go quiet if others are
            # present and talking.
            is_candidate_speaking = self._channel.is_speaking(self.candidate_name)
            if is_candidate_speaking is not None:
                speaker_state_ever_seen = True
            if is_candidate_speaking is None:
                # The candidate's typed name didn't match anyone in Meeting
                # BaaS's speaker roster (their Google account display name
                # is often different from what they typed on the form).
                # Fall back to "is anyone other than our own bot talking" —
                # correct for the common one-candidate interview case.
                is_candidate_speaking = self._channel.anyone_else_speaking(self.display_name)
                if self._channel.has_any_speaker_data():
                    speaker_state_ever_seen = True
            if self._channel.anyone_else_speaking(self.candidate_name):
                had_cross_talk = True

            if is_candidate_speaking:
                candidate_ever_spoke = True
                candidate_quiet_since = None
            elif candidate_ever_spoke and len(frames) > 0:
                now = time.monotonic()
                if candidate_quiet_since is None:
                    candidate_quiet_since = now
                elif now - candidate_quiet_since >= SILENCE_CUTOFF_SECONDS:
                    logger.info("Candidate appears to have finished their turn (speaker-state based).")
                    break

            # If Meeting BaaS never sent usable speaker-state data at all,
            # don't sit on max_seconds silently — fall back to plain audio
            # energy so a real answer still gets cut off at a sane point.
            if not speaker_state_ever_seen:
                if vad_loud_recently and not candidate_ever_spoke:
                    candidate_ever_spoke = True
                    logger.info(
                        "No usable speaker-state data from Meeting BaaS this turn; "
                        "falling back to raw audio-energy voice detection instead."
                    )
                if vad_loud_recently and vad_quiet_since is not None and len(frames) > 0:
                    if time.monotonic() - vad_quiet_since >= SILENCE_CUTOFF_SECONDS:
                        logger.info("Candidate appears to have finished their turn (audio-energy based).")
                        break

        if self.session_id:
            monitor_agent.record_capture(self.session_id, len(frames), had_cross_talk=had_cross_talk)

        if not frames:
            logger.warning(
                "No audio ever received from Meeting BaaS's output stream this turn (0 bytes)."
            )
            return None

        self._total_bytes_received += len(frames)
        logger.info(
            "Captured %d bytes of candidate audio this turn (running total: %d bytes).",
            len(frames),
            self._total_bytes_received,
        )

        output_path = os.path.join(config.AUDIO_DIR, f"response_{uuid.uuid4().hex}.wav")
        with wave.open(output_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(bytes(frames))

        return output_path

    async def leave(self) -> bool:
        removed = True
        if self.bot_id:
            try:
                removed = await asyncio.to_thread(remove_bot, self.bot_id)
            except RuntimeError as exc:
                # asyncio.to_thread() uses the running loop's default
                # executor. If anything upstream has left that executor
                # unusable, don't let bot removal -- the one call that
                # actually matters for cost/cleanup -- silently die with
                # it. Run it on a plain, independent thread instead so
                # removal always gets attempted regardless of the loop's
                # executor state.
                logger.warning(
                    "asyncio.to_thread() failed removing bot %s (%s); "
                    "retrying bot removal on a plain thread instead.",
                    self.bot_id, exc,
                )
                result_holder: dict = {}

                def _remove_sync() -> None:
                    try:
                        result_holder["removed"] = remove_bot(self.bot_id)
                    except Exception as inner_exc:  # noqa: BLE001
                        result_holder["error"] = inner_exc

                thread = threading.Thread(target=_remove_sync, daemon=True)
                thread.start()
                thread.join(timeout=30)
                if "error" in result_holder:
                    raise result_holder["error"]
                removed = result_holder.get("removed", False)
            BOT_TO_SESSION.pop(self.bot_id, None)
        if getattr(self, "_stream_id", None):
            audio_agent.unregister(self._stream_id)
        if removed:
            logger.info("Bot left the meeting (bot_id=%s).", self.bot_id)
        else:
            logger.error(
                "Meeting BaaS could not confirm bot %s left the meeting — "
                "it may still be connected to the call.", self.bot_id,
            )
        return removed
