import base64
import logging

import pytest

from app.api.redaction import install_submission_log_redaction
from app.domain import DomainValidationError
from app.domain.submissions import (
    SUBMISSION_TOKEN_BYTES,
    generate_submission_token,
    redact_submission_token,
    submission_token_hash,
    validate_https_reference,
    validate_submission_description,
    validate_submitter_name,
)


def test_submission_token_contains_256_bits_and_is_not_the_stored_hash() -> None:
    raw = generate_submission_token()
    padded = raw + ("=" * (-len(raw) % 4))

    assert len(base64.urlsafe_b64decode(padded)) == SUBMISSION_TOKEN_BYTES
    digest = submission_token_hash(raw)
    assert digest != raw
    assert len(digest) == 64
    assert submission_token_hash(raw) == digest


def test_https_reference_rejects_insecure_and_credentialed_urls() -> None:
    assert (
        validate_https_reference(" https://example.test/records ")
        == "https://example.test/records"
    )
    for value in (
        "http://example.test/records",
        "https://user:secret@example.test/records",
        "https://user@example.test/records",
        "javascript:alert(1)",
        "https://",
    ):
        with pytest.raises(DomainValidationError, match="https URL"):
            validate_https_reference(value)


def test_public_description_and_name_do_not_invent_a_blank_name() -> None:
    assert validate_submission_description("  Synthetic note  ") == "Synthetic note"
    assert validate_submitter_name("   ") is None
    assert validate_submitter_name(None) is None
    with pytest.raises(DomainValidationError, match="description"):
        validate_submission_description("   ")


def test_redaction_removes_raw_submission_tokens() -> None:
    token = generate_submission_token()

    assert (
        redact_submission_token(f"header X-Submission-Token: {token} path")
        == "header X-Submission-Token: [redacted-submission-token] path"
    )
    assert (
        redact_submission_token(f"open #/s/{token} now")
        == "open #/s/[redacted-submission-token] now"
    )
    assert token not in redact_submission_token(f"failed {token}")


def test_application_log_does_not_keep_a_raw_submission_token(caplog) -> None:
    token = generate_submission_token()
    install_submission_log_redaction()

    with caplog.at_level(logging.WARNING):
        logging.getLogger("commitmentops.submission-test").warning(
            "rejected X-Submission-Token: %s path #/s/%s",
            token,
            token,
        )

    assert token not in caplog.text
    assert "[redacted-submission-token]" in caplog.text
