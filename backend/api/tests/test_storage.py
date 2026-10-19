import pytest

from app import storage
from app.config import get_settings

R2 = "https://0123456789abcdef.r2.cloudflarestorage.com"


def _clear():
    for fn in (get_settings, storage.internal_client, storage.public_client):
        fn.cache_clear()


@pytest.fixture
def r2_env(monkeypatch):
    for key in ("S3_ENDPOINT", "S3_PUBLIC_ENDPOINT", "S3_PUBLIC_ENDPOINT_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("S3_ENDPOINT_URL", R2)
    monkeypatch.setenv("S3_REGION", "auto")
    monkeypatch.setenv("S3_BUCKET", "sppo-documents")
    _clear()
    yield
    monkeypatch.undo()
    _clear()


def test_client_accepts_custom_endpoint():
    client = storage.build_client("https://s3.example.com", "key", "secret", "auto")
    assert client.meta.endpoint_url == "https://s3.example.com"
    assert client.meta.region_name == "auto"


def test_client_for_local_seaweedfs():
    client = storage.build_client("http://s3:8333", "key", "secret")
    assert client.meta.endpoint_url == "http://s3:8333"


def test_r2_settings_drive_the_client(r2_env):
    assert storage.internal_client().meta.endpoint_url == R2
    assert storage.public_client() is storage.internal_client()  # адрес для браузера совпадает


def test_presigned_url_for_r2(r2_env):
    url = storage.presigned_get_url("applications/abc/def.pdf", "passport.pdf")
    assert url.startswith(f"{R2}/sppo-documents/applications/abc/def.pdf?")
    assert "X-Amz-Signature=" in url and "X-Amz-Expires=600" in url
    assert "passport.pdf" in url


def test_separate_public_endpoint(monkeypatch):
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://s3:8333")
    monkeypatch.setenv("S3_PUBLIC_ENDPOINT_URL", "http://localhost:8333")
    _clear()
    try:
        assert storage.internal_client().meta.endpoint_url == "http://s3:8333"
        assert storage.presigned_get_url("k", "f.pdf").startswith("http://localhost:8333/")
    finally:
        monkeypatch.undo()
        _clear()
