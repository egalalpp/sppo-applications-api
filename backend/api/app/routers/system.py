"""Служебные эндпоинты: health/ready и состояние задач Celery."""

import socket

from celery.result import AsyncResult
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app import cache, db, storage
from app.deps import require_staff
from app.schemas import TaskOut
from app.worker.celery_app import celery_app

router = APIRouter()

INSTANCE = socket.gethostname()


@router.get("/health", tags=["system"], summary="Liveness: процесс жив")
async def health():
    return {"status": "ok", "instance": INSTANCE}


@router.get("/ready", tags=["system"], summary="Readiness: доступны MongoDB, Redis и S3")
async def ready():
    checks = {}
    try:
        await db.get_db().command("ping")
        checks["mongo"] = "ok"
    except Exception as exc:  # noqa: BLE001 — в readiness нужна любая ошибка
        checks["mongo"] = f"error: {type(exc).__name__}"
    try:
        await cache.cache().ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {type(exc).__name__}"
    try:
        await run_in_threadpool(storage.ping)
        checks["s3"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["s3"] = f"error: {type(exc).__name__}"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ok" if ok else "degraded", "instance": INSTANCE, "checks": checks},
                        status_code=200 if ok else 503)


@router.get("/api/v1/tasks/{task_id}", response_model=TaskOut, tags=["tasks"],
            summary="Состояние фоновой задачи Celery (оператор и админ)")
async def task_state(task_id: str, user: dict = Depends(require_staff)):
    state = await run_in_threadpool(lambda: AsyncResult(task_id, app=celery_app).state)
    return {"task_id": task_id, "state": state}
