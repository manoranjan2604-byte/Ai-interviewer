"""
utils/validators.py
Input validation helpers for the interview API.
"""
import html
import re
from typing import Optional, Tuple

MEET_LINK_PATTERN = re.compile(
    r"^https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}(\?.*)?$", re.IGNORECASE
)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def sanitize_text(value: str, max_length: int = 200) -> str:
    """Escape and trim free-text user input to reduce injection risk."""
    if value is None:
        return ""
    cleaned = html.escape(str(value).strip())
    return cleaned[:max_length]


def validate_name(name: str) -> Tuple[bool, Optional[str]]:
    if not name or not name.strip():
        return False, "Candidate name is required."
    if len(name.strip()) < 2:
        return False, "Candidate name is too short."
    if len(name.strip()) > 100:
        return False, "Candidate name is too long."
    if not re.match(r"^[a-zA-Z\s.'-]+$", name.strip()):
        return False, "Candidate name contains invalid characters."
    return True, None


def validate_email(email: str) -> Tuple[bool, Optional[str]]:
    if not email or not email.strip():
        return False, "Email address is required so we can send you the report."
    if not EMAIL_PATTERN.match(email.strip()):
        return False, "That doesn't look like a valid email address."
    if len(email.strip()) > 254:
        return False, "Email address is too long."
    return True, None


def validate_meet_link(link: str) -> Tuple[bool, Optional[str]]:
    if not link or not link.strip():
        return False, "Google Meet link is required."
    if not MEET_LINK_PATTERN.match(link.strip()):
        return False, "Invalid Google Meet link format. Expected: https://meet.google.com/xxx-xxxx-xxx"
    return True, None


VALID_EXPERIENCE_LEVELS = {"Fresher", "Junior", "Mid-Level", "Senior"}


def validate_optional_role_experience(data: dict) -> Tuple[bool, Optional[str]]:
    """Role/experience are optional at intake — if the candidate didn't
    provide them, the interviewer asks on the call instead. Only validate
    them if present so we don't accept garbage that later confuses the
    interview agent."""
    role = (data.get("role") or "").strip()
    if role and len(role) > 100:
        return False, "Role is too long."

    experience_level = (data.get("experience_level") or "").strip()
    if experience_level and experience_level not in VALID_EXPERIENCE_LEVELS:
        return False, f"Experience level must be one of: {', '.join(sorted(VALID_EXPERIENCE_LEVELS))}."

    return True, None


def validate_start_payload(data: dict) -> Tuple[bool, Optional[str]]:
    """Run all validations for the /api/start payload. Returns (is_valid, error_message)."""
    if not data:
        return False, "Request body is required."

    ok, err = validate_name(data.get("name", ""))
    if not ok:
        return False, err

    ok, err = validate_email(data.get("email", ""))
    if not ok:
        return False, err

    ok, err = validate_meet_link(data.get("meet_link", ""))
    if not ok:
        return False, err

    ok, err = validate_optional_role_experience(data)
    if not ok:
        return False, err

    return True, None
