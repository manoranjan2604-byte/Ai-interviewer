"""
agents/reasoning_agent.py
Reasoning Agent: decides *what* to ask about next — category, difficulty,
and whether the interview should wrap up early — using the Memory
Agent's context. This is deliberately separate from the Interview Agent,
which turns a plan into actual spoken question text: reasoning decides
strategy, interview agent handles phrasing.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agents.memory_agent import memory_agent
from utils.logger import get_logger

logger = get_logger("interview")

TECHNICAL_ROTATION = ["Technical", "Coding", "Problem Solving", "System Design", "Behavioral"]
HR_ROTATION = ["HR", "Behavioral"]


@dataclass
class QuestionPlan:
    category: str
    difficulty_hint: str
    should_conclude: bool = False
    conclude_reason: Optional[str] = None


class ReasoningAgent:
    def plan_next_question(
        self,
        session_id: str,
        question_number: int,
        role: str,
        history: List[Dict[str, Any]],
        running_avg_score: float,
        question_limit: int,
    ) -> QuestionPlan:
        # Early conclusion: candidate is scoring very poorly across several
        # questions in a row — no value in grinding through the full count.
        if len(history) >= 4:
            recent_scores = [h.get("score") for h in history[-4:] if h.get("score") is not None]
            if recent_scores and all(s is not None and s <= 2 for s in recent_scores):
                return QuestionPlan(
                    category="Wrap-up",
                    difficulty_hint="n/a",
                    should_conclude=True,
                    conclude_reason="Candidate scored very low on several consecutive questions.",
                )

        if question_number >= question_limit:
            return QuestionPlan(
                category="Wrap-up",
                difficulty_hint="n/a",
                should_conclude=True,
                conclude_reason="Reached the question limit.",
            )

        category = self._next_category(question_number, role)
        difficulty_hint = self._difficulty_for(running_avg_score) if history else "an appropriate opening difficulty"

        logger.debug(
            "[reasoning] session=%s q=%d category=%s difficulty=%s avg_score=%.1f",
            session_id, question_number, category, difficulty_hint, running_avg_score,
        )
        return QuestionPlan(category=category, difficulty_hint=difficulty_hint)

    def _next_category(self, question_number: int, role: str) -> str:
        rotation = HR_ROTATION if "hr" in (role or "").lower() else TECHNICAL_ROTATION
        return rotation[question_number % len(rotation)]

    def _difficulty_for(self, running_avg_score: float) -> str:
        if running_avg_score >= 8:
            return "harder than the previous question"
        if running_avg_score <= 4:
            return "easier and more foundational than the previous question"
        return "similar difficulty to the previous question"

    def context_summary(self, session_id: str) -> str:
        """Delegates to the Memory Agent for a compact prompt-ready summary."""
        return memory_agent.get_context_summary(session_id)


reasoning_agent = ReasoningAgent()
