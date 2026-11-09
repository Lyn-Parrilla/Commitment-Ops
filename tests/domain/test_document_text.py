"""Text extraction keeps page locations and does not invent missing text."""

import io
import warnings
import zlib
import zipfile
from uuid import uuid4

import pytest

from app.domain.document_text import (
    _MAX_PDF_OBJECTS,
    _MAX_STREAM_OUTPUT,
    extract_document_text,
    prompt_window,
)
from app.domain.evidence_files import (
    MAX_EVIDENCE_FILE_BYTES,
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


def _pdf_stream(stream: bytes) -> bytes:
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Length "
        + str(len(stream)).encode()
        + b" >> stream\n"
        + stream
        + b"\nendstream endobj\n%%EOF"
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


def test_complete_zlib_stream_is_read() -> None:
    sentence = "The grantee shall submit a quarterly report."
    stream = zlib.compress(f"BT ({sentence}) Tj ET".encode("latin-1"))

    extracted = extract_document_text("application/pdf", _pdf_stream(stream))

    assert extracted.pages[0].text == sentence


def test_truncated_zlib_stream_is_rejected() -> None:
    stream = zlib.compress(b"BT (kept) Tj ET")[:8]

    with pytest.raises(DomainValidationError, match="does not match its extension") as caught:
        extract_document_text("application/pdf", _pdf_stream(stream))

    assert "kept" not in str(caught.value)


def test_malformed_zlib_stream_is_not_read_as_text() -> None:
    header = zlib.compress(b"x")[:2]
    stream = header + b"not-deflate (secret obligation)"

    with pytest.raises(DomainValidationError, match="does not match its extension") as caught:
        extract_document_text("application/pdf", _pdf_stream(stream))

    assert "secret obligation" not in str(caught.value)


def test_zlib_checksum_failure_is_rejected() -> None:
    stream = bytearray(zlib.compress(b"BT (secret obligation) Tj ET"))
    stream[-1] ^= 0xFF

    with pytest.raises(DomainValidationError, match="does not match its extension") as caught:
        extract_document_text("application/pdf", _pdf_stream(bytes(stream)))

    assert "secret obligation" not in str(caught.value)


def test_preset_dictionary_header_is_rejected() -> None:
    stream = bytes((0x78, 0x20)) + b"(secret obligation)"

    with pytest.raises(DomainValidationError, match="does not match its extension") as caught:
        extract_document_text("application/pdf", _pdf_stream(stream))

    assert "secret obligation" not in str(caught.value)


def test_zlib_trailing_data_is_rejected() -> None:
    stream = zlib.compress(b"BT (visible) Tj ET") + b"(trailing secret)"

    with pytest.raises(DomainValidationError, match="does not match its extension") as caught:
        extract_document_text("application/pdf", _pdf_stream(stream))

    assert "trailing secret" not in str(caught.value)
    assert "visible" not in str(caught.value)


def test_concatenated_zlib_streams_are_rejected() -> None:
    stream = zlib.compress(b"BT (one) Tj ET") + zlib.compress(b"BT (two) Tj ET")

    with pytest.raises(DomainValidationError, match="does not match its extension") as caught:
        extract_document_text("application/pdf", _pdf_stream(stream))

    assert "one" not in str(caught.value)
    assert "two" not in str(caught.value)


def test_decompression_stops_at_the_output_cap() -> None:
    stream = zlib.compress(b"A" * (_MAX_STREAM_OUTPUT + 1))

    with pytest.raises(DomainValidationError, match="too large to read") as caught:
        extract_document_text("application/pdf", _pdf_stream(stream))

    assert "A" * 32 not in str(caught.value)


def test_compressed_pdf_bomb_is_rejected() -> None:
    payload = b"(" + (b"A" * 300_000) + b")"
    compressed = zlib.compress(payload)
    content = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Length "
        + str(len(compressed)).encode()
        + b" >> stream\n"
        + compressed
        + b"\nendstream endobj\n%%EOF"
    )

    with pytest.raises(DomainValidationError, match="too large to read"):
        extract_document_text("application/pdf", content)


def test_compressed_docx_bomb_is_rejected() -> None:
    document = b"A" * 200_000
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", document)

    with pytest.raises(DomainValidationError, match="too large to read"):
        extract_document_text(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            buffer.getvalue(),
        )


def test_malformed_documents_are_rejected() -> None:
    with pytest.raises(DomainValidationError, match="does not match its extension"):
        extract_document_text("application/pdf", b"not a pdf")
    with pytest.raises(DomainValidationError, match="does not match its extension"):
        extract_document_text(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"PK\x03\x04broken",
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", b"<not-xml")
    with pytest.raises(DomainValidationError, match="does not match its extension"):
        extract_document_text(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            buffer.getvalue(),
        )


def test_duplicate_docx_document_entries_are_rejected() -> None:
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", b"<Types/>")
            archive.writestr("word/document.xml", b"<w:document/>")
            archive.writestr("word/document.xml", b"<w:document><w:t>hidden</w:t></w:document>")

    with pytest.raises(DomainValidationError, match="does not match its extension") as caught:
        extract_document_text(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            buffer.getvalue(),
        )

    assert "hidden" not in str(caught.value)


def test_excessive_pdf_objects_are_rejected() -> None:
    objects = b"".join(
        f"{number} 0 obj << >> endobj\n".encode()
        for number in range(1, _MAX_PDF_OBJECTS + 2)
    )

    with pytest.raises(DomainValidationError, match="too large to read"):
        extract_document_text("application/pdf", b"%PDF-1.4\n" + objects)


def test_pdf_object_limit_allows_the_maximum() -> None:
    objects = b"".join(
        f"{number} 0 obj << >> endobj\n".encode()
        for number in range(1, _MAX_PDF_OBJECTS + 1)
    )

    extracted = extract_document_text("application/pdf", b"%PDF-1.4\n" + objects)

    assert extracted.pages == ()


def test_oversized_input_is_rejected_before_parsing() -> None:
    content = b"%PDF-" + (b"x" * MAX_EVIDENCE_FILE_BYTES)

    with pytest.raises(DomainValidationError, match="too large to read"):
        extract_document_text("application/pdf", content)


def test_source_document_keys_are_not_evidence_keys() -> None:
    organization_id = str(uuid4())
    workflow_id = str(uuid4())
    document_id = str(uuid4())
    key = build_source_document_key(organization_id, workflow_id, document_id)

    assert key.startswith("sources/")
    assert len(key) == 118
    with pytest.raises(DomainValidationError):
        require_storage_key(key)
