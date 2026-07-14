"""
agents/memory_agent.py
Memory Agent: holds structured facts learned about the candidate over
the course of the interview (role, experience level, notable strengths/
gaps mentioned in answers, topics already covered) so the Reasoning and
Interview agents can make better decisions than re-reading the raw
transcript every time.

This is intentionally simple — an in-memory per-session store, not a
vector DB — since the interview is a single bounded conversation. If you
later want memory to persist *across* interviews for the same candidate,
swap the internal dict for a real datastore keyed by candidate email.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class CandidateMemory:
    session_id: str
    role: Optional[str] = None
    experience_level: Optional[str] = None
    topics_covered: List[str] = field(default_factory=list)
    notable_strengths: List[str] = field(default_factory=list)
    notable_gaps: List[str] = field(default_factory=list)
    running_score_trend: List[float] = field(default_factory=list)


class MemoryAgent:
    def __init__(self):
        self._memory: Dict[str, CandidateMemory] = {}

    def _get(self, session_id: str) -> CandidateMemory:
        if session_id not in self._memory:
            self._memory[session_id] = CandidateMemory(session_id=session_id)
        return self._memory[session_id]

    def set_profile(self, session_id: str, role: str, experience_level: str) -> None:
        mem = self._get(session_id)
        mem.role = role
        mem.experience_level = experience_level

    def record_answer(self, session_id: str, category: str, score: float, evaluation: dict) -> None:
        mem = self._get(session_id)
        if category not in mem.topics_covered:
            mem.topics_covered.append(category)
        mem.running_score_trend.append(score)
        for strength in evaluation.get("strengths", []) or []:
            if strength not in mem.notable_strengths:
                mem.notable_strengths.append(strength)
        for weakness in evaluation.get("weaknesses", []) or []:
            if weakness not in mem.notable_gaps:
                mem.notable_gaps.append(weakness)

    def get_context_summary(self, session_id: str) -> str:
        """A compact text summary suitable for injecting into an LLM prompt."""
        mem = self._get(session_id)
        if not mem.topics_covered:
            return "No prior context yet — this is the start of the interview."
        trend = mem.running_score_trend[-3:]
        parts = [
            f"Role: {mem.role or 'unknown'} ({mem.experience_level or 'unknown'})",
            f"Topics already covered: {', '.join(mem.topics_covered)}",
        ]
        if trend:
            parts.append(f"Recent scores: {', '.join(f'{s:.1f}' for s in trend)}")
        if mem.notable_strengths:
            parts.append(f"Observed strengths so far: {', '.join(mem.notable_strengths[:5])}")
        if mem.notable_gaps:
            parts.append(f"Observed gaps so far: {', '.join(mem.notable_gaps[:5])}")
        return "\n".join(parts)

    def clear(self, session_id: str) -> None:
        self._memory.pop(session_id, None)


memory_agent = MemoryAgent()
