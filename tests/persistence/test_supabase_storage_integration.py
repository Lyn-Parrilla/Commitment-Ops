"""Opt-in Supabase Storage checks for the evidence adapter.

Ordinary tests never reach this module's network path. A round trip runs
only when ``COMMITMENTOPS_STORAGE_INTEGRATION`` is exactly ``1`` and every
``TEST_EVIDENCE_STORAGE_*`` variable is present in the process environment.
This module does not read ``.env`` and does not use
``SUPABASE_SERVICE_ROLE_KEY``.

The bucket must already exist and be private. Live tests accept only
``commitment-ops-test2`` and fail before building a client for any other
name. The endpoint hostname must be exactly
``qpwnywbktuyeudtwdgsh.storage.supabase.co``. Any other hostname fails
before an S3 client is created. The harness does not create or delete
buckets. Objects use a reserved organization id as their prefix, then a
new evidence id and a new random suffix. A key that already exists is not
written.

Export the variables in the shell for one opted-in run. Do not commit them.

- ``COMMITMENTOPS_STORAGE_INTEGRATION=1``
- ``TEST_EVIDENCE_STORAGE_ENDPOINT``
- ``TEST_EVIDENCE_STORAGE_REGION``
- ``TEST_EVIDENCE_STORAGE_BUCKET``
- ``TEST_EVIDENCE_STORAGE_ACCESS_KEY_ID``
- ``TEST_EVIDENCE_STORAGE_SECRET_ACCESS_KEY``
"""

from __future__ import annotations

import hashlib
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError

from app.domain.evidence_files import build_storage_key, inspect_evidence_file
from app.persistence.exceptions import PersistenceError
from app.persistence.object_storage import S3CompatibleObjectStorage
from app.persistence.submissions import SubmissionRepository
from tests.persistence.test_evidence_file_cleanup import _ready, _use_count

OPT_IN_FLAG = "COMMITMENTOPS_STORAGE_INTEGRATION"
_CONFIG_NAMES = (
    "TEST_EVIDENCE_STORAGE_ENDPOINT",
    "TEST_EVIDENCE_STORAGE_REGION",
    "TEST_EVIDENCE_STORAGE_BUCKET",
    "TEST_EVIDENCE_STORAGE_ACCESS_KEY_ID",
    "TEST_EVIDENCE_STORAGE_SECRET_ACCESS_KEY",
)
TEST_ORGANIZATION_ID = "11111111-1111-4111-8111-111111111111"
_DISPOSABLE_TEST_BUCKET = "commitment-ops-test2"
_VERIFIED_STORAGE_HOST = "qpwnywbktuyeudtwdgsh.storage.supabase.co"
_VERIFIED_STORAGE_ENDPOINT = (
    f"https://{_VERIFIED_STORAGE_HOST}/storage/v1/s3"
)
_SKIP_REASON = (
    "Supabase Storage integration tests were skipped. Set "
    "COMMITMENTOPS_STORAGE_INTEGRATION=1 and export every "
    "TEST_EVIDENCE_STORAGE_* variable in the process environment. "
    "The .env file is not read."
)
_MISSING = frozenset({"404", "NoSuchKey", "NotFound"})
_DENIED = frozenset({400, 401, 403, 404})


@dataclass
class StorageTestConfig:
    """Test bucket coordinates. The repr never includes credentials."""

    endpoint: str
    region: str
    bucket: str
    access_key_id: str
    secret_access_key: str

    def __repr__(self) -> str:
        return "StorageTestConfig(redacted)"


def load_storage_test_config() -> StorageTestConfig | None:
    """Return test settings only from the process environment."""

    if os.environ.get(OPT_IN_FLAG) != "1":
        return None
    values = [os.environ.get(name, "").strip() for name in _CONFIG_NAMES]
    if not all(values):
        return None
    endpoint, region, bucket, access_key_id, secret_access_key = values
    return StorageTestConfig(
        endpoint=endpoint,
        region=region,
        bucket=bucket,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
    )


def require_storage_test_config() -> StorageTestConfig:
    """Skip before any client is constructed when the opt-in is absent.

    A configured bucket other than the disposable test bucket fails before
    a client or object operation can start.
    """

    config = load_storage_test_config()
    if config is None:
        pytest.skip(_SKIP_REASON)
    if config.bucket != _DISPOSABLE_TEST_BUCKET:
        pytest.fail("test storage bucket is not the disposable test bucket")
    return config


def fresh_test_key() -> str:
    """Build one new key under the reserved test organization prefix."""

    return build_storage_key(
        TEST_ORGANIZATION_ID,
        str(uuid4()),
        secrets.token_hex(16),
    )


