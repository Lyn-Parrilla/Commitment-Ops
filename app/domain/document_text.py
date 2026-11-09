"""Extract readable text from grant and contract files without executing them.

Page and section locations are kept when the file format provides them.
Missing text stays missing. This module does not interpret obligations.
"""

from __future__ import annotations

import io
import re
import zlib
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

from app.domain.evidence_files import (
    MAX_EVIDENCE_FILE_BYTES,
    MAX_ZIP_COMPRESSION_RATIO,
    MAX_ZIP_ENTRIES,
    MAX_ZIP_ENTRY_BYTES,
)
from app.domain.exceptions import DomainValidationError

_PAGE_CHAR_LIMIT = 8_000
_PAGE_LIMIT = 50
_PROMPT_CHAR_LIMIT = 12_000
_MAX_PDF_OBJECTS = 2_000
_MAX_STREAM_OUTPUT = 1_048_576
_RATIO_FLOOR = 64 * 1024
_MAX_DOCX_XML = 2_097_152
_READ_CHUNK = 64 * 1024
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_STRING = re.compile(rb"\((?:\\.|[^\\)])*\)")
_OBJECT = re.compile(rb"(\d+)\s+\d+\s+obj\b(.*?)\bendobj\b", re.DOTALL)
_CONTENTS = re.compile(rb"/Contents\s+(\d+)\s+\d+\s+R")
_CONTENTS_ARRAY = re.compile(rb"/Contents\s*\[(.*?)\]", re.DOTALL)
_REF = re.compile(rb"(\d+)\s+\d+\s+R")


@dataclass(frozen=True)
class ExtractedPage:
    page: int
    text: str


@dataclass(frozen=True)
class ExtractedDocument:
    pages: tuple[ExtractedPage, ...]
    truncated: bool

    @property
    def has_text(self) -> bool:
        return any(page.text.strip() for page in self.pages)


def prompt_window(
    pages: tuple[ExtractedPage, ...],
) -> tuple[tuple[ExtractedPage, ...], bool]:
    """Keep the model input bounded. Later text stays available to reviewers."""

    kept: list[ExtractedPage] = []
    total = 0
    truncated = False
    for page in pages:
        room = _PROMPT_CHAR_LIMIT - total
        if room <= 0:
            truncated = True
            break
        text = page.text if len(page.text) <= room else page.text[:room]
        if len(text) < len(page.text):
            truncated = True
        if text.strip():
            kept.append(ExtractedPage(page=page.page, text=text.strip()))
        total += len(text)
        if len(text) < len(page.text):
            break
    return tuple(kept), truncated


def extract_document_text(media_type: str, content: bytes) -> ExtractedDocument:
    if len(content) > MAX_EVIDENCE_FILE_BYTES:
        raise DomainValidationError("This file is too large to read.")
    if media_type == "application/pdf":
        return _limit(_extract_pdf(content))
    if media_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ):
        return _limit(_extract_docx(content))
    raise DomainValidationError("This file is not an allowed type.")


def _limit(document: ExtractedDocument) -> ExtractedDocument:
    truncated = document.truncated or len(document.pages) > _PAGE_LIMIT
    pages: list[ExtractedPage] = []
    for page in document.pages[:_PAGE_LIMIT]:
        text = page.text
        if len(text) > _PAGE_CHAR_LIMIT:
            text = text[:_PAGE_CHAR_LIMIT]
            truncated = True
        if text.strip():
            pages.append(ExtractedPage(page=page.page, text=text.strip()))
    return ExtractedDocument(pages=tuple(pages), truncated=truncated)


def _extract_pdf(content: bytes) -> ExtractedDocument:
    if not content.startswith(b"%PDF-"):
        raise DomainValidationError("The file does not match its extension.")
    objects: dict[int, bytes] = {}
    seen = 0
    for match in _OBJECT.finditer(content):
        seen += 1
        if seen > _MAX_PDF_OBJECTS:
            raise DomainValidationError("This file is too large to read.")
        objects[int(match.group(1))] = match.group(2)
    streams = {number: _stream_text(body) for number, body in objects.items()}
    pages: list[ExtractedPage] = []
    for body in objects.values():
        if not re.search(rb"/Type\s*/Page(?!s)", body):
            continue
        numbers = [int(match.group(1)) for match in _CONTENTS.finditer(body)]
        array = _CONTENTS_ARRAY.search(body)
        if array:
            numbers.extend(int(match.group(1)) for match in _REF.finditer(array.group(1)))
        text = "\n".join(streams.get(number, "") for number in numbers).strip()
        pages.append(ExtractedPage(page=len(pages) + 1, text=text))
    if not pages:
        combined = "\n".join(text for text in streams.values() if text.strip())
        if combined.strip():
            pages.append(ExtractedPage(page=1, text=combined.strip()))
    return ExtractedDocument(pages=tuple(pages), truncated=False)


def _stream_text(body: bytes) -> str:
    start = body.find(b"stream")
    end = body.rfind(b"endstream")
    if start < 0 or end < start:
        return _strings(body)
    start += len(b"stream")
    if body[start:start + 2] == b"\r\n":
        start += 2
    elif body[start:start + 1] in (b"\n", b"\r"):
        start += 1
    raw = body[start:end].rstrip(b"\r\n")
    return _strings(_bounded_inflate(raw))


def _zlib_header_kind(raw: bytes) -> str:
    """Classify the front of a stream as literal, one zlib member, or malformed.

    A zlib header is recognized only when its checksum matches. Deflate members
    are inflated. A checksum-valid header that is not a plain deflate member,
    including a preset dictionary, is malformed. Anything else is literal PDF
    content and must not be passed to zlib.
    """

    if len(raw) < 2:
        return "literal"
    cmf = raw[0]
    flg = raw[1]
    if (cmf * 256 + flg) % 31 != 0:
        return "literal"
    if (cmf & 0x0F) != 8 or (cmf >> 4) > 7 or flg & 0x20:
        return "malformed"
    return "member"


