"""Bounded multipart reader for public evidence uploads.

Starlette's ``MultiPartParser`` applies ``max_part_size`` only when a part
has no filename. File parts are appended to a ``SpooledTemporaryFile`` with
no upper bound, and ``Request.form()`` returns only after the whole body
has been parsed. ``python_multipart``'s ``MultipartParser`` is the
streaming parser underneath that spooler. Its ``max_size`` argument
truncates silently, so this module does not use it as the limit.

``read_bounded_multipart`` feeds ``request.stream()`` into
``MultipartParser`` callbacks and counts file-part bytes as they are
emitted. At most ``MAX_EVIDENCE_FILE_BYTES + 1`` file bytes are copied
into memory. Crossing that limit raises ``OversizedUpload`` and stops the
read: bytes already inside the chunk that crossed the limit are not
copied further, and no later chunk is requested. Nothing is written to a
temporary file.

Text fields are capped at 8 KiB. On an oversized file, a disconnect, or a
parse error, the working buffers are cleared and no upload is finalized.
A single ASGI chunk that already contains more than the limit cannot be
un-received, but only the limit plus one detection byte is retained.
"""

from __future__ import annotations

from dataclasses import dataclass

from python_multipart.exceptions import FormParserError, MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect, Request

from app.domain.evidence_files import MAX_EVIDENCE_FILE_BYTES
from app.domain.exceptions import DomainValidationError

_TEXT_FIELDS = frozenset({"description", "reference", "submitter_name"})
_FILE_FIELD = "file"
_TEXT_LIMIT = 8192
_MAX_PARTS = 4
_HEADER_LIMIT = 8192


class OversizedUpload(Exception):
    """The file part reached one byte past the configured limit."""

    def __init__(self, file_bytes_seen: int) -> None:
        self.file_bytes_seen = file_bytes_seen
        super().__init__("upload exceeded the file byte limit")


@dataclass(frozen=True)
class BoundedMultipart:
    """Fields and file bytes retained from one request. No temp file."""

    fields: dict[str, str]
    filename: str
    content: bytes


async def read_bounded_multipart(
    request: Request,
    *,
    max_file_bytes: int = MAX_EVIDENCE_FILE_BYTES,
) -> BoundedMultipart:
    """Read one multipart body, stopping once a file part exceeds the cap."""

    content_type, params = parse_options_header(
        request.headers.get("content-type")
    )
    boundary = params.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise DomainValidationError("The submission is not valid.")
    reader = _Reader(max_file_bytes)
    parser = MultipartParser(
        boundary,
        _callbacks(reader),
        max_header_count=16,
        max_header_size=_HEADER_LIMIT,
    )
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            parser.write(chunk)
            if reader.finished:
                break
        if not reader.finished:
            parser.finalize()
        if not reader.finished or reader.rejected:
            raise DomainValidationError("The submission is not valid.")
        return reader.result()
    except OversizedUpload:
        raise
    except ClientDisconnect:
        raise
    except (MultipartParseError, FormParserError, ValueError) as exc:
        raise DomainValidationError("The submission is not valid.") from exc
    finally:
        reader.clear()


class _Reader:
    def __init__(self, max_file_bytes: int) -> None:
        self.max_file_bytes = max_file_bytes
        self.fields: dict[str, str] = {}
        self.filename = ""
        self._file = bytearray()
        self.file_bytes_seen = 0
        self.finished = False
        self.rejected = False
        self._parts = 0
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._headers: dict[str, bytes] = {}
        self._name = ""
        self._part_filename: str | None = None
        self._text = bytearray()
        self._is_file = False

    def result(self) -> BoundedMultipart:
        return BoundedMultipart(
            fields=dict(self.fields),
            filename=self.filename,
            content=bytes(self._file),
        )

    def clear(self) -> None:
        self._file.clear()
        self._text.clear()
        self._header_name.clear()
        self._header_value.clear()
        self._headers.clear()
        self.fields.clear()

    def begin_part(self) -> None:
        self._parts += 1
        if self._parts > _MAX_PARTS:
            self.rejected = True
            raise DomainValidationError("The submission is not valid.")
        self._headers.clear()
        self._header_name.clear()
        self._header_value.clear()
        self._name = ""
        self._part_filename = None
        self._text.clear()
        self._is_file = False

    def header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name.extend(data[start:end])

    def header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value.extend(data[start:end])

    def header_end(self) -> None:
        name = bytes(self._header_name).decode("latin-1").strip().lower()
        if name:
            self._headers[name] = bytes(self._header_value)
        self._header_name.clear()
        self._header_value.clear()

    def headers_finished(self) -> None:
        disposition = self._headers.get("content-disposition", b"")
        _disposition, params = parse_options_header(disposition)
        raw_name = params.get(b"name", b"")
        try:
            self._name = raw_name.decode("utf-8")
        except UnicodeDecodeError as exc:
            self.rejected = True
            raise DomainValidationError("The submission is not valid.") from exc
        raw_filename = params.get(b"filename")
        if self._name == _FILE_FIELD and raw_filename:
            self._is_file = True
            self._part_filename = _decode_filename(raw_filename)
            if self.filename:
                self.rejected = True
                raise DomainValidationError("The submission is not valid.")
            self.filename = self._part_filename
            return
        if self._name == _FILE_FIELD:
            self._name = ""
            return
        if self._name not in _TEXT_FIELDS or raw_filename is not None:
            self.rejected = True
            raise DomainValidationError("The submission is not valid.")
        if self._name in self.fields:
            self.rejected = True
            raise DomainValidationError("The submission is not valid.")

    def part_data(self, data: bytes, start: int, end: int) -> None:
        if start >= end:
            return
        if self._is_file:
            self._accept_file_bytes(data, start, end)
            return
        incoming = end - start
        if len(self._text) + incoming > _TEXT_LIMIT:
            self.rejected = True
            raise DomainValidationError("The submission is not valid.")
        self._text.extend(data[start:end])

    def end_part(self) -> None:
        if self._is_file or not self._name:
            return
        try:
            text = bytes(self._text).decode("utf-8")
        except UnicodeDecodeError as exc:
            self.rejected = True
            raise DomainValidationError("The submission is not valid.") from exc
        self.fields[self._name] = text.strip()
        self._text.clear()

    def end_message(self) -> None:
        self.finished = True

    def _accept_file_bytes(self, data: bytes, start: int, end: int) -> None:
        room = self.max_file_bytes + 1 - len(self._file)
        available = end - start
        if room <= 0:
            raise OversizedUpload(self.file_bytes_seen)
        take = available if available < room else room
        self._file.extend(data[start : start + take])
        self.file_bytes_seen += take
        if take < available or len(self._file) > self.max_file_bytes:
            raise OversizedUpload(self.file_bytes_seen)


def _callbacks(reader: _Reader) -> dict:
    return {
        "on_part_begin": reader.begin_part,
        "on_header_field": reader.header_field,
        "on_header_value": reader.header_value,
        "on_header_end": reader.header_end,
        "on_headers_finished": reader.headers_finished,
        "on_part_data": reader.part_data,
        "on_part_end": reader.end_part,
        "on_end": reader.end_message,
    }


def _decode_filename(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")