def test_storage_integration_skips_without_the_opt_in_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _hide_config(monkeypatch)
    _write_unused_dotenv(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_CONFIG_NAMES[0], "https://example.supabase.co/storage/v1/s3")
    monkeypatch.setenv(_CONFIG_NAMES[1], "us-east-1")
    monkeypatch.setenv(_CONFIG_NAMES[2], "evidence-test")
    monkeypatch.setenv(_CONFIG_NAMES[3], "example-access-key")
    monkeypatch.setenv(_CONFIG_NAMES[4], "example-secret")

    assert load_storage_test_config() is None
    with pytest.raises(pytest.skip.Exception):
        require_storage_test_config()


def test_storage_integration_skips_when_configuration_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hide_config(monkeypatch)
    monkeypatch.setenv(OPT_IN_FLAG, "1")
    monkeypatch.setenv(_CONFIG_NAMES[0], "https://example.supabase.co/storage/v1/s3")

    assert load_storage_test_config() is None


@pytest.mark.parametrize(
    "bucket",
    ["evidence-test", "commitment-ops-test", "Commitment-Ops-Test2"],
)
def test_storage_integration_rejects_any_other_bucket(
    monkeypatch: pytest.MonkeyPatch,
    bucket: str,
) -> None:
    _hide_config(monkeypatch)
    _set_complete_test_config(monkeypatch, bucket)

    with pytest.raises(pytest.fail.Exception, match="disposable test bucket"):
        require_storage_test_config()


def test_storage_integration_accepts_the_disposable_test_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hide_config(monkeypatch)
    _set_complete_test_config(monkeypatch, _DISPOSABLE_TEST_BUCKET)

    config = require_storage_test_config()

    assert config.bucket == _DISPOSABLE_TEST_BUCKET
    assert repr(config) == "StorageTestConfig(redacted)"


