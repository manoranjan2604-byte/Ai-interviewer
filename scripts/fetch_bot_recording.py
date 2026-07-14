"""
scripts/fetch_bot_recording.py
Standalone debug helper: fetches a bot's status/recording from Meeting BaaS
directly, so you can confirm from the actual meeting recording whether the
interviewer's voice was audible — independent of anything our own server
logs claim happened.

Usage:
    python scripts/fetch_bot_recording.py <bot_id>

The bot_id is logged by meeting_agent.py at join time, e.g.:
    "Meeting BaaS bot 4864f744-64d3-4a36-ab10-69c9bc2eca52 created for ..."

Note: Meeting BaaS needs a little time after the bot leaves to finish
processing the recording. If `audio`/`video` come back null, wait a minute
and try again.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.meetingbaas_client import MeetingBaaSError, get_bot_status  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    bot_id = sys.argv[1]
    try:
        status = get_bot_status(bot_id)
    except MeetingBaaSError as exc:
        print(f"Could not fetch bot status: {exc}")
        sys.exit(1)

    data = status.get("data", status)
    print(json.dumps(data, indent=2))

    audio_url = data.get("audio")
    video_url = data.get("video")
    if audio_url:
        print(f"\nAudio recording URL (this is what the meeting actually heard):\n{audio_url}")
    elif video_url:
        print(f"\nVideo recording URL:\n{video_url}")
    else:
        print(
            "\nNo recording URL yet. Meeting BaaS may still be processing it "
            "— wait a minute after the bot leaves and try again."
        )


if __name__ == "__main__":
    main()
