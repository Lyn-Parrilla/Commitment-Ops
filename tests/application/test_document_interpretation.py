"""Structured interpretation rejects incomplete model output and retries once."""

import json
from io import BytesIO
from urllib.error import HTTPError

import pytest
from pydantic import SecretStr

from app.application.document_interpretation import (
    DocumentInterpretationError,
    HttpDocumentInterpreter,
    UnavailableDocumentInterpreter,
    configured_interpreter,
    parse_interpretation,
)
from app.domain.document_text import ExtractedPage


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _payload(candidates: list[dict[str, object]]) -> str:
    return json.dumps({"candidates": candidates})


def _candidate(**overrides: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "classification": "requirement",
        "provenance": "document_explicit",
        "title": "Quarterly report",
        "description": "Submit a quarterly report.",
        "evidence_expectation": "The report",
        "due_date": None,
        "target": None,
        "explanation": "The agreement requires the report.",
        "confidence": 0.8,
        "uncertain": False,
        "location": {"page": 2, "section": None, "heading": None},
    }
    candidate.update(overrides)
    return candidate


def test_valid_explicit_and_inferred_candidates_keep_their_locations() -> None:
    interpreted = parse_interpretation(
        _payload(
            [
                _candidate(),
                _candidate(
                    provenance="ai_inferred",
                    title="Recurring report",
                    explanation="Reporting appears to continue after the first year.",
                    confidence=0.4,
                    uncertain=True,
                    due_date=None,
                    evidence_expectation=None,
                    location={"page": 4, "section": "Reports", "heading": None},
                ),
            ]
        )
    )

    assert interpreted[0].provenance.value == "document_explicit"
    assert interpreted[0].location.page == 2
    assert interpreted[1].provenance.value == "ai_inferred"
    assert interpreted[1].uncertain is True
    assert interpreted[1].location.section == "Reports"
    assert interpreted[1].due_date is None


def test_invalid_shape_owner_and_missing_location_are_rejected() -> None:
    with pytest.raises(DocumentInterpretationError) as invalid:
        parse_interpretation("{")
    assert invalid.value.code == "interpretation_invalid"
    assert "agreement" not in str(invalid.value)

    owned = _candidate()
    owned["owner"] = "Ada"
    with pytest.raises(DocumentInterpretationError) as extra:
        parse_interpretation(_payload([owned]))
    assert extra.value.code == "interpretation_invalid"

    unlocated = _candidate(location={"page": None, "section": None, "heading": None})
    with pytest.raises(DocumentInterpretationError) as missing:
        parse_interpretation(_payload([unlocated]))
    assert missing.value.code == "interpretation_invalid"


def test_user_provided_provenance_cannot_come_from_the_model() -> None:
    with pytest.raises(DocumentInterpretationError) as rejected:
        parse_interpretation(_payload([_candidate(provenance="user_provided")]))
    assert rejected.value.code == "interpretation_invalid"


def test_transient_provider_failure_retries_once_and_hides_the_endpoint() -> None:
    calls = {"count": 0}

    def opener(request, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            raise HTTPError(
                "https://models.example.test/secret-key",
                429,
                "slow down",
                hdrs=None,
                fp=BytesIO(b"document text must not leak"),
            )
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": _payload([_candidate()]),
                        }
                    }
                ]
            }
        ).encode()
        return _Response(200, body)

    interpreter = HttpDocumentInterpreter(
        base_url="https://models.example.test/v1",
        api_key="super-secret-model-key",
        model="test-model",
        opener=opener,
    )
    interpreted = interpreter.interpret(
        [ExtractedPage(page=2, text="The grantee shall submit a quarterly report.")]
    )

    assert calls["count"] == 2
    assert interpreted[0].title == "Quarterly report"
    assert "super-secret-model-key" not in str(interpreted)


def test_invalid_model_output_is_not_retried() -> None:
    calls = {"count": 0}

    def opener(request, timeout):
        calls["count"] += 1
        return _Response(200, b'{"choices":[{"message":{"content":"not-json"}}]}')

    interpreter = HttpDocumentInterpreter(
        base_url="https://models.example.test/v1",
        api_key="super-secret-model-key",
        model="test-model",
        opener=opener,
    )
    with pytest.raises(DocumentInterpretationError) as failed:
        interpreter.interpret([ExtractedPage(page=1, text="Submit the report.")])

    assert calls["count"] == 1
    assert failed.value.code == "interpretation_invalid"
    assert "super-secret-model-key" not in str(failed.value)


def test_missing_provider_configuration_does_not_call_a_network() -> None:
    class _Settings:
        llm_api_key = None
        llm_api_base_url = None
        llm_model = "gpt-4o-mini"

    interpreter = configured_interpreter(_Settings())
    assert isinstance(interpreter, UnavailableDocumentInterpreter)
    with pytest.raises(DocumentInterpretationError) as failed:
        interpreter.interpret([ExtractedPage(page=1, text="Submit the report.")])
    assert failed.value.code == "interpretation_unavailable"


def test_configured_key_is_not_read_into_the_failure() -> None:
    class _Settings:
        llm_api_key = SecretStr("super-secret-model-key")
        llm_api_base_url = "https://models.example.test/v1"
        llm_model = "test-model"

    interpreter = configured_interpreter(_Settings())
    assert isinstance(interpreter, HttpDocumentInterpreter)
    assert "super-secret-model-key" not in repr(interpreter)
