"""Конфигурация приложения. Все значения берутся из переменных окружения (.env).

Подключения можно задать двумя способами:
- по частям (docker compose): MONGO_HOST + MONGO_APP_USER + MONGO_APP_PASSWORD, REDIS_HOST + REDIS_PASSWORD;
- готовой строкой (облачные сервисы): MONGO_URI (MongoDB Atlas), REDIS_URL (Render Key Value, Upstash).
"""

from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: пустая переменная (EMAIL_BACKEND=, SMTP_USER=) — то же, что незаданная
    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)

    app_name: str = "SPPO Applications API"
    environment: str = "dev"
    cors_origins: list[str] = []

    # MongoDB
    mongo_uri: str | None = None  # готовая строка подключения, например mongodb+srv://... (Atlas)
    mongo_host: str = "mongo"
    mongo_port: int = 27017
    mongo_db: str = "sppo"
    mongo_app_user: str | None = None
    mongo_app_password: str | None = None

    # Redis: db 0 — кэш, db 1 — брокер Celery, db 2 — результаты задач
    redis_dsn: str | None = Field(default=None, validation_alias=AliasChoices("redis_url"))
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str | None = None
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
    # Демо-учётки student@rut.ru / operator@rut.ru / admin@rut.ru с известными паролями (app/seed.py).
    # Только для разработки и демонстрации: на публичном сервере с реальными данными держать выключенным.
    seed_demo_users: bool = False

    # S3-совместимое хранилище: SeaweedFS (docker compose) или Cloudflare R2 и любое другое облако
    s3_endpoint_url: str = Field(
        default="http://s3:8333", validation_alias=AliasChoices("s3_endpoint_url", "s3_endpoint")
    )
    # Адрес, который увидит браузер в presigned-ссылках. Для облака совпадает с s3_endpoint_url.
    s3_public_endpoint_url: str | None = Field(
        default=None, validation_alias=AliasChoices("s3_public_endpoint_url", "s3_public_endpoint")
    )
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str = "sppo-docs"
    s3_region: str = "us-east-1"  # для Cloudflare R2 — "auto"
    presigned_ttl_sec: int = 600
    max_upload_mb: int = 10

    # Очереди Celery и кэш
    celery_queue: str = "applications"  # OCR и проверки заявок
    celery_mail_queue: str = "mail"  # письма: не стоят в очереди за долгим OCR
    max_queue_length: int = 50_000  # K из модели M/M/c/K (PD.ipynb)
    benefit_types_cache_ttl_sec: int = 3600
    status_cache_ttl_sec: int = 5

    # OCR (Tesseract)
    ocr_enabled: bool = True
    ocr_lang: str = "rus+eng"
    ocr_max_side_px: int = 2000  # большие фото уменьшаются: так быстрее и почти не влияет на точность
    ocr_max_pdf_pages: int = 3
    ocr_timeout_sec: int = 180  # на одну страницу
    # Если воркер упал или очередь потеряна (бесплатные хостинги перезапускаются), заявка остаётся в
    # состоянии queued/running. Через столько минут оператор вправе перезапустить OCR.
    ocr_stale_minutes: int = 15

    # Почта: smtp | brevo | log. Если не задано — выбирается по заполненным переменным.
    email_backend: Literal["smtp", "brevo", "log"] | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_pass: str | None = None
    smtp_starttls: bool | None = None  # по умолчанию включён, если указан логин; порт 465 — SSL
    brevo_api_key: str | None = None  # HTTPS API Brevo: работает там, где исходящий SMTP закрыт
    mail_from: str = "СППО РУТ (МИИТ) <noreply@sppo.local>"
    frontend_url: str | None = None  # для ссылки на личный кабинет в письмах

    current_period: str = "2026-autumn"

    @model_validator(mode="after")
    def _check_connections(self) -> "Settings":
        if not self.mongo_uri and not (self.mongo_app_user and self.mongo_app_password):
            raise ValueError("Задайте MONGO_URI или MONGO_APP_USER и MONGO_APP_PASSWORD")
        if not self.redis_dsn and not self.redis_password:
            raise ValueError("Задайте REDIS_URL или REDIS_PASSWORD")
        return self

    @property
    def mongo_dsn(self) -> str:
        if self.mongo_uri:
            return self.mongo_uri
        user = quote_plus(self.mongo_app_user or "")
        password = quote_plus(self.mongo_app_password or "")
        return (
            f"mongodb://{user}:{password}@{self.mongo_host}:{self.mongo_port}/"
            f"{self.mongo_db}?authSource={self.mongo_db}"
        )

    def redis_url(self, db: int) -> str:
        if self.redis_dsn:
            parts = urlsplit(self.redis_dsn)
            return urlunsplit((parts.scheme, parts.netloc, f"/{db}", parts.query, ""))
        password = quote_plus(self.redis_password or "")
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

    @property
    def s3_public_url(self) -> str:
        return self.s3_public_endpoint_url or self.s3_endpoint_url

    @property
    def mail_backend(self) -> str:
        if self.email_backend:
            return self.email_backend
        if self.brevo_api_key:
            return "brevo"
        return "smtp" if self.smtp_host else "log"

    @property
    def smtp_use_ssl(self) -> bool:
        return self.smtp_port == 465

    @property
    def smtp_use_starttls(self) -> bool:
        if self.smtp_use_ssl:
            return False
        if self.smtp_starttls is not None:
            return self.smtp_starttls
        return bool(self.smtp_user)


@lru_cache
def get_settings() -> Settings:
    return Settings()
