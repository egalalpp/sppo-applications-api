"""Задачи Celery-воркера: распознавание вложений (OCR) с автопроверками и отправка писем.

Пайплайн из schema_zayavok_sppo.md, раздел 5:
файлы уже в S3 -> распознавание каждого вложения (Tesseract) -> проверки -> checks[] и ocr в документ
-> заявка ждёт оператора. Автоматических отказов нет.

Сбой распознавания — внутреннее дело оператора, студент о нём не знает: заявка остаётся на
рассмотрении, в ocr.state пишется ocr_failed, а оператор может открыть заявку и нажать
«Перезапустить OCR» (POST /applications/{id}/ocr/restart).
"""

import logging
from statistics import mean

import redis
from bson import ObjectId
from celery import Task
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app.cache import app_status_keys
from app.config import get_settings
from app.db import APPLICATION_TYPES, APPLICATIONS, utcnow
from app.domain import OcrState
from app import storage
from app.services import mailer, ocr
from app.services.checks import build_auto_checks, build_manual_checks, merge_checks
from app.worker.celery_app import PROCESS_APPLICATION, SEND_EMAIL, celery_app

log = logging.getLogger(__name__)
settings = get_settings()

# Клиенты создаются лениво — уже в дочернем процессе воркера, после fork.
_mongo: MongoClient | None = None
_redis: redis.Redis | None = None


def _db():
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(settings.mongo_dsn, tz_aware=True)
    return _mongo[settings.mongo_db]


def _cache() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(settings.redis_cache_url)
    return _redis


def _set_ocr(app_id: ObjectId, state: OcrState, task_id: str | None, *, error: str | None = None,
             accuracy: int | None = None, keep_accuracy: bool = False) -> None:
    now = utcnow()
    fields = {"ocr.state": state, "ocr.task_id": task_id, "ocr.updated_at": now, "ocr.error": error,
              "updated_at": now}
    if not keep_accuracy:
        fields["ocr.accuracy"] = accuracy
    _db()[APPLICATIONS].update_one({"_id": app_id}, {"$set": fields})
    _cache().delete(*app_status_keys(str(app_id)))


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


def _ocr_settings() -> ocr.OcrSettings:
    return ocr.OcrSettings(
        lang=settings.ocr_lang,
        max_side_px=settings.ocr_max_side_px,
        max_pdf_pages=settings.ocr_max_pdf_pages,
        timeout_sec=settings.ocr_timeout_sec,
    )


def _ocr_attachment(app_id: ObjectId, att: dict, cfg: ocr.OcrSettings) -> tuple[dict, str | None]:
    """Распознаёт одно вложение и сохраняет результат. Возвращает (результат, текст или None при сбое)."""
    now = utcnow()
    text: str | None = None
    try:
        data = storage.get_object_bytes(att["storage"]["key"])
        result, text = ocr.analyze(data, att["storage"]["content_type"], att["type"], cfg)
        result["finished_at"] = now
    except ocr.OcrError as exc:
        log.warning("OCR failed for %s/%s: %s", app_id, att["attachment_id"], exc)
        result = {"status": "failed", "engine": ocr.ENGINE, "error": str(exc)[:300], "notes": [],
                  "finished_at": now}
    except Exception as exc:  # noqa: BLE001 — недоступное хранилище, нехватка памяти и т.п.
        log.exception("Unexpected OCR error for %s/%s", app_id, att["attachment_id"])
        result = {"status": "failed", "engine": ocr.ENGINE, "error": f"{type(exc).__name__}: {exc}"[:300],
                  "notes": [], "finished_at": now}
    _db()[APPLICATIONS].update_one(
        {"_id": app_id, "attachments.attachment_id": att["attachment_id"]},
        {"$set": {"attachments.$.ocr": result}},
    )
    return result, text


class OcrTask(Task):
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        # Все повторы исчерпаны (например, MongoDB недоступна): не оставляем заявку в «running».
        try:
            _set_ocr(ObjectId(args[0]), OcrState.ocr_failed, task_id, error=f"{type(exc).__name__}"[:300])
        except Exception:  # noqa: BLE001
            log.exception("Cannot mark application %s as ocr_failed", args)


@celery_app.task(name=PROCESS_APPLICATION, bind=True, base=OcrTask, autoretry_for=(PyMongoError,),
                 retry_backoff=5, retry_backoff_max=60, max_retries=3)
def process_application(self, application_id: str) -> dict:
    db = _db()
    app_id = ObjectId(application_id)
    app = db[APPLICATIONS].find_one({"_id": app_id})
    if app is None:
        log.warning("Application %s not found, skipping", application_id)
        return {"application_id": application_id, "skipped": True}

    task_id = self.request.id
    _set_ocr(app_id, OcrState.running, task_id)

    texts: dict[str, str] = {}
    confidences: list[int] = []
    failed = 0
    if settings.ocr_enabled:
        cfg = _ocr_settings()
        for att in app.get("attachments", []):
            result, text = _ocr_attachment(app_id, att, cfg)
            if result["status"] != "done":
                failed += 1
                continue
            texts[att["type"]] = (texts.get(att["type"], "") + "\n" + text).strip() if text is not None else ""
            if result.get("confidence") is not None:
                confidences.append(result["confidence"])
    else:
        failed = len(app.get("attachments", [])) or 1

    app = db[APPLICATIONS].find_one({"_id": app_id})
    benefit_type = db[APPLICATION_TYPES].find_one({"_id": app["basis"]}) or {}
    now = utcnow()
    auto = build_auto_checks(app, benefit_type, _find_duplicates(app), now, ocr_texts=texts)
    manual = build_manual_checks(benefit_type, now)
    checks = merge_checks(app.get("checks", []), auto, manual)

    state = OcrState.ocr_failed if failed else OcrState.ready_for_review
    accuracy = round(mean(confidences)) if confidences else None
    db[APPLICATIONS].update_one(
        {"_id": app_id},
        {"$set": {
            "checks": checks,
            "ocr.state": state,
            "ocr.task_id": task_id,
            "ocr.engine": ocr.ENGINE,
            "ocr.accuracy": accuracy,
            "ocr.error": f"Не удалось обработать вложений: {failed}" if failed else None,
            "ocr.updated_at": now,
            "updated_at": now,
        }},
    )
    _cache().delete(*app_status_keys(application_id))
    return {"application_id": application_id, "ocr_state": state, "accuracy": accuracy, "failed_attachments": failed}


@celery_app.task(name=SEND_EMAIL, bind=True, autoretry_for=(mailer.TransientMailError,),
                 retry_backoff=10, retry_backoff_max=600, max_retries=5)
def send_email(self, to: str, subject: str, text: str, html: str | None = None) -> dict:
    """Отправка письма. Временные сбои повторяются с растущей паузой, постоянные — нет."""
    try:
        backend = mailer.send(to, subject, text, html)
    except mailer.PermanentMailError:
        log.exception("Mail to %s was rejected permanently", mailer.mask_address(to))
        raise
    return {"to": mailer.mask_address(to), "backend": backend}
