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

from app.domain.exceptions import DomainValidationError

_PAGE_CHAR_LIMIT = 8_000
_PAGE_LIMIT = 50
_PROMPT_CHAR_LIMIT = 12_000
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
    objects = {
        int(match.group(1)): match.group(2)
        for match in _OBJECT.finditer(content)
    }
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
    try:
        decoded = zlib.decompress(raw)
    except zlib.error:
        decoded = raw
    return _strings(decoded)


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
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            document = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
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