def _bounded_inflate(raw: bytes) -> bytes:
    """Return literal PDF bytes, or the output of one complete zlib member.

    Literal bytes are returned unchanged. A recognized member is inflated in
    bounded steps. Each step may emit at most the unused portion of the
    cumulative output cap, so ``unconsumed_tail`` is never inflated without
    that remaining room and is never appended to another buffer.

    A finished member with any unused or unread bytes is rejected. That covers
    both plain trailing data and a concatenated zlib member. A member that
    ends before its checksum, and any zlib error after a recognized header,
    is rejected rather than read as literal text. The output of a single
    accepted member must also stay within the compression-ratio cap.
    """

    if len(raw) > MAX_EVIDENCE_FILE_BYTES:
        raise DomainValidationError("This file is too large to read.")
    kind = _zlib_header_kind(raw)
    if kind == "literal":
        return raw
    if kind != "member":
        raise DomainValidationError("The file does not match its extension.")

    decompressor = zlib.decompressobj()
    produced = bytearray()
    offset = 0
    try:
        while not decompressor.eof:
            remaining = _MAX_STREAM_OUTPUT - len(produced)
            if remaining <= 0:
                raise DomainValidationError("This file is too large to read.")
            pending = decompressor.unconsumed_tail
            if pending:
                piece = decompressor.decompress(pending, remaining)
            elif offset < len(raw):
                end = min(offset + _READ_CHUNK, len(raw))
                piece = decompressor.decompress(raw[offset:end], remaining)
                offset = end
            else:
                break
            if piece:
                produced.extend(piece)
            elif pending or offset >= len(raw):
                break
            if not decompressor.eof and len(produced) >= _MAX_STREAM_OUTPUT:
                raise DomainValidationError("This file is too large to read.")
    except zlib.error:
        raise DomainValidationError("The file does not match its extension.") from None

    if not decompressor.eof or decompressor.unconsumed_tail:
        if len(produced) >= _MAX_STREAM_OUTPUT or decompressor.unconsumed_tail:
            raise DomainValidationError("This file is too large to read.")
        raise DomainValidationError("The file does not match its extension.")
    if decompressor.unused_data or offset < len(raw):
        raise DomainValidationError("The file does not match its extension.")
    if (
        len(produced) > _RATIO_FLOOR
        and len(produced) > MAX_ZIP_COMPRESSION_RATIO * max(len(raw), 1)
    ):
        raise DomainValidationError("This file is too large to read.")
    return bytes(produced)


def _strings(payload: bytes) -> str:
    parts: list[str] = []
    for match in _STRING.finditer(payload):
        parts.append(_unescape(match.group(0)[1:-1]))
    return " ".join(part for part in parts if part.strip())


def _unescape(value: bytes) -> str:
    output = bytearray()
    index = 0
    while index < len(value):
        character = value[index]
        if character == 0x5C and index + 1 < len(value):
            escaped = value[index + 1]
            mapping = {ord("n"): ord("\n"), ord("r"): ord("\r"), ord("t"): ord("\t")}
            output.append(mapping.get(escaped, escaped))
            index += 2
            continue
        output.append(character)
        index += 1
    return output.decode("latin-1", errors="ignore")


def _extract_docx(content: bytes) -> ExtractedDocument:
    try:
        document = _read_docx_xml(content)
    except DomainValidationError:
        raise
    except (
        KeyError,
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        OSError,
    ) as exc:
        raise DomainValidationError("The file does not match its extension.") from exc
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise DomainValidationError("The file does not match its extension.") from exc
    pages: list[list[str]] = [[]]
    for paragraph in root.iter(f"{_WORD_NS}p"):
        if any(
            break_element.get(f"{_WORD_NS}type") == "page"
            for break_element in paragraph.iter(f"{_WORD_NS}br")
        ):
            pages.append([])
        text = "".join(
            node.text or "" for node in paragraph.iter(f"{_WORD_NS}t")
        ).strip()
        if text:
            pages[-1].append(text)
    extracted = tuple(
        ExtractedPage(page=index, text="\n".join(lines))
        for index, lines in enumerate(pages, start=1)
        if lines
    )
    return ExtractedDocument(pages=extracted, truncated=False)


def _read_docx_xml(content: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise DomainValidationError("This file is too large to read.")
        total = 0
        document_info = None
        for info in infos:
            declared = info.file_size
            compressed = info.compress_size
            if declared < 0 or compressed < 0 or declared > MAX_ZIP_ENTRY_BYTES:
                raise DomainValidationError("This file is too large to read.")
            if (
                declared > _RATIO_FLOOR
                and declared > MAX_ZIP_COMPRESSION_RATIO * max(compressed, 1)
            ):
                raise DomainValidationError("This file is too large to read.")
            total += declared
            if total > MAX_ZIP_ENTRY_BYTES:
                raise DomainValidationError("This file is too large to read.")
            if info.filename == "word/document.xml":
                if document_info is not None:
                    raise DomainValidationError(
                        "The file does not match its extension."
                    )
                document_info = info
        if document_info is None:
            raise DomainValidationError("The file does not match its extension.")
        return _read_zip_entry(archive, document_info)


def _read_zip_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    chunks: list[bytes] = []
    total = 0
    with archive.open(info, "r") as handle:
        while True:
            block = handle.read(_READ_CHUNK)
            if not block:
                break
            total += len(block)
            if total > _MAX_DOCX_XML:
                raise DomainValidationError("This file is too large to read.")
            chunks.append(block)
    return b"".join(chunks)
