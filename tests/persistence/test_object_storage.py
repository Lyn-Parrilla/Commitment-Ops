"""Fake object storage and the Supabase S3 adapter."""

import hashlib
from datetime import timedelta
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from app.config import Settings
from app.domain.evidence_files import MAX_EVIDENCE_FILE_BYTES, build_storage_key
from app.domain.exceptions import DomainValidationError
from app.persistence.exceptions import PersistenceError
from app.persistence.object_storage import (
    FakeObjectStorage,
    ObjectAlreadyExistsError,
    S3CompatibleObjectStorage,
    UnavailableObjectStorage,
    configured_object_storage,
)


def _key() -> str:
    return build_storage_key(str(uuid4()), str(uuid4()), "cd" * 16)


def test_fake_storage_is_create_only_and_delete_is_idempotent() -> None:
    storage = FakeObjectStorage()
    key = _key()

    storage.put_new(key, b"%PDF-1.4\n", "application/pdf")

    assert storage.exists(key) is True
    assert storage.get(key) == b"%PDF-1.4\n"
    with pytest.raises(ObjectAlreadyExistsError):
        storage.put_new(key, b"%PDF-1.4\n", "application/pdf")
    storage.delete(key)
    storage.delete(key)
    assert storage.exists(key) is False


def test_fake_storage_rejects_a_path_outside_the_key_pattern() -> None:
    storage = FakeObjectStorage()

    with pytest.raises(DomainValidationError):
        storage.put_new("../notes.pdf", b"%PDF-1.4\n", "application/pdf")


def test_unconfigured_storage_fails_closed() -> None:
    storage = configured_object_storage(Settings(_env_file=None))

    assert isinstance(storage, UnavailableObjectStorage)
    with pytest.raises(PersistenceError, match="not configured"):
        storage.put_new(_key(), b"%PDF-1.4\n", "application/pdf")


def test_partial_storage_configuration_fails_closed() -> None:
    settings = Settings(
        _env_file=None,
        evidence_storage_endpoint="https://storage.example.test",
    )

    with pytest.raises(PersistenceError, match="incomplete"):
        configured_object_storage(settings)


def test_complete_configuration_does_not_open_a_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid(*args, **kwargs):
        raise AssertionError("storage client contacted an endpoint")

    monkeypatch.setattr("boto3.client", forbid)
    storage = configured_object_storage(_complete_settings())

    assert isinstance(storage, S3CompatibleObjectStorage)
    assert "scoped-secret" not in repr(storage)


def test_s3_adapter_rejects_an_invalid_endpoint_or_service_role() -> None:
    with pytest.raises(PersistenceError, match="private HTTPS"):
        _adapter(endpoint="http://storage.example.test")
    with pytest.raises(PersistenceError, match="service-role"):
        _adapter(secret_access_key="prefix-service_role-suffix")
    with pytest.raises(PersistenceError, match="service-role"):
        _adapter(secret_access_key="eyJheader.eyJwYXlsb2FkIjoiIn0.signature")
    settings = Settings(
        _env_file=None,
        evidence_storage_endpoint="https://storage.example.test",
        evidence_storage_region="auto",
        evidence_storage_bucket="evidence",
        evidence_storage_access_key_id=SecretStr("scoped-access-key"),
        evidence_storage_secret_access_key=SecretStr("same-as-service-role"),
        supabase_service_role_key=SecretStr("same-as-service-role"),
    )
    with pytest.raises(PersistenceError, match="service-role"):
        configured_object_storage(settings)


def test_wrong_upload_limit_fails_closed() -> None:
    settings = Settings(_env_file=None, evidence_max_bytes=MAX_EVIDENCE_FILE_BYTES + 1)

    with pytest.raises(PersistenceError, match="approved maximum"):
        configured_object_storage(settings)


def test_s3_client_stores_bytes_and_does_not_replace_an_existing_object() -> None:
    client = FakeS3Client()
    storage = _adapter(client)
    key = _key()
    body = b"%PDF-1.4\n"

    storage.put_new(key, body, "application/pdf")

    assert client.objects[key] == body
    assert storage.exists(key) is True
    assert storage.get(key) == body
    assert hashlib.sha256(storage.get(key)).hexdigest() == hashlib.sha256(
        body
    ).hexdigest()
    assert client.urls == []
    with pytest.raises(ObjectAlreadyExistsError):
        storage.put_new(key, body, "application/pdf")
    assert client.puts == 1


