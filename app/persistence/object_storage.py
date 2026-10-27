"""Private Supabase Storage for evidence files, through its S3 endpoint.

The application generates every key and talks to one private bucket from
this process. Supabase-generated S3 access keys bypass Storage RLS and can
read and write every bucket in the project. They are not bucket-scoped
credentials. This module never reads or sends the Supabase service-role
key, never creates a bucket, and never returns an object URL.

Constructing the adapter does not open a connection. The cleanup worker is
not started here. The application login cannot assume the maintenance role,
and overlapping cleanup workers are not enabled. See
``SubmissionRepository.cleanup_unreferenced_objects``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from app.domain.evidence_files import (
    EVIDENCE_MEDIA_TYPES,
    MAX_EVIDENCE_FILE_BYTES,
    require_storage_key,
)
from app.persistence.exceptions import PersistenceError

_REGION = re.compile(r"^[a-z0-9-]{2,32}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
_MISSING = frozenset({"404", "NoSuchKey", "NotFound"})
_EXISTS = frozenset({"PreconditionFailed", "412", "ConditionalRequestConflict"})
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


class ObjectStorage(Protocol):
    """Create-only private objects addressed by a server-generated key."""

    def put_new(self, key: str, body: bytes, media_type: str) -> None:
        """Store bytes only when the key is absent."""

    def get(self, key: str) -> bytes:
        """Return the object bytes for an authorized server-side read."""

    def delete(self, key: str) -> None:
        """Remove the object. A missing key is already gone."""

    def exists(self, key: str) -> bool:
        """Return whether the exact key is present."""


@dataclass(frozen=True)
class StoredObject:
    body: bytes
    media_type: str
    stored_at: datetime


class ObjectAlreadyExistsError(PersistenceError):
    """Raised when a create-only write finds the key occupied."""


class UnavailableObjectStorage:
    """Fail closed when no private bucket is configured."""

    def put_new(self, key: str, body: bytes, media_type: str) -> None:
        require_storage_key(key)
        raise PersistenceError("evidence file storage is not configured")

    def get(self, key: str) -> bytes:
        require_storage_key(key)
        raise PersistenceError("evidence file storage is not configured")

    def delete(self, key: str) -> None:
        require_storage_key(key)
        raise PersistenceError("evidence file storage is not configured")

    def exists(self, key: str) -> bool:
        require_storage_key(key)
        raise PersistenceError("evidence file storage is not configured")


class S3Client(Protocol):
    """The few S3 operations this adapter uses. Tests supply a fake."""

    def head_object(self, *, Bucket: str, Key: str) -> dict: ...

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ContentLength: int,
    ) -> dict: ...

    def delete_object(self, *, Bucket: str, Key: str) -> dict: ...

    def get_object(self, *, Bucket: str, Key: str) -> dict: ...


class S3CompatibleObjectStorage:
    """Private Supabase Storage bucket addressed with path-style S3 calls.

    ``client`` is injected by tests. Production builds a boto3 client on the
    first operation and does not connect while the adapter is constructed.
    Credentials stay on this object and are omitted from its representation.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        region: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        service_role_key: str | None = None,
        client: S3Client | None = None,
        client_factory: Callable[[str, str, str, str], S3Client] | None = None,
    ) -> None:
        _require_endpoint(endpoint)
        _require_region(region)
        _require_bucket(bucket)
        _require_credential(access_key_id, service_role_key)
        _require_credential(secret_access_key, service_role_key)
        self._endpoint = endpoint
        self._region = region
        self._bucket = bucket
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._client_instance = client
        self._factory = client_factory or _boto3_s3_client

    def __repr__(self) -> str:
        return "S3CompatibleObjectStorage(bucket=configured)"

    def put_new(self, key: str, body: bytes, media_type: str) -> None:
        require_storage_key(key)
        if media_type not in EVIDENCE_MEDIA_TYPES or not body:
            raise PersistenceError("evidence object was rejected")
        if len(body) > MAX_EVIDENCE_FILE_BYTES:
            raise PersistenceError("evidence object was rejected")
        if self.exists(key):
            raise ObjectAlreadyExistsError("evidence object already exists")
        try:
            self._client().put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType=media_type,
                ContentLength=len(body),
            )
        except PersistenceError:
            raise
        except Exception as exc:
            if _error_code(exc) in _EXISTS:
                raise ObjectAlreadyExistsError(
                    "evidence object already exists"
                ) from None
            raise PersistenceError("evidence object could not be stored") from None

    def get(self, key: str) -> bytes:
        """Read object bytes inside this process. No URL is created."""

        require_storage_key(key)
        try:
            response = self._client().get_object(Bucket=self._bucket, Key=key)
        except PersistenceError:
            raise
        except Exception as exc:
            if _error_code(exc) in _MISSING:
                raise PersistenceError("evidence object was not found") from None
            raise PersistenceError("evidence object could not be read") from None
        body = response.get("Body") if isinstance(response, dict) else None
        read = getattr(body, "read", None)
        if not callable(read):
            raise PersistenceError("evidence object could not be read")
        data = read(MAX_EVIDENCE_FILE_BYTES + 1)
        if not isinstance(data, bytes) or len(data) > MAX_EVIDENCE_FILE_BYTES:
            raise PersistenceError("evidence object could not be read")
        return data

    def delete(self, key: str) -> None:
        require_storage_key(key)
        try:
            self._client().delete_object(Bucket=self._bucket, Key=key)
        except PersistenceError:
            raise
        except Exception as exc:
            if _error_code(exc) in _MISSING:
                return
            raise PersistenceError("evidence object could not be deleted") from None

    def exists(self, key: str) -> bool:
        require_storage_key(key)
        try:
            self._client().head_object(Bucket=self._bucket, Key=key)
        except PersistenceError:
            raise
        except Exception as exc:
            if _error_code(exc) in _MISSING:
                return False
            raise PersistenceError(
                "evidence object storage could not be verified"
            ) from None
        return True

    def _client(self) -> S3Client:
        if self._client_instance is None:
            self._client_instance = self._factory(
                self._endpoint,
                self._region,
                self._access_key_id,
                self._secret_access_key,
            )
        return self._client_instance


