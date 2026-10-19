"""S3-совместимое хранилище сканов.

Работает с любым S3 API: SeaweedFS в docker-compose, Cloudflare R2 или другое облако.
Хранилище выбирается переменными S3_ENDPOINT_URL, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET, S3_REGION
(для Cloudflare R2: S3_REGION=auto, S3_ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com).

В MongoDB хранится только bucket + key + sha256, сам файл лежит здесь.
Бакет приватный, скачивание — только по presigned URL с коротким TTL.
"""

import logging
from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import get_settings

log = logging.getLogger(__name__)


def build_client(endpoint_url: str, access_key: str, secret_key: str, region: str = "us-east-1"):
    """Клиент boto3 для произвольного S3-совместимого endpoint."""
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            # Адреса вида https://endpoint/bucket/key: работает и у SeaweedFS, и у R2.
            s3={"addressing_style": "path"},
            # boto3 >= 1.36 по умолчанию добавляет CRC32 ко всем загрузкам; не все
            # S3-совместимые хранилища это поддерживают — включаем только где обязательно.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=30,
        ),
    )


def _settings_client(endpoint_url: str):
    settings = get_settings()
    return build_client(endpoint_url, settings.s3_access_key, settings.s3_secret_key, settings.s3_region)


@lru_cache
def internal_client():
    """Клиент для загрузки, чтения и удаления файлов: ходит в S3 по адресу S3_ENDPOINT_URL."""
    return _settings_client(get_settings().s3_endpoint_url)


@lru_cache
def public_client():
    """Клиент только для подписи ссылок: подпись привязана к хосту, который увидит браузер.

    В docker-compose это localhost:8333 (внутри сети хранилище доступно как s3:8333),
    для облачного хранилища адрес один и тот же.
    """
    settings = get_settings()
    if settings.s3_public_url == settings.s3_endpoint_url:
        return internal_client()
    return _settings_client(settings.s3_public_url)


def object_key(application_id: str, attachment_id: str, ext: str) -> str:
    # Без ФИО и прочих персональных данных в пути.
    return f"applications/{application_id}/{attachment_id}.{ext}"


def put_object(key: str, data: bytes, content_type: str) -> None:
    internal_client().put_object(
        Bucket=get_settings().s3_bucket, Key=key, Body=data, ContentType=content_type
    )


def get_object_bytes(key: str) -> bytes:
    response = internal_client().get_object(Bucket=get_settings().s3_bucket, Key=key)
    return response["Body"].read()


def delete_object(key: str) -> None:
    internal_client().delete_object(Bucket=get_settings().s3_bucket, Key=key)


def presigned_get_url(key: str, filename: str) -> str:
    settings = get_settings()
    return public_client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.s3_bucket,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
        },
        ExpiresIn=settings.presigned_ttl_sec,
    )


def ensure_bucket() -> bool:
    """Создаёт бакет, если его нет. Возвращает True, если бакет был создан.

    В облаке токен часто выдан только на чтение и запись объектов, и создавать бакеты
    ему нельзя (Cloudflare R2: «Object Read & Write»). Тогда это не ошибка: бакет создан
    вручную в панели, а здесь мы только предупреждаем.
    """
    client = internal_client()
    bucket = get_settings().s3_bucket
    try:
        client.head_bucket(Bucket=bucket)
        return False
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in ("404", "NoSuchBucket", "NotFound"):
            log.warning("Cannot check bucket %s (%s): assuming it exists", bucket, code)
            return False
    try:
        client.create_bucket(Bucket=bucket)
    except ClientError as exc:
        log.warning("Bucket %s does not exist and cannot be created (%s): create it manually",
                    bucket, exc.response.get("Error", {}).get("Code"))
        return False
    return True


def ping() -> None:
    """Проверка доступности: листинг одного объекта работает с правами только на объекты,
    в отличие от HeadBucket, который у облачных токенов часто запрещён."""
    internal_client().list_objects_v2(Bucket=get_settings().s3_bucket, MaxKeys=1)
