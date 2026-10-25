import os

# Минимальное окружение, чтобы импортировать приложение без docker-compose.
os.environ.setdefault("MONGO_APP_USER", "test")
os.environ.setdefault("MONGO_APP_PASSWORD", "test")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-123")
os.environ.setdefault("HASH_PEPPER", "test-pepper-test-pepper-1234")
os.environ.setdefault("S3_ACCESS_KEY", "test")
os.environ.setdefault("S3_SECRET_KEY", "test-secret")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_redis_breaker():
    """Состояние «Redis недоступен» живёт в модуле: каждый тест начинает с чистого."""
    from app import cache

    cache._down_until = 0.0
    yield
    cache._down_until = 0.0
