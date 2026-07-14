"""
routes/audio_ws_routes.py
WebSocket endpoints that Meeting BaaS's bot connects to for real-time
audio streaming, using flask-sock. Registered on the Flask app in app.py.

  /ws/audio/in/<bot_id>   Meeting BaaS PULLS audio from us here (our TTS
                          output to be played into the meeting).
  /ws/audio/out/<bot_id>  Meeting BaaS PUSHES audio to us here (the raw
                          meeting audio, i.e. the candidate speaking).

Per Meeting BaaS's documented streaming protocol, the output connection
first sends a JSON handshake ({"protocol_version", "bot_id", "sample_rate",
...}) as a text frame, then binary PCM16 audio chunks every ~100ms, with
JSON speaker-state updates (also text frames) interleaved whenever active
speakers change. Text and binary frames must be handled differently —
only binary frames are actual audio. Speaker-state updates are fed into
the Audio Agent so the Meeting Agent can do per-participant turn
detection instead of relying on whole-stream silence.

The input connection (us -> Meeting BaaS) is paced to real time: chunks
are sent roughly as fast as they'd naturally play (a ~100ms chunk every
~100ms), not dumped in a burst. Real-time audio ingestion on the
receiving side generally expects a steady drip-feed matching playback
rate; blasting a whole utterance over the socket in a few milliseconds is
a common cause of audio being silently dropped even though ws.send()
itself reports success.

Requires PUBLIC_BASE_URL to be a real, internet-reachable HTTPS/WSS
origin — see README "Meeting BaaS setup".
"""
import json
import queue
import time

from flask import Blueprint
from flask_sock import Sock

from agents.audio_agent import audio_agent
from utils.logger import get_logger

logger = get_logger("browser")

audio_ws_bp = Blueprint("audio_ws", __name__)
sock = Sock()

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # bytes (16-bit PCM)


def init_audio_ws(app):
    sock.init_app(app)

    @sock.route("/ws/audio/in/<bot_id>")
    def audio_in(ws, bot_id):
        """Meeting BaaS reads from this socket to get audio we want spoken into the call."""
        channel = audio_agent.get(bot_id)
        if not channel:
            logger.warning("audio_in: no channel registered for bot %s", bot_id)
            return
        logger.info("Meeting BaaS connected to input stream for bot %s", bot_id)

        # Mirror the handshake Meeting BaaS sends us on the output socket
        # (JSON text frame declaring protocol_version/sample_rate) before
        # any binary audio. Their documented protocol only describes this
        # for their side; sending our own declares the format we're about
        # to stream in case their input channel expects the same
        # negotiation before it'll treat binary frames as playable audio.
        try:
            ws.send(json.dumps({
                "protocol_version": 1,
                "bot_id": bot_id,
                "sample_rate": SAMPLE_RATE,
                "encoding": "pcm_s16le",
                "channels": 1,
            }))
        except Exception as exc:  # noqa: BLE001
            logger.warning("audio_in: failed to send handshake for bot %s: %s", bot_id, exc)

        try:
            stream_start = time.monotonic()
            bytes_sent = 0
            while True:
                try:
                    chunk = channel.to_meeting.get(timeout=1.0)
                except queue.Empty:
                    # Nothing queued right now -- reset the pacing clock so
                    # the next utterance starts fresh instead of the gap
                    # being counted as "behind schedule" and dumped in a burst.
                    stream_start = time.monotonic()
                    bytes_sent = 0
                    continue
                if chunk is None:  # sentinel: stream closing
                    break

                ws.send(chunk)
                bytes_sent += len(chunk)

                # Pace against an absolute schedule (stream start + total
                # bytes sent so far) rather than sleeping a fixed duration
                # per chunk. Relative per-chunk sleeps accumulate drift
                # whenever this thread gets delayed by anything else on the
                # box (e.g. CPU-bound local Whisper transcription running
                # concurrently) -- each late chunk pushes the next one later
                # too, and the audio arrives at Meeting BaaS later and
                # burstier each time, which is heard as breaking/stuttering
                # speech. Scheduling against elapsed-time-should-have-passed
                # instead means a delayed chunk gets sent immediately and
                # timing recovers on the next chunk rather than compounding.
                expected_elapsed = bytes_sent / (SAMPLE_RATE * SAMPLE_WIDTH)
                actual_elapsed = time.monotonic() - stream_start
                remaining = expected_elapsed - actual_elapsed
                if remaining > 0:
                    time.sleep(remaining)
        except Exception as exc:  # noqa: BLE001
            logger.info("audio_in stream closed for bot %s: %s", bot_id, exc)

    @sock.route("/ws/audio/out/<bot_id>")
    def audio_out(ws, bot_id):
        """
        Meeting BaaS pushes the meeting's live audio to us on this socket,
        interleaved with JSON text messages (handshake + speaker state).
        Only binary frames are audio; text frames must be parsed and
        handled separately, never queued as audio data.
        """
        channel = audio_agent.get(bot_id)
        if not channel:
            logger.warning("audio_out: no channel registered for bot %s", bot_id)
            return
        logger.info("Meeting BaaS connected to output stream for bot %s", bot_id)
        try:
            while True:
                data = ws.receive(timeout=5)
                if data is None:
                    continue

                if isinstance(data, str):
                    # JSON text frame: either the initial handshake or a
                    # speaker-state update. Never audio — do not queue it.
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        logger.debug("audio_out: non-JSON text frame for bot %s, ignoring", bot_id)
                        continue

                    if isinstance(parsed, dict) and "protocol_version" in parsed:
                        logger.info(
                            "Meeting BaaS handshake for bot %s: sample_rate=%s bot_id=%s",
                            bot_id, parsed.get("sample_rate"), parsed.get("bot_id"),
                        )
                        # Definitive confirmation that Meeting BaaS is actually
                        # streaming audio, not just that a socket opened.
                        channel.connected_event.set()
                    elif isinstance(parsed, list):
                        # Speaker-state update: feed into the Audio Agent so
                        # the Meeting Agent can do per-participant turn
                        # detection instead of relying on whole-stream silence.
                        channel.update_speaker_states(parsed)
                        logger.debug("Speaker state update for bot %s: %s", bot_id, parsed)
                    continue

                # Binary frame: raw PCM16 audio chunk (~100ms per the protocol).
                channel.from_meeting.put(data)
        except Exception as exc:  # noqa: BLE001
            logger.info("audio_out stream closed for bot %s: %s", bot_id, exc)
