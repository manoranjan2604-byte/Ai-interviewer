"""
agents/evaluation_agent.py
Evaluation Agent: evaluates a candidate's transcribed answer using the LLM,
returning structured scoring + feedback.
"""
from typing import Any, Dict, Optional

from api.gemini_api import LLMClient
from utils.helpers import clamp
from utils.logger import get_logger

logger = get_logger("interview")

EVAL_SYSTEM_PROMPT = (
    "You are an expert, fair technical interview evaluator. You score answers "
    "objectively based on correctness, clarity, and depth. You never invent "
    "claims about the candidate beyond what the transcript shows."
)


class EvaluationAgent:
    def __init__(self, role: Optional[str] = None, experience_level: Optional[str] = None):
        self.role = role or "General"
        self.experience_level = experience_level or "Mid-Level"
        self.llm = LLMClient()

    def update_profile(self, role: str, experience_level: str) -> None:
        self.role = role or self.role
        self.experience_level = experience_level or self.experience_level

    async def evaluate(self, question: str, category: str, transcript: str) -> Dict[str, Any]:
        """
        Returns a dict with: score (0-10), technical_score, communication_score,
        confidence_score, feedback, strengths (list), weaknesses (list).
        """
        if not transcript or not transcript.strip():
            return self._empty_answer_result()

        prompt = (
            f"Role: {self.role} ({self.experience_level})\n"
            f"Question category: {category}\n"
            f"Question asked: {question}\n"
            f"Candidate's transcribed answer: \"{transcript}\"\n\n"
            "Evaluate this answer and return JSON with exactly these fields:\n"
            "{\n"
            '  "score": <0-10 overall score>,\n'
            '  "technical_score": <0-10>,\n'
            '  "communication_score": <0-10, based on clarity/structure of the transcript>,\n'
            '  "confidence_score": <0-10, inferred only from transcript fluency and hedging '
            'language, not from voice or visual cues>,\n'
            '  "feedback": "<2-3 sentence constructive feedback>",\n'
            '  "strengths": ["<short strength>", ...],\n'
            '  "weaknesses": ["<short weakness>", ...]\n'
            "}"
        )
        try:
            result = await self.llm.generate_json(prompt, system=EVAL_SYSTEM_PROMPT)
            return self._normalize(result)
        except Exception as exc:  # noqa: BLE001
            logger.error("Answer evaluation failed, using neutral fallback: %s", exc)
            return self._fallback_result()

    @staticmethod
    def _normalize(result: Dict[str, Any]) -> Dict[str, Any]:
        def score(key: str) -> float:
            try:
                return clamp(float(result.get(key, 5)), 0, 10)
            except (TypeError, ValueError):
                return 5.0

        return {
            "score": score("score"),
            "technical_score": score("technical_score"),
            "communication_score": score("communication_score"),
            "confidence_score": score("confidence_score"),
            "feedback": str(result.get("feedback", "")).strip() or "No feedback generated.",
            "strengths": list(result.get("strengths", []))[:5],
            "weaknesses": list(result.get("weaknesses", []))[:5],
        }

    @staticmethod
    def _empty_answer_result() -> Dict[str, Any]:
        return {
            "score": 0.0,
            "technical_score": 0.0,
            "communication_score": 0.0,
            "confidence_score": 0.0,
            "feedback": "No answer was captured for this question.",
            "strengths": [],
            "weaknesses": ["No response provided."],
        }

    @staticmethod
    def _fallback_result() -> Dict[str, Any]:
        return {
            "score": 5.0,
            "technical_score": 5.0,
            "communication_score": 5.0,
            "confidence_score": 5.0,
            "feedback": "Automated evaluation was unavailable for this answer; a neutral score was applied.",
            "strengths": [],
            "weaknesses": [],
        }
