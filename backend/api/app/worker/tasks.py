"""Задачи Celery-воркера: OCR (заглушка) и автоматические проверки заявки.

Пайплайн из schema_zayavok_sppo.md, раздел 5:
файлы уже в S3 -> OCR каждого вложения -> проверки -> checks[] в документ
-> заявка ждёт модератора. Автоматических отказов нет.
"""

import logging
import time

import redis
from bson import ObjectId
from pymongo import MongoClient

from app.cache import app_status_key
from app.config import get_settings
from app.db import APPLICATION_TYPES, APPLICATIONS, utcnow
from app.domain import ProcessingState
from app.services.checks import build_auto_checks, build_manual_checks, merge_checks
from app.worker.celery_app import PROCESS_APPLICATION, celery_app

log = logging.getLogger(__name__)
settings = get_settings()

# Клиенты создаются лениво — уже в дочернем процессе воркера, после fork.
_mongo: MongoClient | None = None
_redis: redis.Redis | None = None


def _db():
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(settings.mongo_uri, tz_aware=True)
    return _mongo[settings.mongo_db]


def _cache() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(settings.redis_cache_url)
    return _redis


def _set_processing(app_id: ObjectId, state: ProcessingState, task_id: str, error: str | None = None) -> None:
    now = utcnow()
    _db()[APPLICATIONS].update_one(
        {"_id": app_id},
        {"$set": {
            "processing": {"state": state, "task_id": task_id, "updated_at": now, "error": error},
            "updated_at": now,
        }},
    )
    _cache().delete(app_status_key(str(app_id)))


def _find_duplicates(app: dict) -> dict:
    """Те же файлы или тот же счёт в заявках других студентов (раздел 6.6 схемы)."""
    db = _db()
    others = {"_id": {"$ne": app["_id"]}, "applicant_user_id": {"$ne": app["applicant_user_id"]}}

    hashes = [a["storage"]["sha256"] for a in app.get("attachments", [])]
    files = []
    if hashes:
        cursor = db[APPLICATIONS].find(
            {**others, "attachments.storage.sha256": {"$in": hashes}}, {"_id": 1}
        ).limit(20)
        files = [str(d["_id"]) for d in cursor]

    payout = []
    account = app.get("payout", {}).get("account_number")
    if account:
        cursor = db[APPLICATIONS].find(
            {**others, "period": app["period"], "payout.account_number": account}, {"_id": 1}
        ).limit(20)
        payout = [str(d["_id"]) for d in cursor]

    return {"files": files, "payout": payout}


def _run_ocr_stub(app: dict) -> None:
    """Вместо PaddleOCR — пауза, имитирующая время распознавания одного файла."""
    for att in app.get("attachments", []):
        if att.get("ocr", {}).get("status") == "done":
            continue
        time.sleep(settings.ocr_stub_delay_sec)
        _db()[APPLICATIONS].update_one(
            {"_id": app["_id"], "attachments.attachment_id": att["attachment_id"]},
            {"$set": {"attachments.$.ocr": {
                "status": "done", "engine": "stub", "extracted": {}, "confidence": None,
            }}},
        )


@celery_app.task(name=PROCESS_APPLICATION, bind=True, max_retries=3, default_retry_delay=10)
def process_application(self, application_id: str) -> dict:
    db = _db()
    app_id = ObjectId(application_id)
    app = db[APPLICATIONS].find_one({"_id": app_id})
    if app is None:
        log.warning("Application %s not found, skipping", application_id)
        return {"application_id": application_id, "skipped": True}

    task_id = self.request.id
    _set_processing(app_id, ProcessingState.running, task_id)
    try:
        _run_ocr_stub(app)
        app = db[APPLICATIONS].find_one({"_id": app_id})
        benefit_type = db[APPLICATION_TYPES].find_one({"_id": app["basis"]}) or {}

        now = utcnow()
        auto = build_auto_checks(app, benefit_type, _find_duplicates(app), now)
        manual = build_manual_checks(benefit_type, now)
        checks = merge_checks(app.get("checks", []), auto, manual)

        db[APPLICATIONS].update_one(
            {"_id": app_id},
            {"$set": {
                "checks": checks,
                "processing": {"state": ProcessingState.done, "task_id": task_id, "updated_at": now, "error": None},
                "updated_at": now,
            }},
        )
        _cache().delete(app_status_key(application_id))
    except Exception as exc:
        log.exception("Processing of %s failed (attempt %d)", application_id, self.request.retries + 1)
        final = self.request.retries >= self.max_retries
        # Между попытками заявка остаётся queued: модератор не возьмёт её в работу,
        # а клиент не примет временную ошибку за окончательную.
        state = ProcessingState.failed if final else ProcessingState.queued
        _set_processing(app_id, state, task_id, error=str(exc)[:500])
        if final:
            raise
        raise self.retry(exc=exc)

    summary = {}
    for check in checks:
        summary[check["status"]] = summary.get(check["status"], 0) + 1
    return {"application_id": application_id, "checks": summary}