def test_fresh_test_key_uses_the_reserved_prefix() -> None:
    first = fresh_test_key()
    second = fresh_test_key()

    assert first.startswith(f"{TEST_ORGANIZATION_ID}/")
    assert second.startswith(f"{TEST_ORGANIZATION_ID}/")
    assert first != second


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.supabase.co/storage/v1/s3",
        "https://qpwnywbktuyeudtwdgsh.supabase.co/storage/v1/s3",
        "https://other-project.storage.supabase.co/storage/v1/s3",
        "https://prefix.qpwnywbktuyeudtwdgsh.storage.supabase.co/storage/v1/s3",
        "http://qpwnywbktuyeudtwdgsh.storage.supabase.co/storage/v1/s3",
        "https://user:token@qpwnywbktuyeudtwdgsh.storage.supabase.co/storage/v1/s3",
        "https://qpwnywbktuyeudtwdgsh.storage.supabase.co/storage/v1/s3?acl=public",
        "https://qpwnywbktuyeudtwdgsh.storage.supabase.co:444/storage/v1/s3",
    ],
)
def test_live_run_refuses_wrong_project_endpoint_before_creating_a_client(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    _hide_config(monkeypatch)
    _set_complete_test_config(monkeypatch, _DISPOSABLE_TEST_BUCKET)
    monkeypatch.setenv(_CONFIG_NAMES[0], endpoint)
    _forbid_s3_client(monkeypatch)

    with pytest.raises(pytest.fail.Exception, match="verified test project"):
        config = require_storage_test_config()
        _require_supabase_endpoint(config.endpoint)
        _verification_client(config)


def test_verified_project_endpoint_passes_the_host_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_s3_client(monkeypatch)

    _require_supabase_endpoint(_VERIFIED_STORAGE_ENDPOINT)


def test_non_supabase_endpoint_still_fails_before_creating_a_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hide_config(monkeypatch)
    _set_complete_test_config(monkeypatch, _DISPOSABLE_TEST_BUCKET)
    monkeypatch.setenv(
        _CONFIG_NAMES[0],
        "https://storage.example.test/storage/v1/s3",
    )
    _forbid_s3_client(monkeypatch)

    with pytest.raises(pytest.fail.Exception, match="Supabase Storage S3 endpoint"):
        config = require_storage_test_config()
        _require_supabase_endpoint(config.endpoint)
        _verification_client(config)


@pytest.mark.supabase_storage
def test_supabase_storage_round_trip_and_privacy() -> None:
    config = require_storage_test_config()
    _require_supabase_endpoint(config.endpoint)
    storage = _adapter(config)
    verifier = _verification_client(config)
    key = fresh_test_key()
    inspected = inspect_evidence_file("notes.pdf", b"%PDF-1.4\nstorage-test\n")
    try:
        _assert_absent(verifier, config.bucket, key)
        storage.put_new(key, inspected.content, inspected.media_type)
        downloaded = _read_object(verifier, config.bucket, key)
        assert downloaded == inspected.content
        assert hashlib.sha256(downloaded).hexdigest() == inspected.checksum_sha256
        _assert_private(config, key, inspected.content)
        storage.delete(key)
        storage.delete(key)
        _assert_absent(verifier, config.bucket, key)
    finally:
        storage.delete(key)


@pytest.mark.supabase_storage
def test_supabase_storage_database_failure_deletes_the_object(tmp_path) -> None:
    config = require_storage_test_config()
    _require_supabase_endpoint(config.endpoint)
    storage = _adapter(config)
    recorded = _RecordingStorage(storage)
    repository, _, token = _ready(
        tmp_path,
        recorded,
        organization_id=UUID(TEST_ORGANIZATION_ID),
    )

    def fail(self, *args, **kwargs):
        raise PersistenceError("database failed")

    repository._commit_file = fail.__get__(repository, SubmissionRepository)
    try:
        with pytest.raises(PersistenceError, match="database failed"):
            repository.public_submit_file(
                token,
                description="Synthetic file",
                reference=None,
                submitter_name=None,
                inspected=inspect_evidence_file("notes.pdf", b"%PDF-1.4\n"),
            )
        assert recorded.keys
        verifier = _verification_client(config)
        for key in recorded.keys:
            _assert_absent(verifier, config.bucket, key)
        assert _use_count(repository) == 0
    finally:
        for key in recorded.keys:
            storage.delete(key)


class _RecordingStorage:
    """Delegates to the adapter and remembers keys written in this test."""

    def __init__(self, storage: S3CompatibleObjectStorage) -> None:
        self._storage = storage
        self.keys: list[str] = []

    def put_new(self, key: str, body: bytes, media_type: str) -> None:
        if not key.startswith(f"{TEST_ORGANIZATION_ID}/"):
            raise AssertionError("test object is outside the test prefix")
        if self._storage.exists(key):
            raise AssertionError("refusing to write a key that already exists")
        self.keys.append(key)
        self._storage.put_new(key, body, media_type)

    def get(self, key: str) -> bytes:
        return self._storage.get(key)

    def delete(self, key: str) -> None:
        self._storage.delete(key)

    def exists(self, key: str) -> bool:
        return self._storage.exists(key)


def _set_complete_test_config(monkeypatch: pytest.MonkeyPatch, bucket: str) -> None:
    monkeypatch.setenv(OPT_IN_FLAG, "1")
    monkeypatch.setenv(_CONFIG_NAMES[0], "https://example.supabase.co/storage/v1/s3")
    monkeypatch.setenv(_CONFIG_NAMES[1], "us-east-1")
    monkeypatch.setenv(_CONFIG_NAMES[2], bucket)
    monkeypatch.setenv(_CONFIG_NAMES[3], "example-access-key")
    monkeypatch.setenv(_CONFIG_NAMES[4], "example-secret")


def _hide_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OPT_IN_FLAG, raising=False)
    for name in _CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)


def _write_unused_dotenv(tmp_path) -> None:
    (tmp_path / ".env").write_text(
        "COMMITMENTOPS_STORAGE_INTEGRATION=1\n"
        "TEST_EVIDENCE_STORAGE_ENDPOINT=https://example.supabase.co/storage/v1/s3\n"
        "TEST_EVIDENCE_STORAGE_REGION=us-east-1\n"
        "TEST_EVIDENCE_STORAGE_BUCKET=evidence-test\n"
        "TEST_EVIDENCE_STORAGE_ACCESS_KEY_ID=example-access-key\n"
        "TEST_EVIDENCE_STORAGE_SECRET_ACCESS_KEY=example-secret\n"
        "SUPABASE_SERVICE_ROLE_KEY=example-service-role\n"
    )


def _require_supabase_endpoint(endpoint: str) -> None:
    parts = urllib.parse.urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    if not host.endswith(".supabase.co") or not path.endswith("/storage/v1/s3"):
        pytest.fail("test storage endpoint is not a Supabase Storage S3 endpoint")
    if (
        parts.scheme != "https"
        or host != _VERIFIED_STORAGE_HOST
        or path != "/storage/v1/s3"
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.port not in (None, 443)
    ):
        pytest.fail("test storage endpoint is not the verified test project")


def _forbid_s3_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid(*args, **kwargs):
        raise AssertionError("S3 client was created")

    monkeypatch.setattr(
        "app.persistence.object_storage._boto3_s3_client",
        forbid,
    )
    import boto3

    monkeypatch.setattr(boto3, "client", forbid)


