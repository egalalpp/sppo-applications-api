"""S3-совместимое хранилище сканов (SeaweedFS в docker-compose, подойдёт любое S3).

В MongoDB хранится только bucket + key + sha256, сам файл лежит здесь.
Бакет приватный, скачивание — только по presigned URL с коротким TTL.
"""

from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import get_settings


def _client(endpoint: str):
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            # boto3 >= 1.36 по умолчанию добавляет CRC32 ко всем загрузкам; не все
            # S3-совместимые хранилища это поддерживают — включаем только где обязательно.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


@lru_cache
def internal_client():
    """Клиент для загрузки и удаления файлов: ходит в S3 по сети docker."""
    return _client(get_settings().s3_endpoint)


@lru_cache
def public_client():
    """Клиент только для подписи ссылок: подпись привязана к хосту, который увидит браузер."""
    return _client(get_settings().s3_public_endpoint)


def object_key(application_id: str, attachment_id: str, ext: str) -> str:
    # Без ФИО и прочих персональных данных в пути.
    return f"applications/{application_id}/{attachment_id}.{ext}"


def put_object(key: str, data: bytes, content_type: str) -> None:
    internal_client().put_object(
        Bucket=get_settings().s3_bucket, Key=key, Body=data, ContentType=content_type
    )


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
    """Создаёт бакет, если его нет. Возвращает True, если бакет был создан."""
    client = internal_client()
    bucket = get_settings().s3_bucket
    try:
        client.head_bucket(Bucket=bucket)
        return False
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket", "NotFound"):
            raise
    client.create_bucket(Bucket=bucket)
    return True


def ping() -> None:
    internal_client().head_bucket(Bucket=get_settings().s3_bucket)
