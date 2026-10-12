"""Вложения заявки: сканы документов в S3, в MongoDB — только ссылка и sha256."""

import hashlib
import uuid

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from app import audit, db, storage
from app.config import get_settings
from app.deps import get_current_user, is_staff, require_student
from app.domain import EDITABLE_STATUSES, MAX_ATTACHMENTS, detect_file_type
from app.errors import ApiError, responses
from app.schemas import AttachmentOut, AttachmentType, DownloadUrlOut
from app.serializers import attachment_out
from app.services import applications as svc
from app.services import benefit_types

router = APIRouter(prefix="/applications/{app_id}/attachments", tags=["attachments"])


def _coll():
    return db.get_db()[db.APPLICATIONS]


@router.post("", response_model=AttachmentOut, status_code=201,
             responses=responses(404, 409, 413, 415, 422),
             summary="Загрузить скан документа (PDF, JPEG или PNG)")
async def upload_attachment(
    app_id: str,
    file: UploadFile = File(...),
    type: AttachmentType = Form(..., description="Тип документа из справочника"),
    user: dict = Depends(require_student),
):
    settings = get_settings()
    doc = await svc.load(app_id, user, {"status": 1, "basis": 1, "attachments.attachment_id": 1})
    if doc["status"] not in EDITABLE_STATUSES:
        raise ApiError(409, f"В статусе {doc['status']} менять вложения нельзя")
    if len(doc.get("attachments", [])) >= MAX_ATTACHMENTS:
        raise ApiError(409, f"Не больше {MAX_ATTACHMENTS} вложений на заявку")

    max_bytes = settings.max_upload_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ApiError(413, f"Файл больше {settings.max_upload_mb} МБ")
    if not data:
        raise ApiError(422, "Пустой файл")
    detected = detect_file_type(data[:16])
    if detected is None:
        raise ApiError(415, "Допустимы только PDF, JPEG и PNG")
    content_type, ext = detected

    # Слот определяется по справочнику: какой обязательный документ закрывает этот файл.
    benefit_type = await benefit_types.get(doc["basis"]) or {"requires": []}
    slot = next((r["slot"] for r in benefit_type["requires"] if type in r["any_of"]), "other")

    attachment_id = uuid.uuid4().hex
    key = storage.object_key(app_id, attachment_id, ext)
    await run_in_threadpool(storage.put_object, key, data, content_type)

    now = db.utcnow()
    attachment = {
        "attachment_id": attachment_id,
        "type": type,
        "slot": slot,
        "original_filename": (file.filename or "")[:200] or None,
        "storage": {
            "bucket": settings.s3_bucket,
            "key": key,
            "content_type": content_type,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        "uploaded_at": now,
        "meta": {},
        "ocr": {"status": "not_started"},
    }
    # Условие в фильтре защищает от гонок: статус и лимит проверяются атомарно с записью.
    try:
        result = await _coll().update_one(
            {
                "_id": doc["_id"],
                "applicant_user_id": user["_id"],
                "status": {"$in": list(EDITABLE_STATUSES)},
                f"attachments.{MAX_ATTACHMENTS - 1}": {"$exists": False},
            },
            {"$push": {"attachments": attachment}, "$set": {"updated_at": now}},
        )
    except BaseException:
        await run_in_threadpool(storage.delete_object, key)
        raise
    if result.modified_count == 0:
        await run_in_threadpool(storage.delete_object, key)
        raise ApiError(409, "Заявка изменилась во время загрузки, повторите")
    return attachment_out(attachment)


@router.get("", response_model=list[AttachmentOut], responses=responses(404), summary="Список вложений")
async def list_attachments(app_id: str, user: dict = Depends(get_current_user)):
    doc = await svc.load(app_id, user, {"attachments": 1})
    return [attachment_out(a) for a in doc.get("attachments", [])]


@router.get("/{attachment_id}/download-url", response_model=DownloadUrlOut, responses=responses(404),
            summary="Временная ссылка на скачивание скана (presigned URL)")
async def download_url(app_id: str, attachment_id: str, user: dict = Depends(get_current_user)):
    doc = await svc.load(app_id, user, {"attachments": 1})
    att = next((a for a in doc.get("attachments", []) if a["attachment_id"] == attachment_id), None)
    if att is None:
        raise ApiError(404, "Вложение не найдено")
    ext = att["storage"]["key"].rsplit(".", 1)[-1]
    url = storage.presigned_get_url(att["storage"]["key"], f"{att['type']}.{ext}")
    if is_staff(user):
        # Доступ сотрудника к персональным данным фиксируется в журнале.
        await audit.log(user, "attachment.download_url", "application", app_id,
                        {"attachment_id": attachment_id, "type": att["type"]})
    return {"url": url, "expires_in": get_settings().presigned_ttl_sec}


@router.delete("/{attachment_id}", status_code=204, responses=responses(404, 409), summary="Удалить вложение")
async def delete_attachment(app_id: str, attachment_id: str, user: dict = Depends(require_student)):
    doc = await svc.load(app_id, user, {"status": 1, "attachments": 1})
    att = next((a for a in doc.get("attachments", []) if a["attachment_id"] == attachment_id), None)
    if att is None:
        raise ApiError(404, "Вложение не найдено")
    result = await _coll().update_one(
        {"_id": doc["_id"], "applicant_user_id": user["_id"], "status": {"$in": list(EDITABLE_STATUSES)}},
        {"$pull": {"attachments": {"attachment_id": attachment_id}}, "$set": {"updated_at": db.utcnow()}},
    )
    if result.modified_count == 0:
        raise ApiError(409, f"В статусе {doc['status']} менять вложения нельзя")
    await run_in_threadpool(storage.delete_object, att["storage"]["key"])
    return Response(status_code=204)
