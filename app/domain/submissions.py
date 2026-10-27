"""Submission-link tokens and the public evidence payload.

The raw token is shown once. Persistence stores only its hash.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from urllib.parse import urlsplit

from app.domain.exceptions import DomainValidationError

SUBMISSION_TOKEN_BYTES = 32
SUBMISSION_LINK_TTL_DAYS = 14
SUBMISSION_LINK_MAX_USES = 25
SUBMISSION_DESCRIPTION_LIMIT = 4000
SUBMISSION_NAME_LIMIT = 200
SUBMISSION_REFERENCE_LIMIT = 1000
SUBMISSION_ACTOR_PREFIX = "submission-link:"
_TOKEN_LIMIT = 200
_RAW_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")
_HEADER_TOKEN = re.compile(
    r"(X-Submission-Token\s*[:=]\s*)([^\s%]+)",
    re.IGNORECASE,
)
_PATH_TOKEN = re.compile(r"(#/s/)[A-Za-z0-9_-]+")


def generate_submission_token() -> str:
    """Return a URL-safe token with at least 256 bits of randomness."""

    return secrets.token_urlsafe(SUBMISSION_TOKEN_BYTES)


def submission_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def submission_actor_reference(link_id: str) -> str:
    return f"{SUBMISSION_ACTOR_PREFIX}{link_id}"


def normalize_submission_token(value: str | None) -> str | None:
    """Accept a presented token without echoing it back to the caller."""

    if value is None:
        return None
    token = value.strip()
    if not token or len(token) > _TOKEN_LIMIT:
        return None
    return token


def redact_submission_token(value: str) -> str:
    """Remove raw submission tokens from a log or error string."""

    redacted = _HEADER_TOKEN.sub(r"\1[redacted-submission-token]", value)
    redacted = _PATH_TOKEN.sub(r"\1[redacted-submission-token]", redacted)
    return _RAW_TOKEN.sub("[redacted-submission-token]", redacted)


def validate_submission_description(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > SUBMISSION_DESCRIPTION_LIMIT:
        raise DomainValidationError("description is required")
    return cleaned


def validate_submitter_name(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > SUBMISSION_NAME_LIMIT:
        raise DomainValidationError(
            "submitter name must be 200 characters or less"
        )
    return cleaned


def validate_https_reference(value: str) -> str:
    """Accept one https URL and reject credentials embedded in it."""

    reference = value.strip()
    if (
        not reference
        or len(reference) > SUBMISSION_REFERENCE_LIMIT
        or any(character.isspace() or ord(character) < 32 for character in reference)
        or not reference.startswith("https://")
    ):
        raise DomainValidationError("reference must be an https URL")
    parsed = urlsplit(reference)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.hostname
    ):
        raise DomainValidationError("reference must be an https URL")
    return reference
