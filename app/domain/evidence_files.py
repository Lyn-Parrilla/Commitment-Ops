"""Validate evidence files before they are stored.

Browser-supplied names and content types are not trusted. Spreadsheet
formulas, CSV values, macros, embedded objects, and external links are
stored as data and are never executed, followed, or previewed here.
"""

from __future__ import annotations

import hashlib
import io
import re
import time
import zipfile
import zlib
from dataclasses import dataclass

from app.domain.exceptions import DomainValidationError

MAX_EVIDENCE_FILE_BYTES = 10 * 1024 * 1024
MAX_ZIP_ENTRY_BYTES = 8 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 16 * 1024 * 1024
MAX_ZIP_ENTRIES = 64
MAX_ZIP_COMPRESSION_RATIO = 100
MAX_ZIP_INSPECTION_SECONDS = 2.0
_FILENAME_LIMIT = 200
_CONTENT_TYPES_LIMIT = 1_000_000
_ZIP_RATIO_FLOOR = 64 * 1024
_STORAGE_KEY = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    r"[0-9a-f]{32}$"
)
_OLE = bytes.fromhex("D0CF11E0A1B11AE1")
_PNG = b"\x89PNG\r\n\x1a\n"
_PDF = b"%PDF-"
_JPEG = b"\xff\xd8\xff"

_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "csv": "text/csv",
    "docx": (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    ),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet"
    ),
}
EVIDENCE_MEDIA_TYPES = frozenset(_MEDIA_TYPES.values())


@dataclass(frozen=True)
class InspectedEvidenceFile:
    filename: str
    media_type: str
    content: bytes
    checksum_sha256: str

    @property
    def byte_size(self) -> int:
        return len(self.content)


def require_storage_key(key: str) -> str:
    if _STORAGE_KEY.fullmatch(key) is None:
        raise DomainValidationError("evidence storage key is not valid")
    return key


def build_storage_key(organization_id: str, evidence_id: str, random_hex: str) -> str:
    key = f"{organization_id}/{evidence_id}/{random_hex}"
    return require_storage_key(key)


def inspect_evidence_file(filename: str, content: bytes) -> InspectedEvidenceFile:
    if len(content) > MAX_EVIDENCE_FILE_BYTES:
        raise DomainValidationError("This file is too large.")
    if not content:
        raise DomainValidationError("This file is empty.")
    cleaned_name, extension = _sanitize_filename(filename)
    media_type = _MEDIA_TYPES[extension]
    _validate_structure(extension, content)
    return InspectedEvidenceFile(
        filename=cleaned_name,
        media_type=media_type,
        content=content,
        checksum_sha256=hashlib.sha256(content).hexdigest(),
    )


def _sanitize_filename(filename: str) -> tuple[str, str]:
    base = filename.replace("\\", "/").split("/")[-1].strip()
    base = "".join(character for character in base if character.isprintable())
    base = base.strip(". ")
    if not base or len(base) > _FILENAME_LIMIT:
        raise DomainValidationError("The file name is not valid.")
    if ".." in base.split("."):
        raise DomainValidationError("The file name is not valid.")
    extension = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if extension not in _MEDIA_TYPES:
        raise DomainValidationError("This file is not an allowed type.")
    stem = base[: -(len(extension) + 1)]
    if not stem or stem.endswith("."):
        raise DomainValidationError("The file name is not valid.")
    blocked = {"doc", "xls", "ppt", "docm", "xlsm", "pptm", "html", "svg", "exe", "js"}
    if any(part.lower() in blocked for part in stem.split(".")):
        raise DomainValidationError("This file is not an allowed type.")
    return f"{stem}.{extension}", extension