def configured_object_storage(settings: object) -> ObjectStorage:
    """Return private storage, or fail closed when it is not fully configured.

    The fake store is never selected here. A complete configuration is
    validated and held without contacting the bucket.
    """

    if getattr(settings, "evidence_max_bytes", None) != MAX_EVIDENCE_FILE_BYTES:
        raise PersistenceError("evidence upload limit is not the approved maximum")
    endpoint = _text(getattr(settings, "evidence_storage_endpoint", None))
    region = _text(getattr(settings, "evidence_storage_region", None))
    bucket = _text(getattr(settings, "evidence_storage_bucket", None))
    access_value = _secret(getattr(settings, "evidence_storage_access_key_id", None))
    secret_value = _secret(
        getattr(settings, "evidence_storage_secret_access_key", None)
    )
    service_role = _secret(getattr(settings, "supabase_service_role_key", None))
    values = (endpoint, region, bucket, access_value, secret_value)
    if all(value == "" for value in values):
        return UnavailableObjectStorage()
    if any(value == "" for value in values):
        raise PersistenceError("evidence storage configuration is incomplete")
    return S3CompatibleObjectStorage(
        endpoint=endpoint,
        region=region,
        bucket=bucket,
        access_key_id=access_value,
        secret_access_key=secret_value,
        service_role_key=service_role or None,
    )


def _boto3_s3_client(
    endpoint: str,
    region: str,
    access_key_id: str,
    secret_access_key: str,
) -> S3Client:
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise PersistenceError("evidence storage client is not installed") from None
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 1},
            connect_timeout=5,
            read_timeout=30,
            s3={"addressing_style": "path"},
        ),
    )


def _require_endpoint(endpoint: str) -> None:
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or not host
        or host in _LOOPBACK
        or host.endswith(".local")
        or host.startswith("169.254.")
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.port not in (None, 443)
        or "\\" in endpoint
        or ".." in parts.path
        or endpoint != endpoint.strip()
        or "service_role" in endpoint.lower()
    ):
        raise PersistenceError(
            "evidence storage endpoint is not a private HTTPS bucket"
        )


def _require_region(region: str) -> None:
    if _REGION.fullmatch(region) is None:
        raise PersistenceError("evidence storage configuration is invalid")


def _require_bucket(bucket: str) -> None:
    if _BUCKET.fullmatch(bucket) is None or "service_role" in bucket.lower():
        raise PersistenceError("evidence storage configuration is invalid")


def _require_credential(value: str, service_role_key: str | None) -> None:
    if (
        not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise PersistenceError("evidence storage configuration is invalid")
    if (
        "service_role" in value.lower()
        or (value.startswith("eyJ") and value.count(".") == 2)
        or (service_role_key is not None and value == service_role_key)
    ):
        raise PersistenceError(
            "evidence storage credentials cannot be a Supabase service-role key"
        )


def _text(value: object) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else ""


def _secret(value: object) -> str:
    if value is None:
        return ""
    reader = getattr(value, "get_secret_value", None)
    if callable(reader):
        secret = reader()
        return secret if isinstance(secret, str) else ""
    return ""


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    if not isinstance(error, dict):
        return ""
    code = error.get("Code")
    return code if isinstance(code, str) else ""


class FakeObjectStorage:
    """Deterministic in-memory bucket for tests."""

    def __init__(self) -> None:
        self._objects: dict[str, StoredObject] = {}
        self.fail_deletes: set[str] = set()

    def put_new(self, key: str, body: bytes, media_type: str) -> None:
        require_storage_key(key)
        if key in self._objects:
            raise ObjectAlreadyExistsError("evidence object already exists")
        self._objects[key] = StoredObject(
            body=body,
            media_type=media_type,
            stored_at=datetime.now(UTC),
        )

    def get(self, key: str) -> bytes:
        require_storage_key(key)
        stored = self._objects.get(key)
        if stored is None:
            raise PersistenceError("evidence object was not found")
        return stored.body

    def delete(self, key: str) -> None:
        require_storage_key(key)
        if key in self.fail_deletes:
            raise PersistenceError("evidence object could not be deleted")
        self._objects.pop(key, None)

    def exists(self, key: str) -> bool:
        require_storage_key(key)
        return key in self._objects

    def stored_at(self, key: str) -> datetime | None:
        require_storage_key(key)
        stored = self._objects.get(key)
        return None if stored is None else stored.stored_at
