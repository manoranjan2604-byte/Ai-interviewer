"""
agents/interview_agent.py
Interview Agent: turns a strategy decided by the Reasoning Agent into
actual spoken question text via the LLM, and handles the opening
role/experience capture. Category/difficulty selection lives in
agents/reasoning_agent.py — this module only handles phrasing.
"""
import random
from typing import Any, Dict, Optional

from agents.reasoning_agent import QuestionPlan, reasoning_agent
from api.gemini_api import LLMClient
from utils.logger import get_logger

logger = get_logger("interview")

SYSTEM_PROMPT = (
    "You are a professional, encouraging technical interviewer conducting a "
    "structured interview. You ask one clear, focused question at a time, "
    "adapted to the candidate's role, experience level, and performance so far."
)

# Multiple options per category so that if the LLM call fails (or keeps
# failing across a whole session), the candidate doesn't hear the exact
# same sentence every time that category comes back around in the
# rotation. generate_question() also filters out anything already asked
# this session before picking one.
FALLBACK_QUESTIONS = {
    "Technical": [
        "Can you explain the difference between a process and a thread?",
        "What's the difference between an abstract class and an interface?",
        "Can you explain how a hash map works internally?",
        "What happens, at a high level, when you make an HTTP request to a web server?",
    ],
    "Coding": [
        "How would you reverse a linked list, and what's the time complexity?",
        "How would you find the first non-repeating character in a string?",
        "How would you detect a cycle in a linked list?",
        "How would you find two numbers in an array that add up to a target sum?",
    ],
    "Problem Solving": [
        "Walk me through how you would debug a slow API endpoint in production.",
        "How would you approach diagnosing a memory leak in a running service?",
        "How would you troubleshoot a service that's intermittently timing out?",
        "How would you plan a large database migration with zero downtime?",
    ],
    "Behavioral": [
        "Tell me about a time you disagreed with a teammate and how you resolved it.",
        "Tell me about a time you had to meet a tight deadline. How did you handle it?",
        "Describe a situation where you made a mistake at work. What did you do next?",
        "Tell me about a time you had to learn something new quickly for a project.",
    ],
    "HR": [
        "Why are you interested in this role, and what are you looking for next in your career?",
        "What motivates you in your day-to-day work?",
        "Where do you see yourself professionally in the next few years?",
        "What kind of work environment helps you do your best work?",
    ],
    "System Design": [
        "How would you design a URL shortening service at a high level?",
        "How would you design a rate limiter for an API?",
        "How would you design a simple notification system that supports email and push?",
        "How would you design a basic file storage service like a mini Dropbox?",
    ],
}


