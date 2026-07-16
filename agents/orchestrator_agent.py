"""
agents/orchestrator_agent.py
Orchestrator Agent: the master coordinator. Drives the full interview
lifecycle by delegating to the specialist agents:
  Meeting Agent    -> join/speak/listen/leave the call
  TTS/STT Agents   -> synthesize questions, transcribe answers
  Interview Agent  -> phrase questions (via Reasoning Agent's plan)
  Reasoning Agent  -> decide category/difficulty/when to conclude
  Memory Agent     -> structured candidate context across the interview
  Evaluation Agent -> score each answer
  Monitor Agent    -> track audio/call health, surface warnings
  Report Agent     -> build the final JSON + PDF report
  Email Agent      -> deliver the report to the candidate

Runs inside a background thread per session (see routes/interview_routes.py)
so the Flask request that started it can return immediately.
"""
import asyncio
from typing import Optional

from agents.email_agent import send_report_email
from agents.evaluation_agent import EvaluationAgent
from agents.interview_agent import InterviewAgent
from agents.meeting_agent import MeetingAgent
from agents.memory_agent import memory_agent
from agents.monitor_agent import monitor_agent
from agents.report_agent import ReportAgent
from agents.stt_agent import STTAgent, STTError
from agents.tts_agent import TTSAgent, TTSError
from config import config
from interview.session import InterviewSession, QARecord, session_store
from utils.helpers import now_iso, seconds_between
from utils.logger import get_logger

logger = get_logger("interview")


