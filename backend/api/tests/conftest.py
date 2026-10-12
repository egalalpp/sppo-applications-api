import os

# Минимальное окружение, чтобы импортировать приложение без docker-compose.
os.environ.setdefault("MONGO_APP_USER", "test")
os.environ.setdefault("MONGO_APP_PASSWORD", "test")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-123")
os.environ.setdefault("S3_ACCESS_KEY", "test")
os.environ.setdefault("S3_SECRET_KEY", "test-secret")
