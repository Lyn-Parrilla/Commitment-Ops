"""Text extraction keeps page locations and does not invent missing text."""

import io
import zipfile
from uuid import uuid4

import pytest

from app.domain.document_text import extract_document_text, prompt_window
from app.domain.evidence_files import (
    build_source_document_key,
    inspect_source_document,
    require_storage_key,
)
from app.domain.exceptions import DomainValidationError


def _pdf(text: str) -> bytes:
    stream = f"BT ({text}) Tj ET".encode("latin-1")
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /Contents 4 0 R >> endobj\n"
        + f"4 0 obj << /Length {len(stream)} >> stream\n".encode()
        + stream
        + b"\nendstream endobj\ntrailer << /Root 1 0 R >>\n%%EOF"
    )


def _docx(first: str, second: str) -> bytes:
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>{first}</w:t></w:r></w:p>
    <w:p><w:r><w:br w:type="page"/></w:r></w:p>
    <w:p><w:r><w:t>{second}</w:t></w:r></w:p>
  </w:body>
</w:document>
""".encode()
    content_types = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def test_pdf_text_keeps_the_page_and_does_not_invent_text() -> None:
    extracted = extract_document_text(
        "application/pdf",
        _pdf("The grantee shall submit a quarterly report."),
    )

    assert extracted.pages == (
        type(extracted.pages[0])(
            page=1,
            text="The grantee shall submit a quarterly report.",
        ),
    )
    assert "owner" not in extracted.pages[0].text.lower()


def test_docx_page_break_separates_sections() -> None:
    extracted = extract_document_text(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        _docx("Deliver the annual report.", "Quarterly reporting follows."),
    )

    assert [page.page for page in extracted.pages] == [1, 2]
    assert extracted.pages[0].text == "Deliver the annual report."
    assert extracted.pages[1].text == "Quarterly reporting follows."


def test_empty_pdf_has_no_invented_text() -> None:
    extracted = extract_document_text("application/pdf", b"%PDF-1.4\n%%EOF")

    assert extracted.pages == ()
    assert extracted.has_text is False


def test_prompt_window_flags_truncated_text() -> None:
    page = extract_document_text(
        "application/pdf",
        _pdf("A" * 20),
    ).pages[0]
    long_page = type(page)(page=1, text="B" * 13_000)
    kept, truncated = prompt_window((long_page,))

    assert truncated is True
    assert len(kept[0].text) <= 12_000


def test_source_documents_accept_pdf_and_reject_other_evidence_types() -> None:
    pdf = _pdf("Submit the report.")
    inspected = inspect_source_document("award.pdf", pdf)
    assert inspected.media_type == "application/pdf"

    with pytest.raises(DomainValidationError, match="not an allowed type"):
        inspect_source_document("notes.csv", b"column\nvalue\n")


def test_source_document_keys_are_not_evidence_keys() -> None:
    organization_id = str(uuid4())
    workflow_id = str(uuid4())
    document_id = str(uuid4())
    key = build_source_document_key(organization_id, workflow_id, document_id)

    assert key.startswith("sources/")
    assert len(key) == 118
    with pytest.raises(DomainValidationError):
        require_storage_key(key)
