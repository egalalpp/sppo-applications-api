"""Конфигурация приложения. Все значения берутся из переменных окружения (.env)."""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_name: str = "SPPO Applications API"
    environment: str = "dev"
    cors_origins: list[str] = []

    # MongoDB
    mongo_host: str = "mongo"
    mongo_port: int = 27017
    mongo_db: str = "sppo"
    mongo_app_user: str
    mongo_app_password: str

    # Redis: db 0 — кэш, db 1 — брокер Celery, db 2 — результаты задач
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str
    redis_cache_db: int = 0
    redis_broker_db: int = 1
    redis_result_db: int = 2
    # Необязательно: другой брокер Celery, например amqp://user:pass@rabbitmq:5672//
    # (тогда проверка переполнения очереди через LLEN Redis не работает).
    celery_broker: str | None = None

    # JWT
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_ttl_min: int = 30
    refresh_token_ttl_days: int = 7

    # Первый администратор (создаётся сервисом init, если админов ещё нет)
    admin_email: str | None = None
    admin_password: str | None = None

    # S3 (SeaweedFS в docker-compose; подойдёт любое S3-совместимое хранилище)
    s3_endpoint: str = "http://s3:8333"
    s3_public_endpoint: str = "http://localhost:8333"
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str = "sppo-docs"
    s3_region: str = "us-east-1"
    presigned_ttl_sec: int = 600
    max_upload_mb: int = 10

    # Очередь и кэш
    celery_queue: str = "applications"
    max_queue_length: int = 50_000  # K из модели M/M/c/K (PD.ipynb)
    benefit_types_cache_ttl_sec: int = 3600
    status_cache_ttl_sec: int = 5
    ocr_stub_delay_sec: float = 2.0

    current_period: str = "2026-autumn"

    @property
    def mongo_uri(self) -> str:
        user = quote_plus(self.mongo_app_user)
        password = quote_plus(self.mongo_app_password)
        return (
            f"mongodb://{user}:{password}@{self.mongo_host}:{self.mongo_port}/"
            f"{self.mongo_db}?authSource={self.mongo_db}"
        )

    def redis_url(self, db: int) -> str:
        password = quote_plus(self.redis_password)
        return f"redis://:{password}@{self.redis_host}:{self.redis_port}/{db}"

    @property
    def redis_cache_url(self) -> str:
        return self.redis_url(self.redis_cache_db)

    @property
    def celery_broker_url(self) -> str:
        return self.celery_broker or self.redis_url(self.redis_broker_db)

    @property
    def celery_result_backend(self) -> str:
        return self.redis_url(self.redis_result_db)


@lru_cache
def get_settings() -> Settings:
    return Settings()
