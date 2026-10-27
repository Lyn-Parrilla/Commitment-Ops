"""Evidence-file inspection. These tests never touch object storage."""

import hashlib
import io
import struct
import zipfile
import zlib

import pytest

from app.domain.evidence_files import (
    MAX_EVIDENCE_FILE_BYTES,
    MAX_ZIP_ENTRIES,
    MAX_ZIP_ENTRY_BYTES,
    build_storage_key,
    inspect_evidence_file,
    require_storage_key,
)
from app.domain.exceptions import DomainValidationError


def test_each_allowed_type_has_a_checksum_and_detected_media_type() -> None:
    samples = {
        "notes.pdf": (_pdf(), "application/pdf"),
        "notes.docx": (_docx(), _DOCX),
        "notes.xlsx": (_xlsx(), _XLSX),
        "notes.csv": (b"name,amount\nAda,1\n", "text/csv"),
        "notes.png": (_png(), "image/png"),
        "notes.jpg": (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
        "notes.jpeg": (b"\xff\xd8\xff\xe1" + b"\x00" * 8, "image/jpeg"),
    }

    for filename, (content, media_type) in samples.items():
        inspected = inspect_evidence_file(filename, content)
        assert inspected.media_type == media_type
        assert inspected.byte_size == len(content)
        assert inspected.checksum_sha256 == hashlib.sha256(content).hexdigest()
        assert "/" not in inspected.filename


_DOCX = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _pdf() -> bytes:
    return b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def _docx(*, macro: bool = False) -> bytes:
    content_type = (
        "application/vnd.ms-word.document.macroEnabled.main+xml"
        if macro
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    )
    return _zip(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0"?>'
                "<Types>"
                f'<Override PartName="/word/document.xml" ContentType="{content_type}"/>'
                "</Types>"
            ),
            "word/document.xml": "<document/>",
            **({"word/vbaProject.bin": "macro"} if macro else {}),
        }
    )


def _xlsx(*, macro: bool = False) -> bytes:
    content_type = (
        "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
        if macro
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    )
    return _zip(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0"?>'
                "<Types>"
                f'<Override PartName="/xl/workbook.xml" ContentType="{content_type}"/>'
                "</Types>"
            ),
            "xl/workbook.xml": "<workbook/>",
            **({"xl/vbaProject.bin": "macro"} if macro else {}),
        }
    )


def _zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, contents in entries.items():
            archive.writestr(name, contents)
    return buffer.getvalue()


def test_filename_is_sanitized_and_never_used_as_a_storage_key() -> None:
    inspected = inspect_evidence_file(r"..\..\notes.pdf", _pdf())
    key = build_storage_key(
        "30000000-0000-0000-0000-000000000001",
        "40000000-0000-0000-0000-000000000001",
        "ab" * 16,
    )

    assert inspected.filename == "notes.pdf"
    assert "notes" not in key
    require_storage_key(key)
    with pytest.raises(DomainValidationError):
        require_storage_key("../" + key)


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("notes.pdf", b"x" * (MAX_EVIDENCE_FILE_BYTES + 1), "too large"),
        ("notes.pdf", b"", "empty"),
        ("notes.exe", _pdf(), "not an allowed type"),
        ("notes.doc", _pdf(), "not an allowed type"),
        ("notes.docm", _docx(), "not an allowed type"),
        ("notes.xlsm", _xlsx(), "not an allowed type"),
        ("notes.pdf", _png(), "does not match"),
        ("notes.docx", bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 32, "not an allowed type"),
        ("notes.csv", b"PK\x03\x04not-a-csv", "does not match"),
        ("notes.csv", b"\xff\xfe" + "name".encode("utf-16-le"), "does not match"),
        ("notes.docx", _docx(macro=True), "not an allowed type"),
        ("notes.xlsx", _xlsx(macro=True), "not an allowed type"),
        ("", _pdf(), "not valid"),
    ],
)
def test_disallowed_and_mislabeled_files_are_rejected(
    filename: str,
    content: bytes,
    message: str,
) -> None:
    with pytest.raises(DomainValidationError, match=message):
        inspect_evidence_file(filename, content)


def test_ooxml_rejects_oversized_expansion_and_malformed_archives() -> None:
    types = (
        '<?xml version="1.0"?>'
        "<Types>"
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    oversized = _zip_bytes(
        {
            "[Content_Types].xml": types.encode(),
            "word/document.xml": b"A" * (MAX_ZIP_ENTRY_BYTES + 1),
        }
    )
    with pytest.raises(DomainValidationError, match="not an allowed type"):
        inspect_evidence_file("notes.docx", oversized)

    ratio = io.BytesIO()
    with zipfile.ZipFile(ratio, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("word/document.xml", b"\x00" * 200_000)
    with pytest.raises(DomainValidationError, match="not an allowed type"):
        inspect_evidence_file("notes.docx", ratio.getvalue())

    crowded = {
        "[Content_Types].xml": types,
        "word/document.xml": "<document/>",
    }
    crowded.update({f"word/extra-{index}.xml": "x" for index in range(MAX_ZIP_ENTRIES)})
    with pytest.raises(DomainValidationError, match="not an allowed type"):
        inspect_evidence_file("notes.docx", _zip(crowded))

    with pytest.raises(DomainValidationError, match="does not match"):
        inspect_evidence_file("notes.docx", b"PK\x03\x04this is not a zip")


def test_ooxml_rejects_excessive_declared_size_and_processing_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.domain.evidence_files.MAX_ZIP_TOTAL_BYTES", 10)
    with pytest.raises(DomainValidationError, match="not an allowed type"):
        inspect_evidence_file("notes.docx", _docx())

    calls = {"count": 0}

    def clock() -> float:
        calls["count"] += 1
        return 0.0 if calls["count"] == 1 else 30.0

    monkeypatch.setattr("app.domain.evidence_files.MAX_ZIP_TOTAL_BYTES", 16 * 1024 * 1024)
    monkeypatch.setattr("app.domain.evidence_files.time.monotonic", clock)
    with pytest.raises(DomainValidationError, match="not an allowed type"):
        inspect_evidence_file("notes.docx", _docx())


def test_spreadsheet_formula_text_is_not_executed() -> None:
    content = _xlsx_with_formula()
    inspected = inspect_evidence_file("notes.xlsx", content)

    assert inspected.media_type.endswith("spreadsheetml.sheet")
    assert b"HYPERLINK" in inspected.content
    assert inspected.content == content


def _xlsx_with_formula() -> bytes:
    return _zip(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0"?>'
                "<Types>"
                '<Override PartName="/xl/workbook.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet.main+xml"/>'
                "</Types>"
            ),
            "xl/workbook.xml": '<workbook><f>HYPERLINK("https://example.test")</f></workbook>',
        }
    )


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, contents in entries.items():
            archive.writestr(name, contents)
    return buffer.getvalue()


def test_csv_formula_text_is_kept_as_untrusted_data() -> None:
    content = b'name,link\nAda,"=HYPERLINK(""https://example.test"")"\n'
    inspected = inspect_evidence_file("roster.csv", content)

    assert inspected.media_type == "text/csv"
    assert b"HYPERLINK" in inspected.content
