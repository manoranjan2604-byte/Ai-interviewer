"""
agents/email_agent.py
Email Agent: sends the interview report to the candidate once the
interview and report generation are complete. Two providers:

  smtp  (default) - any standard SMTP server (Gmail SMTP, SES, a company
        mail server). Needs SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD/EMAIL_FROM.
  brevo - Brevo's transactional email REST API. Free tier: 300 emails/day,
        no App Password setup, just BREVO_API_KEY. Better fit than SMTP
        on platforms where outbound port 587 can be flaky.

Selected via config.EMAIL_PROVIDER. If the selected provider isn't
configured, sending is skipped gracefully rather than crashing the
interview flow — this behavior is unchanged from before.
"""
import base64
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests

from config import config
from utils.logger import get_logger

logger = get_logger("interview")


class EmailSendError(Exception):
    """Raised when sending the report email fails."""


def is_configured() -> bool:
    if config.EMAIL_PROVIDER == "brevo":
        return bool(config.BREVO_API_KEY and config.EMAIL_FROM)
    return bool(config.SMTP_HOST and config.SMTP_USERNAME and config.SMTP_PASSWORD and config.EMAIL_FROM)


def send_report_email(
    to_email: str,
    candidate_name: str,
    role: str,
    overall_score: float,
    pdf_path: Optional[str] = None,
) -> "tuple[bool, Optional[str]]":
    """
    Emails the interview report to the candidate. Returns (True, None) if
    sent, or (False, reason) if email isn't configured or sending failed
    (never raises, so a failed email doesn't take down the rest of the
    interview completion). `reason` is a short, user-facing explanation
    suitable for showing in the report/status API, not the raw exception.
    """
    if not is_configured():
        reason = (
            "Email not configured (BREVO_API_KEY/EMAIL_FROM missing)."
            if config.EMAIL_PROVIDER == "brevo"
            else "Email not configured (SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD/EMAIL_FROM missing)."
        )
        logger.warning("%s Skipping report email to %s.", reason, to_email)
        return False, reason

    subject = f"Your interview report — {role or 'Interview'}"
    body = (
        f"Hi {candidate_name},\n\n"
        f"Thanks for completing your interview{f' for {role}' if role else ''}. "
        f"Your overall score was {overall_score}/10.\n\n"
        "Your full report, including question-by-question feedback, is attached as a PDF.\n\n"
        f"— {config.EMAIL_FROM_NAME}"
    )

    if config.EMAIL_PROVIDER == "brevo":
        return _send_via_brevo(to_email, subject, body, pdf_path)
    return _send_via_smtp(to_email, subject, body, pdf_path)


def _send_via_brevo(
    to_email: str, subject: str, body: str, pdf_path: Optional[str]
) -> "tuple[bool, Optional[str]]":
    payload = {
        "sender": {"name": config.EMAIL_FROM_NAME, "email": config.EMAIL_FROM},
        "to": [{"email": to_email}],
        "subject": subject,
        "textContent": body,
    }
    if pdf_path:
        with open(pdf_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        payload["attachment"] = [{"content": encoded, "name": "interview_report.pdf"}]

    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": config.BREVO_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if response.status_code in (200, 201):
            logger.info("Report email sent to %s via Brevo", to_email)
            return True, None
        logger.error("Brevo send failed for %s: %s %s", to_email, response.status_code, response.text)
        return False, f"Brevo API returned {response.status_code}: {response.text[:200]}"
    except requests.RequestException as exc:
        logger.error("Failed to send report email to %s via Brevo: %s", to_email, exc)
        return False, f"Failed to reach Brevo: {exc}"


def _send_via_smtp(
    to_email: str, subject: str, body: str, pdf_path: Optional[str]
) -> "tuple[bool, Optional[str]]":
    try:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM}>"
        msg["To"] = to_email
        msg.attach(MIMEText(body, "plain"))

        if pdf_path:
            with open(pdf_path, "rb") as f:
                attachment = MIMEApplication(f.read(), _subtype="pdf")
                attachment.add_header(
                    "Content-Disposition", "attachment", filename="interview_report.pdf"
                )
                msg.attach(attachment)

        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
            if config.SMTP_USE_TLS:
                server.starttls()
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            server.send_message(msg)

        logger.info("Report email sent to %s via SMTP", to_email)
        return True, None

    except smtplib.SMTPAuthenticationError as exc:
        hint = ""
        if "gmail" in (config.SMTP_HOST or "").lower():
            hint = (
                " Gmail rejects your regular account password for SMTP — generate a 16-character "
                "App Password (Google Account > Security > 2-Step Verification > App passwords) "
                "and put that in SMTP_PASSWORD instead."
            )
        logger.error("SMTP authentication failed sending report to %s: %s.%s", to_email, exc, hint)
        return False, "SMTP authentication failed — check SMTP_USERNAME/SMTP_PASSWORD." + hint

    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send report email to %s via SMTP: %s", to_email, exc)
        return False, f"Failed to send email: {exc}"