def test_s3_client_failed_write_leaves_no_object() -> None:
    client = FakeS3Client()
    client.fail_puts = True
    storage = _adapter(client)
    key = _key()

    with pytest.raises(PersistenceError, match="could not be stored") as caught:
        storage.put_new(key, b"%PDF-1.4\n", "application/pdf")

    assert key not in client.objects
    assert "scoped-secret" not in str(caught.value)
    assert key not in str(caught.value)


def test_s3_client_delete_failure_keeps_the_object() -> None:
    client = FakeS3Client()
    storage = _adapter(client)
    key = _key()
    storage.put_new(key, b"name\n", "text/csv")
    client.fail_deletes = True

    with pytest.raises(PersistenceError, match="could not be deleted"):
        storage.delete(key)

    assert storage.exists(key) is True
    client.fail_deletes = False
    storage.delete(_key())
    storage.delete(key)
    storage.delete(key)
    assert storage.exists(key) is False


def test_s3_client_rejects_an_oversized_body_before_writing() -> None:
    client = FakeS3Client()
    storage = _adapter(client)

    with pytest.raises(PersistenceError, match="rejected"):
        storage.put_new(
            _key(),
            b"x" * (MAX_EVIDENCE_FILE_BYTES + 1),
            "application/pdf",
        )

    assert client.puts == 0


def test_s3_head_failure_does_not_report_the_object_missing() -> None:
    client = FakeS3Client()
    client.fail_heads = True
    storage = _adapter(client)

    with pytest.raises(PersistenceError, match="could not be verified"):
        storage.exists(_key())


def _complete_settings() -> Settings:
    return Settings(
        _env_file=None,
        evidence_storage_endpoint="https://storage.example.test",
        evidence_storage_region="auto",
        evidence_storage_bucket="evidence",
        evidence_storage_access_key_id=SecretStr("scoped-access-key"),
        evidence_storage_secret_access_key=SecretStr("scoped-secret"),
    )


def _adapter(client: "FakeS3Client | None" = None, **overrides: str) -> S3CompatibleObjectStorage:
    values = {
        "endpoint": "https://storage.example.test",
        "region": "auto",
        "bucket": "evidence",
        "access_key_id": "scoped-access-key",
        "secret_access_key": "scoped-secret",
    }
    values.update(overrides)
    return S3CompatibleObjectStorage(
        endpoint=values["endpoint"],
        region=values["region"],
        bucket=values["bucket"],
        access_key_id=values["access_key_id"],
        secret_access_key=values["secret_access_key"],
        client=client,
        client_factory=_forbid_factory,
    )


def _forbid_factory(*args: str, **kwargs: str):
    raise AssertionError("storage client contacted an endpoint")


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, amount: int) -> bytes:
        return self._data[:amount]


class FakeS3Client:
    """In-memory S3 stand-in. It has no network and no presigned URLs."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_puts = False
        self.fail_deletes = False
        self.fail_heads = False
        self.puts = 0
        self.urls: list[str] = []

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        self._bucket(Bucket)
        if self.fail_heads:
            raise _error("InternalError", "HeadObject")
        if Key not in self.objects:
            raise _error("404", "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ContentLength: int,
    ) -> dict:
        self._bucket(Bucket)
        self.puts += 1
        if self.fail_puts:
            raise _error("InternalError", "PutObject")
        if ContentLength != len(Body) or not ContentType:
            raise _error("InvalidRequest", "PutObject")
        self.objects[Key] = bytes(Body)
        return {}

    def delete_object(self, *, Bucket: str, Key: str) -> dict:
        self._bucket(Bucket)
        if self.fail_deletes:
            raise _error("InternalError", "DeleteObject")
        if Key not in self.objects:
            raise _error("NoSuchKey", "DeleteObject")
        del self.objects[Key]
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        self._bucket(Bucket)
        if Key not in self.objects:
            raise _error("NoSuchKey", "GetObject")
        return {"Body": _Body(self.objects[Key])}

    def _bucket(self, bucket: str) -> None:
        if bucket != "evidence":
            raise _error("NoSuchBucket", "HeadObject")


def _error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "synthetic"}}, operation)


def test_failed_delete_can_be_simulated() -> None:
    storage = FakeObjectStorage()
    key = _key()
    storage.put_new(key, b"name\n", "text/csv")
    storage.fail_deletes.add(key)

    with pytest.raises(PersistenceError):
        storage.delete(key)
    assert storage.exists(key) is True
    assert storage.stored_at(key) is not None
    assert timedelta(0) <= (storage.stored_at(key) - storage.stored_at(key))