def _validate_structure(extension: str, content: bytes) -> None:
    if content.startswith(_OLE):
        raise DomainValidationError("This file is not an allowed type.")
    if extension == "pdf":
        if not content.startswith(_PDF):
            raise DomainValidationError("The file does not match its extension.")
        return
    if extension == "png":
        if not content.startswith(_PNG):
            raise DomainValidationError("The file does not match its extension.")
        return
    if extension in {"jpg", "jpeg"}:
        if not content.startswith(_JPEG):
            raise DomainValidationError("The file does not match its extension.")
        return
    if extension == "csv":
        _validate_csv(content)
        return
    if extension == "docx":
        _validate_ooxml(
            content,
            required_entry="word/document.xml",
            required_type=b"wordprocessingml.document.main+xml",
        )
        return
    if extension == "xlsx":
        _validate_ooxml(
            content,
            required_entry="xl/workbook.xml",
            required_type=b"spreadsheetml.sheet.main+xml",
        )
        return
    raise DomainValidationError("This file is not an allowed type.")


def _validate_csv(content: bytes) -> None:
    if content.startswith((b"PK\x03\x04", _PDF, _PNG, _JPEG, _OLE)):
        raise DomainValidationError("The file does not match its extension.")
    if b"\x00" in content:
        raise DomainValidationError("The file does not match its extension.")
    try:
        content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DomainValidationError(
            "The file does not match its extension."
        ) from exc


def _validate_ooxml(content: bytes, *, required_entry: str, required_type: bytes) -> None:
    if not content.startswith(b"PK\x03\x04"):
        raise DomainValidationError("The file does not match its extension.")
    started = time.monotonic()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            _check_zip_time(started)
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise DomainValidationError("This file is not an allowed type.")
            names = [info.filename for info in infos]
            _reject_unsafe_zip_entries(names)
            _reject_oversized_zip(infos, started)
            info = archive.getinfo("[Content_Types].xml")
            content_types = _read_zip_entry(archive, info, started)
    except DomainValidationError:
        raise
    except (
        KeyError,
        zipfile.BadZipFile,
        RuntimeError,
        zlib.error,
        EOFError,
        OSError,
    ) as exc:
        raise DomainValidationError(
            "The file does not match its extension."
        ) from exc
    lowered = content_types.lower()
    if b"macroenabled" in lowered or b"vbaproject" in lowered:
        raise DomainValidationError("This file is not an allowed type.")
    if required_entry not in names or required_type not in lowered:
        raise DomainValidationError("The file does not match its extension.")


def _reject_unsafe_zip_entries(names: list[str]) -> None:
    for name in names:
        parts = name.replace("\\", "/").split("/")
        lowered = name.lower()
        if (
            name.startswith("/")
            or ".." in parts
            or lowered.endswith("vbaproject.bin")
            or "macroenabled" in lowered
        ):
            raise DomainValidationError("This file is not an allowed type.")


def _reject_oversized_zip(infos: list[zipfile.ZipInfo], started: float) -> None:
    total = 0
    for info in infos:
        _check_zip_time(started)
        if info.flag_bits & 0x1:
            raise DomainValidationError("This file is not an allowed type.")
        declared = info.file_size
        compressed = info.compress_size
        if declared < 0 or compressed < 0 or declared > MAX_ZIP_ENTRY_BYTES:
            raise DomainValidationError("This file is not an allowed type.")
        if compressed == 0 and declared > 0:
            raise DomainValidationError("This file is not an allowed type.")
        if (
            declared > _ZIP_RATIO_FLOOR
            and declared > MAX_ZIP_COMPRESSION_RATIO * max(compressed, 1)
        ):
            raise DomainValidationError("This file is not an allowed type.")
        total += declared
        if total > MAX_ZIP_TOTAL_BYTES:
            raise DomainValidationError("This file is not an allowed type.")


def _read_zip_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    started: float,
) -> bytes:
    """Read one entry up to its cap. Declared size is not trusted alone."""

    if info.file_size > _CONTENT_TYPES_LIMIT:
        raise DomainValidationError("This file is not an allowed type.")
    chunks: list[bytes] = []
    total = 0
    with archive.open(info, "r") as handle:
        while True:
            _check_zip_time(started)
            block = handle.read(64 * 1024)
            if not block:
                break
            total += len(block)
            if total > _CONTENT_TYPES_LIMIT:
                raise DomainValidationError("This file is not an allowed type.")
            chunks.append(block)
    return b"".join(chunks)


def _check_zip_time(started: float) -> None:
    if time.monotonic() - started > MAX_ZIP_INSPECTION_SECONDS:
        raise DomainValidationError("This file is not an allowed type.")