class OrchestratorAgent:
    def __init__(self, session: InterviewSession):
        self.session = session
        self.interview_agent = InterviewAgent(role=session.role, experience_level=session.experience_level)
        self.evaluation_agent = EvaluationAgent()
        if session.role and session.experience_level:
            self.evaluation_agent.update_profile(session.role, session.experience_level)
        self.tts: Optional[TTSAgent] = None
        self.stt: Optional[STTAgent] = None
        self.meeting_agent: Optional[MeetingAgent] = None

        try:
            self.tts = TTSAgent()
        except Exception as exc:  # noqa: BLE001
            logger.error("TTS init failed, will run in text-only mode: %s", exc)

        try:
            self.stt = STTAgent()
        except Exception as exc:  # noqa: BLE001
            logger.error("STT init failed, will run in text-only mode: %s", exc)

    def run(self) -> None:
        """Synchronous entrypoint, called from a background thread."""
        try:
            asyncio.run(self._run_async())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Interview run crashed for session %s", self.session.session_id)
            session_store.update(
                self.session.session_id,
                status="failed",
                error_message=f"Interview failed: {exc}",
                end_time=now_iso(),
            )
            # Safety net: _wrap_up() normally handles bot removal, but if
            # the crash happened somewhere that skipped it, make one more
            # attempt here rather than leaving the bot stuck in the call
            # with no further cleanup ever running.
            if self.meeting_agent and self.meeting_agent.bot_id:
                try:
                    asyncio.run(self.meeting_agent.leave())
                except Exception as leave_exc:  # noqa: BLE001
                    logger.error(
                        "Safety-net bot removal also failed for session %s: %s",
                        self.session.session_id, leave_exc,
                    )
        finally:
            memory_agent.clear(self.session.session_id)

    async def _run_async(self) -> None:
        sid = self.session.session_id
        session_store.update(sid, status="joining", bot_status="joining")

        bot_display_name = "Aperture AI Interviewer"
        self.meeting_agent = MeetingAgent(
            meet_link=self.session.meet_link,
            display_name=bot_display_name,
            session_id=sid,
            candidate_name=self.session.name,
        )
        joined = await self.meeting_agent.join(should_stop=self.session.cancel_event.is_set)

        if not joined:
            # A bot may already have been created (and possibly be sitting in
            # the meeting or its waiting room) even though we couldn't
            # confirm it joined in time — always try to remove it rather
            # than abandoning it to be cleaned up by hand.
            try:
                await self.meeting_agent.leave()
            except Exception as exc:  # noqa: BLE001
                logger.error("Error cleaning up bot after failed join: %s", exc)
            session_store.update(
                sid,
                status="failed",
                bot_status="failed",
                error_message="Bot could not join the Google Meet call.",
                end_time=now_iso(),
            )
            return

        session_store.update(sid, meeting_joined=True, bot_status="joined", status="in_progress")

        try:
            await self._conduct_interview()
        finally:
            await self._wrap_up()

    async def _conduct_interview(self) -> None:
        sid = self.session.session_id
        cancelled = self.session.cancel_event.is_set

        intro = await self.interview_agent.generate_intro(self.session.name)
        await self._speak(intro)
        if cancelled():
            logger.info("[%s] Interview cancelled during intro.", sid)
            return

        profile_given_at_intake = bool(self.session.role and self.session.experience_level)

        if profile_given_at_intake:
            logger.info(
                "[%s] Role/experience already provided at intake (%s, %s); skipping the "
                "spoken profile question.", sid, self.session.role, self.session.experience_level,
            )
            profile = {"role": self.session.role, "experience_level": self.session.experience_level}
        else:
            profile_question = await self.interview_agent.generate_profile_question()
            await self._speak(profile_question)
            profile_answer = await self._listen(time_budget_seconds=config.MAX_ANSWER_SECONDS)
            if cancelled():
                logger.info("[%s] Interview cancelled while awaiting role/experience answer.", sid)
                return

            if not profile_answer or not profile_answer.strip():
                # Don't silently fall through to "General/Mid-Level" on the
                # first miss — it's usually a timing issue (candidate started
                # talking late, or right after the bot's audio) rather than
                # them having nothing to say. Ask once more before defaulting.
                logger.warning(
                    "[%s] No answer captured for the role/experience question; asking once more "
                    "before falling back to defaults.", sid,
                )
                await self._speak(
                    "Sorry, I didn't catch that. Could you tell me the role you're interviewing "
                    "for, and how many years of experience you have?"
                )
                profile_answer = await self._listen(time_budget_seconds=config.MAX_ANSWER_SECONDS)
                if cancelled():
                    logger.info("[%s] Interview cancelled while awaiting retried role/experience answer.", sid)
                    return
                if not profile_answer or not profile_answer.strip():
                    logger.warning(
                        "[%s] Still no answer captured after retry; falling back to General/Mid-Level. "
                        "Check STT/audio pipeline logs for this session if this keeps happening.", sid,
                    )

            profile = await self.interview_agent.extract_profile(profile_answer)

        self.interview_agent.update_profile(profile["role"], profile["experience_level"])
        self.evaluation_agent.update_profile(profile["role"], profile["experience_level"])
        memory_agent.set_profile(sid, profile["role"], profile["experience_level"])
        session_store.update(sid, role=profile["role"], experience_level=profile["experience_level"])

        transition = (
            f"Let's dive into some {profile['role']} questions suited to your experience."
            if profile_given_at_intake
            else f"Great, thanks. Let's dive into some {profile['role']} questions suited to your experience."
        )
        await self._speak(transition)

        question_count = 0
        running_score_total = 0.0
        logger.info(
            "[%s] Starting question loop: question_limit=%d (role=%s, level=%s)",
            sid, self.session.question_limit, profile["role"], profile["experience_level"],
        )

        while question_count < self.session.question_limit and not cancelled():
            running_avg = running_score_total / question_count if question_count else 5.0
            history = [
                {"question": r.question, "answer": r.answer, "score": r.score}
                for r in self.session.qa_records
            ]
            result = await self.interview_agent.generate_question(
                session_id=sid,
                question_number=question_count,
                history=history,
                running_avg_score=running_avg,
                question_limit=self.session.question_limit,
            )

            if result["plan"].should_conclude:
                logger.info(
                    "[%s] Reasoning Agent concluded early at Q%d/%d: %s | recent scores: %s",
                    sid, question_count, self.session.question_limit,
                    result["plan"].conclude_reason,
                    [r.score for r in self.session.qa_records[-4:]],
                )
                break

            record = QARecord(question=result["question"], category=result["category"])
            self.session.qa_records.append(record)
            question_count += 1
            session_store.update(sid, question_number=question_count)

            await self._speak(result["question"])
            if cancelled():
                logger.info("[%s] Interview cancelled after asking Q%d, before answer.", sid, question_count)
                break

            transcript = await self._listen(time_budget_seconds=config.MAX_ANSWER_SECONDS)
            record.answer = transcript
            record.answered_at = now_iso()

            if cancelled():
                logger.info("[%s] Interview cancelled while awaiting answer to Q%d.", sid, question_count)
                break

            evaluation = await self.evaluation_agent.evaluate(result["question"], result["category"], transcript)
            record.evaluation = evaluation
            record.score = evaluation["score"]
            running_score_total += evaluation["score"]

            memory_agent.record_answer(sid, result["category"], evaluation["score"], evaluation)
            session_store.update(sid, current_score=running_score_total / question_count)

            if evaluation.get("feedback"):
                preview = (transcript or "")[:120].replace("\n", " ")
                logger.info(
                    "[%s] Q%d scored %.1f/10 | transcript(%d chars): %r%s",
                    sid, question_count, evaluation["score"], len(transcript or ""),
                    preview, "..." if transcript and len(transcript) > 120 else "",
                )

        if not cancelled():
            closing = (
                f"That concludes our interview, {self.session.name}. Thank you for your time today. "
                "Your detailed report will be emailed to you shortly."
            )
            await self._speak(closing)

    async def _speak(self, text: str) -> None:
        if not text or self.session.cancel_event.is_set():
            return
        if self.tts:
            try:
                audio_path = await self.tts.synthesize(text)
                if self.meeting_agent:
                    duration = self.meeting_agent.push_audio_file(audio_path, text=text)
                    # push_audio_file only enqueues the audio; the audio_in
                    # WebSocket handler drains it asynchronously. Wait out the
                    # playback duration (plus a small buffer for network/queue
                    # drain) so we don't start listening for the candidate's
                    # answer while the interviewer is still mid-sentence.
                    # Sleep in small increments so an "End interview" click
                    # is noticed right away instead of only after the full
                    # sentence finishes playing.
                    remaining = duration + 0.5
                    while remaining > 0 and not self.session.cancel_event.is_set():
                        step = min(0.25, remaining)
                        await asyncio.sleep(step)
                        remaining -= step
            except TTSError as exc:
                logger.error("TTS synthesis failed, continuing text-only: %s", exc)
                monitor_agent.record_tts_failure(self.session.session_id)
        else:
            logger.info("[text-only mode] Interviewer says: %s", text)

    async def _listen(self, time_budget_seconds: float) -> str:
        if self.meeting_agent and self.stt:
            try:
                audio_path = await self.meeting_agent.record_response(
                    max_seconds=time_budget_seconds,
                    should_stop=self.session.cancel_event.is_set,
                )
                if audio_path:
                    logger.info(
                        "[%s] Transcribing candidate's response (this can take a while on "
                        "CPU-based local Whisper)...", self.session.session_id,
                    )
                    return await self.stt.transcribe(audio_path)
            except STTError as exc:
                logger.error("STT transcription failed: %s", exc)
                monitor_agent.record_stt_failure(self.session.session_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Recording candidate response failed: %s", exc)
        return ""

    async def _wrap_up(self) -> None:
        sid = self.session.session_id
        logger.info(
            "[%s] Interview loop finished; starting wrap-up (leaving the call via "
            "Meeting BaaS first, then generating the report).", sid,
        )

        # Leave the call first. Nothing below this point (report generation,
        # emailing the report) needs the bot to still be in the meeting --
        # it's all built from the qa_records already collected in memory --
        # so there's no reason to keep the bot sitting in the call while an
        # LLM call and an SMTP send run. Previously this ran after report
        # generation + email, which is why the bot lingered in the meeting
        # for however long those steps took after the interview was
        # actually over.
        bot_left = True
        if self.meeting_agent:
            try:
                bot_left = await self.meeting_agent.leave()
            except Exception as exc:  # noqa: BLE001
                logger.error("Error leaving meeting cleanly: %s", exc)
                bot_left = False

        session_store.update(sid, bot_status="left" if bot_left else "leave_failed")

        try:
            report_agent = ReportAgent(self.session)
            report_json, report_path = await report_agent.generate()
            report_json["monitor_warnings"] = monitor_agent.get_warnings(sid)
            session_store.update(sid, report_json=report_json, report_path=report_path)

            if self.session.email:
                sent, email_error = send_report_email(
                    to_email=self.session.email,
                    candidate_name=self.session.name,
                    role=self.session.role or "Interview",
                    overall_score=report_json.get("overall_score", 0),
                    pdf_path=report_path,
                )
                session_store.update(sid, email_sent=sent, email_error=email_error)
            else:
                logger.info("[%s] No email on file; skipping report email (download the PDF instead).", sid)
                session_store.update(sid, email_sent=False, email_error=None)
        except Exception as exc:  # noqa: BLE001
            logger.error("Report generation/email failed for session %s: %s", sid, exc)
            session_store.update(sid, error_message=f"Report generation failed: {exc}")

        final_status = self.session.status
        if final_status not in ("failed", "ended"):
            final_status = "completed"

        session_store.update(
            sid,
            status=final_status,
            end_time=now_iso(),
        )
        logger.info(
            "Session %s finished in %.0fs",
            sid,
            seconds_between(self.session.start_time, now_iso()),
        )