class InterviewAgent:
    def __init__(self, role: Optional[str] = None, experience_level: Optional[str] = None):
        self.role = role or "General"
        self.experience_level = experience_level or "Mid-Level"
        self.llm = LLMClient()

    def update_profile(self, role: str, experience_level: str) -> None:
        self.role = role or self.role
        self.experience_level = experience_level or self.experience_level

    async def generate_intro(self, candidate_name: str) -> str:
        prompt = (
            f"Write a short, warm spoken introduction for an AI interviewer greeting "
            f"a candidate named {candidate_name}. 2-3 sentences, conversational tone, "
            f"suitable to be read aloud by text-to-speech. Do not use markdown."
        )
        try:
            return await self.llm.generate(prompt, system=SYSTEM_PROMPT)
        except Exception as exc:  # noqa: BLE001
            logger.error("Falling back to static intro due to LLM error: %s", exc)
            return f"Hello {candidate_name}. I am your AI interviewer today."

    async def generate_profile_question(self) -> str:
        """The opening question that gathers role + experience level conversationally."""
        return (
            "Before we begin, could you tell me what role you're interviewing for today, "
            "and how many years of experience you have in that area?"
        )

    async def extract_profile(self, transcript: str) -> Dict[str, str]:
        """
        Parses the candidate's spoken answer to the opening question into a
        structured role + experience level. Falls back to sensible defaults
        if the answer was unclear or empty.
        """
        if not transcript or not transcript.strip():
            return {"role": self.role, "experience_level": self.experience_level}

        prompt = (
            f"The candidate was asked what role they're interviewing for and their "
            f"experience level. Their spoken answer (via speech-to-text, may be "
            f'imperfect): "{transcript}"\n\n'
            "Return JSON with exactly these fields:\n"
            "{\n"
            '  "role": "<short role/track name, e.g. \'Python Backend Developer\', '
            "'Frontend Engineer', 'Data Scientist', 'HR Generalist', etc — infer your "
            'best label from what they said>",\n'
            '  "experience_level": "<one of: Fresher, Junior, Mid-Level, Senior>"\n'
            "}"
        )
        try:
            result = await self.llm.generate_json(prompt, system=SYSTEM_PROMPT)
            role = str(result.get("role", "")).strip() or self.role
            experience = str(result.get("experience_level", "")).strip() or self.experience_level
            return {"role": role, "experience_level": experience}
        except Exception as exc:  # noqa: BLE001
            # Don't throw away what the candidate actually said just
            # because the LLM call failed (e.g. quota/rate-limit) — use
            # their raw answer as the role label instead of a generic
            # "General" default, so the rest of the interview is still
            # at least loosely relevant to what they told us.
            logger.error(
                "Could not extract role/experience via LLM (%s); using the raw transcript "
                "as the role instead of defaulting to 'General'.", exc,
            )
            fallback_role = transcript.strip()[:80] or self.role
            return {"role": fallback_role, "experience_level": self.experience_level}

    async def generate_question(
        self,
        session_id: str,
        question_number: int,
        history: list,
        running_avg_score: float,
        question_limit: int,
    ) -> Dict[str, Any]:
        """Asks the Reasoning Agent what to cover, then phrases the actual question."""
        plan: QuestionPlan = reasoning_agent.plan_next_question(
            session_id=session_id,
            question_number=question_number,
            role=self.role,
            history=history,
            running_avg_score=running_avg_score,
            question_limit=question_limit,
        )

        if plan.should_conclude:
            return {"question": None, "category": plan.category, "plan": plan}

        history_summary = "\n".join(
            f"- Q: {h['question']}\n  A: {h.get('answer', '')[:300]}\n  Score: {h.get('score', 'N/A')}"
            for h in history[-3:]
        ) or "No previous questions yet; this is the opening technical question."

        context_summary = reasoning_agent.context_summary(session_id)

        prompt = (
            f"Role: {self.role}\n"
            f"Experience level: {self.experience_level}\n"
            f"Question category to ask next: {plan.category}\n"
            f"Target difficulty: {plan.difficulty_hint}\n\n"
            f"Candidate context so far:\n{context_summary}\n\n"
            f"Recent interview history:\n{history_summary}\n\n"
            f"Write ONLY the next interview question as plain spoken text (no numbering, "
            f"no markdown, no answer). Keep it concise and natural to be read aloud."
        )
        already_asked = {h["question"].strip() for h in history if h.get("question")}

        question_text = None
        for attempt in range(2):  # one retry before giving up on the LLM
            try:
                candidate = (await self.llm.generate(prompt, system=SYSTEM_PROMPT)).strip()
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "LLM question generation failed (attempt %d/2): %s", attempt + 1, exc,
                )
                continue

            if candidate and candidate not in already_asked:
                question_text = candidate
                break
            logger.warning(
                "LLM returned an empty or already-asked question (attempt %d/2); retrying.",
                attempt + 1,
            )

        if not question_text:
            logger.error(
                "Falling back to a canned question for category=%s after LLM retries failed "
                "or kept repeating.", plan.category,
            )
            question_text = self._fallback_question(plan.category, already_asked)

        return {"question": question_text.strip(), "category": plan.category, "plan": plan}

    @staticmethod
    def _fallback_question(category: str, already_asked: Optional[set] = None) -> str:
        already_asked = already_asked or set()
        pool = FALLBACK_QUESTIONS.get(
            category, ["Tell me about a challenging project you've worked on."]
        )
        unused = [q for q in pool if q not in already_asked]
        # If every canned option for this category has already been used
        # this session (long interview, category came around several
        # times), fall back to the full pool rather than repeat-blocking
        # forever — a repeat here is still better than no question at all.
        return random.choice(unused) if unused else random.choice(pool)
