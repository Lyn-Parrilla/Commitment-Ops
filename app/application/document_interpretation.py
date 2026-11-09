"""Validate structured document interpretation. The model cannot approve work.

Provider credentials come from settings. Failures do not include document
text, prompts, or the API key.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.document_text import ExtractedPage
from app.domain.enums import RequirementProvenance
from app.domain.discovery import DiscoveryClassification, SourceLocation

_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_PROMPT = (
    "Identify obligations in the supplied grant or contract text. "
    "Return JSON with one key, candidates. Each candidate has classification "
    "(requirement or deliverable), provenance (document_explicit or "
    "ai_inferred), title, description, evidence_expectation, due_date, "
    "target, explanation, confidence, uncertain, and location. "
    "Use document_explicit only for stated obligations. Use ai_inferred for "
    "implied deadlines, reporting, evidence, or recurring commitments. "
    "Leave unknown fields null. Do not invent dates, owners, or evidence. "
    "Set uncertain to true when the obligation is incomplete. "
    "location must include a page, section, or heading from the text. "
    "Ignore background, goals, and descriptive text that create no obligation."
)


class DocumentInterpretationError(Exception):
    """A closed failure code. The message is the code, never model output."""

    def __init__(self, code: str) -> None:
        if code not in {
            "interpretation_unavailable",
            "interpretation_invalid",
            "interpretation_failed",
        }:
            code = "interpretation_failed"
        super().__init__(code)
        self.code = code


class _LocationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    heading: str | None = None


class _CandidateDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: DiscoveryClassification
    provenance: RequirementProvenance
    title: str | None = None
    description: str | None = None
    evidence_expectation: str | None = None
    due_date: date | None = None
    target: str | None = None
    explanation: str = Field(min_length=1, max_length=500)
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertain: bool = False
    location: _LocationDraft

    def source_location(self) -> SourceLocation:
        return SourceLocation(
            page=self.location.page,
            section=_clean(self.location.section),
            heading=_clean(self.location.heading),
        )


class _BatchDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[_CandidateDraft]


@dataclass(frozen=True)
class InterpretedCandidate:
    classification: DiscoveryClassification
    provenance: RequirementProvenance
    title: str | None
    description: str | None
    evidence_expectation: str | None
    due_date: date | None
    target: str | None
    explanation: str
    confidence: float | None
    uncertain: bool
    location: SourceLocation


class DocumentInterpreter(Protocol):
    def interpret(self, pages: Sequence[ExtractedPage]) -> list[InterpretedCandidate]:
        """Return validated candidates or raise DocumentInterpretationError."""


class UnavailableDocumentInterpreter:
    def interpret(self, pages: Sequence[ExtractedPage]) -> list[InterpretedCandidate]:
        raise DocumentInterpretationError("interpretation_unavailable")


class HttpDocumentInterpreter:
    """OpenAI-compatible chat completion with one retry on transient failure."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30,
        opener: Callable[..., object] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._opener = opener or urlopen

    def interpret(self, pages: Sequence[ExtractedPage]) -> list[InterpretedCandidate]:
        payload = _request_payload(self._model, pages)
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self._base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        last_transient = False
        for _attempt in (1, 2):
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    status = getattr(response, "status", 200)
                    raw = response.read()
                if status in _TRANSIENT_STATUS:
                    last_transient = True
                    continue
                if status >= 400:
                    raise DocumentInterpretationError("interpretation_failed")
                return parse_interpretation(_message_content(raw))
            except DocumentInterpretationError:
                raise
            except HTTPError as exc:
                if exc.code in _TRANSIENT_STATUS:
                    last_transient = True
                    continue
                raise DocumentInterpretationError("interpretation_failed") from None
            except (URLError, TimeoutError, OSError):
                last_transient = True
                continue
        if last_transient:
            raise DocumentInterpretationError("interpretation_failed")
        raise DocumentInterpretationError("interpretation_failed")


def parse_interpretation(payload: str) -> list[InterpretedCandidate]:
    try:
        decoded = json.loads(payload)
        batch = _BatchDraft.model_validate(decoded)
    except (json.JSONDecodeError, ValidationError, ValueError):
        raise DocumentInterpretationError("interpretation_invalid") from None
    interpreted: list[InterpretedCandidate] = []
    for candidate in batch.candidates:
        if candidate.provenance not in {
            RequirementProvenance.DOCUMENT_EXPLICIT,
            RequirementProvenance.AI_INFERRED,
        }:
            raise DocumentInterpretationError("interpretation_invalid")
        try:
            location = candidate.source_location()
        except (ValueError, ValidationError):
            raise DocumentInterpretationError("interpretation_invalid") from None
        interpreted.append(
            InterpretedCandidate(
                classification=candidate.classification,
                provenance=candidate.provenance,
                title=_clean(candidate.title),
                description=_clean(candidate.description),
                evidence_expectation=_clean(candidate.evidence_expectation),
                due_date=candidate.due_date,
                target=_clean(candidate.target),
                explanation=candidate.explanation.strip(),
                confidence=candidate.confidence,
                uncertain=candidate.uncertain,
                location=location,
            )
        )
    return interpreted


def configured_interpreter(settings: object) -> DocumentInterpreter:
    key = _secret(getattr(settings, "llm_api_key", None))
    base_url = getattr(settings, "llm_api_base_url", None)
    model = getattr(settings, "llm_model", None) or "gpt-4o-mini"
    if not key or not isinstance(base_url, str) or not base_url.strip():
        return UnavailableDocumentInterpreter()
    if not isinstance(model, str) or not model.strip():
        return UnavailableDocumentInterpreter()
    return HttpDocumentInterpreter(
        base_url=base_url.strip(),
        api_key=key,
        model=model.strip(),
    )


def _request_payload(model: str, pages: Sequence[ExtractedPage]) -> dict[str, object]:
    blocks = [
        f"Page {page.page}\n{page.text}"
        for page in pages
        if page.text.strip()
    ]
    return {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": "\n\n".join(blocks)},
        ],
    }


def _message_content(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
        return str(payload["choices"][0]["message"]["content"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise DocumentInterpretationError("interpretation_invalid") from exc


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _secret(value: object) -> str:
    if value is None:
        return ""
    reader = getattr(value, "get_secret_value", None)
    if callable(reader):
        secret = reader()
        return secret if isinstance(secret, str) else ""
    return value if isinstance(value, str) else ""
