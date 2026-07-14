"""
agents/report_agent.py
Report Agent: builds the final JSON report and a formatted PDF (via
reportlab) summarizing the interview: scores, per-question breakdown,
strengths/weaknesses, and a recommendation.
"""
import os
from datetime import datetime
from typing import Any, Dict, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from api.gemini_api import LLMClient
from config import config
from interview.session import InterviewSession
from utils.helpers import seconds_between
from utils.logger import get_logger

logger = get_logger("interview")


class ReportAgent:
    def __init__(self, session: InterviewSession):
        self.session = session
        self.llm = LLMClient()

    async def generate(self) -> Tuple[Dict[str, Any], str]:
        report_data = self._aggregate_scores()
        summary = await self._generate_summary(report_data)
        report_data["summary"] = summary["summary"]
        report_data["recommendation"] = summary["recommendation"]
        report_data["strengths"] = summary["strengths"]
        report_data["weaknesses"] = summary["weaknesses"]
        report_data["recommendations"] = summary["recommendations"]

        pdf_path = self._render_pdf(report_data)
        return report_data, pdf_path

    def _aggregate_scores(self) -> Dict[str, Any]:
        records = self.session.qa_records
        answered = [r for r in records if r.evaluation]

        def avg(key: str) -> float:
            if not answered:
                return 0.0
            return round(sum(r.evaluation.get(key, 0) for r in answered) / len(answered), 2)

        total_duration_minutes = None
        if self.session.end_time:
            total_duration_minutes = round(seconds_between(self.session.start_time, self.session.end_time) / 60, 1)

        return {
            "session_id": self.session.session_id,
            "candidate_name": self.session.name,
            "candidate_email": self.session.email,
            "role": self.session.role or "Not determined",
            "experience_level": self.session.experience_level or "Not determined",
            "total_duration_minutes": total_duration_minutes,
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "questions_asked": len(records),
            "overall_score": avg("score"),
            "technical_score": avg("technical_score"),
            "communication_score": avg("communication_score"),
            "confidence_score": avg("confidence_score"),
            "problem_solving_score": avg("score"),  # proxy: overall reflects problem-solving categories too
            "qa_breakdown": [
                {
                    "question_number": i + 1,
                    "category": r.category,
                    "question": r.question,
                    "answer": r.answer,
                    "score": r.evaluation.get("score") if r.evaluation else None,
                    "feedback": r.evaluation.get("feedback") if r.evaluation else None,
                    "time_taken_seconds": r.time_taken_seconds(),
                }
                for i, r in enumerate(records)
            ],
        }

    async def _generate_summary(self, report_data: Dict[str, Any]) -> Dict[str, Any]:
        breakdown_text = "\n".join(
            f"Q{q['question_number']} [{q['category']}] score={q['score']}: {q['feedback']}"
            for q in report_data["qa_breakdown"]
        ) or "No questions were answered."

        prompt = (
            f"Candidate: {report_data['candidate_name']}\n"
            f"Role: {report_data['role']} ({report_data['experience_level']})\n"
            f"Overall score: {report_data['overall_score']}/10\n\n"
            f"Per-question results:\n{breakdown_text}\n\n"
            "Based only on this data, return JSON with:\n"
            "{\n"
            '  "summary": "<3-4 sentence professional summary of performance>",\n'
            '  "recommendation": "<one of: Strong Hire, Hire, Lean Hire, No Hire>",\n'
            '  "strengths": ["<point>", ...max 5],\n'
            '  "weaknesses": ["<point>", ...max 5],\n'
            '  "recommendations": ["<actionable improvement tip>", ...max 5]\n'
            "}"
        )
        try:
            result = await self.llm.generate_json(
                prompt, system="You write concise, fair, evidence-based interview summaries."
            )
            return {
                "summary": result.get("summary", ""),
                "recommendation": result.get("recommendation", "Lean Hire"),
                "strengths": list(result.get("strengths", []))[:5],
                "weaknesses": list(result.get("weaknesses", []))[:5],
                "recommendations": list(result.get("recommendations", []))[:5],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("Report summary generation failed, using fallback: %s", exc)
            return {
                "summary": "Automated summary generation was unavailable. See per-question scores below.",
                "recommendation": "Lean Hire" if report_data["overall_score"] >= 5 else "No Hire",
                "strengths": [],
                "weaknesses": [],
                "recommendations": [],
            }

    def _render_pdf(self, data: Dict[str, Any]) -> str:
        filename = f"interview_report_{data['session_id']}.pdf"
        path = os.path.join(config.REPORTS_DIR, filename)

        doc = SimpleDocTemplate(path, pagesize=A4, topMargin=1.5 * cm, bottomMargin=1.5 * cm)
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "TitleStyle", parent=styles["Title"], textColor=colors.HexColor("#4338CA")
        )
        heading_style = ParagraphStyle(
            "HeadingStyle", parent=styles["Heading2"], textColor=colors.HexColor("#312E81"),
            spaceBefore=14, spaceAfter=6,
        )
        body = styles["BodyText"]

        story = [
            Paragraph("AI Interview Report", title_style),
            Spacer(1, 0.4 * cm),
            Paragraph(
                f"<b>Candidate:</b> {data['candidate_name']} &nbsp;&nbsp; "
                f"<b>Email:</b> {data.get('candidate_email', 'N/A')}<br/>"
                f"<b>Role:</b> {data['role']} ({data['experience_level']})<br/>"
                f"<b>Date:</b> {data['date']} &nbsp;&nbsp; "
                f"<b>Total Duration:</b> {data.get('total_duration_minutes') or 'N/A'} min "
                f"&nbsp;&nbsp; <b>Questions Asked:</b> {data['questions_asked']}",
                body,
            ),
        ]

        score_table_data = [
            ["Metric", "Score (/10)"],
            ["Overall", data["overall_score"]],
            ["Technical", data["technical_score"]],
            ["Communication", data["communication_score"]],
            ["Confidence", data["confidence_score"]],
            ["Problem Solving", data["problem_solving_score"]],
        ]
        score_table = Table(score_table_data, colWidths=[8 * cm, 4 * cm])
        score_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4338CA")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F1F5F9")]),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )

        story.append(Paragraph("Scores", heading_style))
        story.append(score_table)

        story.append(Paragraph("Summary", heading_style))
        story.append(Paragraph(data.get("summary", ""), body))

        story.append(Paragraph(f"Recommendation: {data.get('recommendation', '')}", heading_style))

        for label, key in (("Strengths", "strengths"), ("Weaknesses", "weaknesses"), ("Suggestions", "recommendations")):
            story.append(Paragraph(label, heading_style))
            items = data.get(key) or ["N/A"]
            for item in items:
                story.append(Paragraph(f"&bull; {item}", body))

        story.append(Paragraph("Question-by-Question Breakdown", heading_style))
        for q in data["qa_breakdown"]:
            time_taken = q.get("time_taken_seconds")
            time_label = f"{int(time_taken)}s" if time_taken is not None else "N/A"
            qa_block = [
                [
                    Paragraph(
                        f"<b>Q{q['question_number']} [{q['category']}] — Score: {q['score']}/10 "
                        f"— Time taken: {time_label}</b>",
                        body,
                    )
                ],
                [Paragraph(f"<b>Question:</b> {q['question']}", body)],
                [Paragraph(f"<b>Answer:</b> {q['answer'] or '(no answer captured)'}", body)],
            ]
            if q.get("feedback"):
                qa_block.append([Paragraph(f"<b>Feedback:</b> {q['feedback']}", body)])

            qa_table = Table(qa_block, colWidths=[16 * cm])
            qa_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#EEF2FF")),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ]
                )
            )
            story.append(qa_table)
            story.append(Spacer(1, 0.3 * cm))

        doc.build(story)
        logger.info("Generated PDF report at %s", path)
        return path
