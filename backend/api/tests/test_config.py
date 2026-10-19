import pytest
from pydantic import ValidationError

from app.config import Settings

ENV_KEYS = ["MONGO_URI", "MONGO_APP_USER", "MONGO_APP_PASSWORD", "REDIS_URL", "REDIS_PASSWORD", "S3_ENDPOINT_URL",
            "S3_ENDPOINT", "S3_PUBLIC_ENDPOINT_URL", "S3_PUBLIC_ENDPOINT", "MONGO_HOST", "REDIS_HOST"]


@pytest.fixture
def env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("S3_ACCESS_KEY", "key")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    return monkeypatch


def test_compose_style_connections(env):
    env.setenv("MONGO_APP_USER", "sppo_app")
    env.setenv("MONGO_APP_PASSWORD", "p@ss/word")
    env.setenv("REDIS_PASSWORD", "redis-pass")
    s = Settings()
    assert s.mongo_dsn == "mongodb://sppo_app:p%40ss%2Fword@mongo:27017/sppo?authSource=sppo"
    assert s.redis_cache_url == "redis://:redis-pass@redis:6379/0"
    assert s.celery_broker_url.endswith("/1") and s.celery_result_backend.endswith("/2")


def test_atlas_uri_is_used_as_is(env):
    uri = "mongodb+srv://sppo_app:pw@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority"
    env.setenv("MONGO_URI", uri)
    env.setenv("REDIS_URL", "redis://red-abc123:6379")
    s = Settings()  # логин и пароль по отдельности не нужны
    assert s.mongo_dsn == uri
    assert s.redis_cache_url == "redis://red-abc123:6379/0"
    assert s.celery_broker_url == "redis://red-abc123:6379/1"


def test_redis_url_with_credentials_and_tls(env):
    env.setenv("MONGO_URI", "mongodb://x")
    env.setenv("REDIS_URL", "rediss://default:tok@eu1-xyz.upstash.io:6379")
    assert Settings().redis_url(2) == "rediss://default:tok@eu1-xyz.upstash.io:6379/2"


def test_missing_connections_fail_loudly(env):
    with pytest.raises(ValidationError, match="MONGO_URI"):
        Settings()
    env.setenv("MONGO_URI", "mongodb://x")
    with pytest.raises(ValidationError, match="REDIS_URL"):
        Settings()


def test_s3_endpoint_url_names(env):
    env.setenv("MONGO_URI", "mongodb://x")
    env.setenv("REDIS_URL", "redis://r")
    env.setenv("S3_ENDPOINT_URL", "https://acct.r2.cloudflarestorage.com")
    s = Settings()
    assert s.s3_endpoint_url == "https://acct.r2.cloudflarestorage.com"
    assert s.s3_public_url == s.s3_endpoint_url  # облако: адрес для браузера тот же

    env.delenv("S3_ENDPOINT_URL")
    env.setenv("S3_ENDPOINT", "http://s3:8333")  # старое имя переменной продолжает работать
    env.setenv("S3_PUBLIC_ENDPOINT", "http://localhost:8333")
    s = Settings()
    assert (s.s3_endpoint_url, s.s3_public_url) == ("http://s3:8333", "http://localhost:8333")


def test_empty_variables_are_treated_as_unset(env):
    """docker compose передаёт незаданные переменные как пустые строки."""
    env.setenv("MONGO_URI", "mongodb://x")
    env.setenv("REDIS_URL", "redis://r")
    for key in ("EMAIL_BACKEND", "SMTP_STARTTLS", "SMTP_USER", "BREVO_API_KEY", "S3_PUBLIC_ENDPOINT_URL", "ADMIN_EMAIL"):
        env.setenv(key, "")
    s = Settings()
    assert s.email_backend is None and s.smtp_starttls is None and s.smtp_user is None
    assert s.s3_public_endpoint_url is None and s.admin_email is None