def _adapter(config: StorageTestConfig) -> S3CompatibleObjectStorage:
    try:
        return S3CompatibleObjectStorage(
            endpoint=config.endpoint,
            region=config.region,
            bucket=config.bucket,
            access_key_id=config.access_key_id,
            secret_access_key=config.secret_access_key,
        )
    except PersistenceError:
        pytest.fail("test storage configuration was rejected")


def _verification_client(config: StorageTestConfig):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint,
        region_name=config.region,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 1},
            connect_timeout=5,
            read_timeout=30,
            s3={"addressing_style": "path"},
        ),
    )


def _error_code(exc: ClientError) -> str:
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    code = error.get("Code") if isinstance(error, dict) else ""
    return code if isinstance(code, str) else ""


def _assert_absent(client, bucket: str, key: str) -> None:
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if _error_code(exc) in _MISSING:
            return
        raise AssertionError("storage object could not be checked") from None
    raise AssertionError("refusing to write or keep an existing test object")


def _read_object(client, bucket: str, key: str) -> bytes:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError:
        raise AssertionError("stored object could not be read") from None
    body = response["Body"].read()
    if not isinstance(body, bytes):
        raise AssertionError("stored object could not be read")
    return body


def _assert_private(config: StorageTestConfig, key: str, body: bytes) -> None:
    """Confirm this object is not readable without a user session.

    The public object URL is requested with no credentials. The private
    object URL is requested without storage credentials or a user session.
    The project's public anon key is added only when the gateway requires
    an API key. A response that contains the uploaded bytes is a failure
    regardless of status.
    """

    _assert_bytes_unavailable(
        _public_object_url(config, key),
        body,
        "public url returned the object",
    )
    private_url = _private_object_url(config, key)
    status, data = _fetch(private_url, None, len(body))
    if body in data:
        raise AssertionError("private object endpoint returned the object")
    if _gateway_requires_api_key(status, data):
        anon_key = _public_anon_key()
        if anon_key is None:
            raise AssertionError(
                "private-object access could not be checked because the "
                "public anon key is not configured"
            )
        status, data = _fetch(private_url, _anon_headers(anon_key), len(body))
        if body in data:
            raise AssertionError("private object endpoint returned the object")
    if status not in _DENIED:
        raise AssertionError("private object endpoint did not deny access")


def _assert_bytes_unavailable(url: str, body: bytes, message: str) -> None:
    _status, data = _fetch(url, None, len(body))
    if body in data:
        raise AssertionError(message)


def _fetch(
    url: str,
    headers: dict[str, str] | None,
    body_length: int,
) -> tuple[int, bytes]:
    class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise AssertionError("anonymous request was redirected")

    opener = urllib.request.build_opener(_RefuseRedirect)
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, response.read(body_length + 8192)
    except AssertionError:
        raise
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(body_length + 8192)
    except Exception:
        raise AssertionError("anonymous request could not be checked") from None


def _gateway_requires_api_key(status: int, data: bytes) -> bool:
    if status != 401:
        return False
    lowered = data.lower()
    return b"api key" in lowered or b"apikey" in lowered


def _public_anon_key() -> str | None:
    """Return the publishable anon key, never a service-role credential."""

    value = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if not value or "service_role" in value.lower() or "://" in value:
        return None
    if value.count(".") == 2 and _jwt_role(value) != "anon":
        return None
    return value


def _jwt_role(token: str) -> str | None:
    import base64
    import json

    try:
        payload = token.split(".", 2)[1]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding)
        role = json.loads(decoded).get("role")
    except Exception:
        return None
    return role if isinstance(role, str) else None


def _anon_headers(anon_key: str) -> dict[str, str]:
    headers = {"apikey": anon_key}
    if anon_key.count(".") == 2:
        headers["Authorization"] = f"Bearer {anon_key}"
    return headers


def _storage_host(endpoint: str) -> str:
    parts = urllib.parse.urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    suffix = ".storage.supabase.co"
    if host.endswith(suffix):
        return f"{host[: -len(suffix)]}.supabase.co"
    return host


def _object_path(config: StorageTestConfig, key: str, *, public: bool) -> str:
    quoted = "/".join(urllib.parse.quote(part) for part in key.split("/"))
    kind = "object/public" if public else "object"
    return (
        f"https://{_storage_host(config.endpoint)}/storage/v1/"
        f"{kind}/{config.bucket}/{quoted}"
    )


def _public_object_url(config: StorageTestConfig, key: str) -> str:
    return _object_path(config, key, public=True)


def _private_object_url(config: StorageTestConfig, key: str) -> str:
    return _object_path(config, key, public=False)
