"""The public upload reader stops at the byte cap and does not spool to disk."""

import asyncio
import tempfile

import pytest

from app.api.bounded_multipart import OversizedUpload, read_bounded_multipart
from app.domain.evidence_files import MAX_EVIDENCE_FILE_BYTES
from app.domain.exceptions import DomainValidationError

_BOUNDARY = "----commitmentops-limit"


class _ChunkedRequest:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.pulled = 0
        self.headers = {
            "content-type": f"multipart/form-data; boundary={_BOUNDARY}"
        }

    async def stream(self):
        for chunk in self._chunks:
            self.pulled += 1
            yield chunk


def _part(name: str, body: bytes, *, filename: str | None = None) -> bytes:
    disposition = f'Content-Disposition: form-data; name="{name}"'
    if filename is not None:
        disposition += f'; filename="{filename}"'
    header = f"--{_BOUNDARY}\r\n{disposition}\r\n"
    if filename is not None:
        header += "Content-Type: application/pdf\r\n"
    return header.encode("ascii") + b"\r\n" + body


def _closing() -> bytes:
    return f"\r\n--{_BOUNDARY}--\r\n".encode("ascii")


def test_oversized_stream_stops_at_the_limit_and_does_not_spool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_spool(*args, **kwargs):
        raise AssertionError("multipart reader spooled upload bytes to disk")

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", forbid_spool)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", forbid_spool)
    monkeypatch.setattr(tempfile, "mkstemp", forbid_spool)
    prefix = b"%PDF-" + (b"a" * (MAX_EVIDENCE_FILE_BYTES - 5))
    assert len(prefix) == MAX_EVIDENCE_FILE_BYTES
    detection = b"Z"
    remainder = b"R" * (1024 * 1024)
    request = _ChunkedRequest(
        [
            _part("description", b"Synthetic file")
            + b"\r\n"
            + _part("file", prefix, filename="notes.pdf"),
            detection,
            remainder + _closing(),
        ]
    )

    with pytest.raises(OversizedUpload) as caught:
        asyncio.run(read_bounded_multipart(request))

    assert caught.value.file_bytes_seen == MAX_EVIDENCE_FILE_BYTES + 1
    assert request.pulled == 2
    assert len(request._chunks) == 3


def test_small_multipart_keeps_text_fields_and_file_bytes() -> None:
    body = (
        _part("description", b"Synthetic file")
        + b"\r\n"
        + _part("reference", b"https://example.test/receipt")
        + b"\r\n"
        + _part("file", b"%PDF-1.4\n", filename="notes.pdf")
        + _closing()
    )
    request = _ChunkedRequest([body[:40], body[40:80], body[80:]])

    parsed = asyncio.run(read_bounded_multipart(request))

    assert parsed.fields["description"] == "Synthetic file"
    assert parsed.fields["reference"] == "https://example.test/receipt"
    assert parsed.filename == "notes.pdf"
    assert parsed.content == b"%PDF-1.4\n"
    assert request.pulled == 3


def test_unknown_multipart_field_is_rejected() -> None:
    body = _part("unexpected", b"value") + _closing()
    request = _ChunkedRequest([body])

    with pytest.raises(DomainValidationError):
        asyncio.run(read_bounded_multipart(request))
