"""
interview/session.py
In-memory interview session state and a thread-safe session store.
For a single-process deployment this dict-based store is sufficient;
swap SessionStore's internals for Redis if you need multi-worker sharing.
"""
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import config
from utils.helpers import new_session_id, now_iso, seconds_between
from utils import redis_store


@dataclass
class QARecord:
    question: str
    category: str
    answer: str = ""
    evaluation: Optional[Dict[str, Any]] = None
    score: Optional[float] = None
    asked_at: str = field(default_factory=now_iso)
    answered_at: Optional[str] = None

    def time_taken_seconds(self) -> Optional[float]:
        if not self.answered_at:
            return None
        return round(seconds_between(self.asked_at, self.answered_at), 1)


@dataclass
class InterviewSession:
    session_id: str
    name: str
    email: str
    meet_link: str
    # role/experience_level start unset and are filled in during the call,
    # once the AI interviewer asks the candidate directly.
    role: Optional[str] = None
    experience_level: Optional[str] = None
    status: str = "initializing"  # initializing|joining|in_progress|completed|failed|ended
    bot_status: str = "not_started"  # not_started|joining|joined|failed|left|leave_failed
    meeting_joined: bool = False
    # Meeting BaaS bot id, set once the bot successfully joins. Lets
    # /api/end remove the bot from the call immediately, rather than only
    # waiting for the background interview loop to notice cancellation.
    bot_id: Optional[str] = None
    question_number: int = 0
    question_limit: int = config.MAX_QUESTIONS
    qa_records: List[QARecord] = field(default_factory=list)
    current_score: float = 0.0
    error_message: Optional[str] = None
    start_time: str = field(default_factory=now_iso)
    end_time: Optional[str] = None
    report_path: Optional[str] = None
    report_json: Optional[Dict[str, Any]] = None
    email_sent: bool = False
    email_error: Optional[str] = None
    # Set by /api/end to signal the running interview loop (in its own
    # background thread) to stop cooperatively — checked between steps
    # rather than killing the thread outright, so cleanup (leaving the
    # meeting, generating whatever report is possible) still happens.
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def to_public_dict(self) -> Dict[str, Any]:
        """Serialize the parts of session state that are safe/useful for the frontend."""
        return {
            "session_id": self.session_id,
            "name": self.name,
            "role": self.role,
            "experience_level": self.experience_level,
            "status": self.status,
            "bot_status": self.bot_status,
            "meeting_joined": self.meeting_joined,
            "question_number": self.question_number,
            "question_limit": self.question_limit,
            "current_score": round(self.current_score, 2),
            "current_question": self.qa_records[-1].question if self.qa_records else None,
            "error_message": self.error_message,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "report_ready": self.report_path is not None,
            "email_sent": self.email_sent,
            "email_error": self.email_error,
        }


class SessionStore:
    """Thread-safe in-memory store for active/completed interview sessions,
    with a best-effort Redis mirror (see utils/redis_store.py) so status
    and JSON report data survive a restart or a request landing on a
    different worker. The live orchestrator thread / audio bridge stay
    process-local regardless — only status_dict()/report_dict() below
    fall back to Redis, not full session control (e.g. /api/end)."""

    def __init__(self):
        self._sessions: Dict[str, InterviewSession] = {}
        self._lock = threading.RLock()

    def _mirror(self, session: InterviewSession) -> None:
        payload = session.to_public_dict()
        if session.report_json:
            payload["report_json"] = session.report_json
        redis_store.save_session(session.session_id, payload)

    def create(self, **kwargs) -> InterviewSession:
        with self._lock:
            if len(self._sessions) >= config.MAX_SESSIONS:
                raise RuntimeError("Maximum concurrent interview sessions reached. Try again shortly.")
            session_id = new_session_id()
            session = InterviewSession(session_id=session_id, **kwargs)
            self._sessions[session_id] = session
        self._mirror(session)
        return session

    def get(self, session_id: str) -> Optional[InterviewSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def update(self, session_id: str, **fields) -> Optional[InterviewSession]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            for key, value in fields.items():
                setattr(session, key, value)
        self._mirror(session)
        return session

    def status_dict(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Status lookup for the /api/status route: in-memory first, then
        the Redis mirror (survives restarts / other workers) as a
        fallback."""
        session = self.get(session_id)
        if session:
            return session.to_public_dict()
        return redis_store.load_session(session_id)

    def report_dict(self, session_id: str) -> "tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]":
        """Report lookup for the /api/report route. Returns
        (report_json, report_path, status). report_path is only ever
        populated from the in-memory/local-disk session — the PDF file
        itself isn't mirrored to Redis, so it's unavailable after a
        restart or from a different worker than the one that generated
        it (this is a real limitation on platforms with ephemeral disks
        — see README)."""
        session = self.get(session_id)
        if session:
            return session.report_json, session.report_path, session.status
        cached = redis_store.load_session(session_id)
        if cached:
            return cached.get("report_json"), None, cached.get("status")
        return None, None, None

    def exists_active_for_meet_link(self, meet_link: str) -> bool:
        with self._lock:
            return any(
                s.meet_link == meet_link and s.status in ("initializing", "joining", "in_progress")
                for s in self._sessions.values()
            )

    def all(self) -> List[InterviewSession]:
        with self._lock:
            return list(self._sessions.values())


session_store = SessionStore()
