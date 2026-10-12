"""Точка входа FastAPI. Документация: /redoc, схема: /openapi.json."""

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html
from fastapi.openapi.utils import get_openapi

from app import cache, db, errors
from app.config import get_settings
from app.routers import admin, applications, attachments, auth, benefit_types, moderation, system
from app.schemas import Problem

API_PREFIX = "/api/v1"
# Версия закреплена: redoc@next, который FastAPI подключает по умолчанию, не грузится в браузере.
REDOC_JS_URL = "https://cdn.jsdelivr.net/npm/redoc@2.5.0/bundles/redoc.standalone.js"

DESCRIPTION = """
Прототип REST API для приёма и проверки заявок студентов на материальную поддержку (СППО РУТ (МИИТ)).

**Авторизация:** получите токен через `POST /api/v1/auth/login` (студент — после
`POST /api/v1/auth/register`, администратор — из `.env`) и передавайте его в заголовке
`Authorization: Bearer <token>`.

Роли: `student` — свои заявки; `moderator` — очередь и проверка; `admin` — всё плюс пользователи и справочник.
Ошибки возвращаются в формате `application/problem+json` (RFC 9457).
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await cache.connect()
    yield
    await cache.close()
    await db.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        # X-Instance показывает, какая реплика ответила — видно работу балансировщика nginx.
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Instance"] = system.INSTANCE
        return response

    errors.install(app)

    app.include_router(system.router)
    for module in (auth, benefit_types, applications, attachments, moderation, admin):
        app.include_router(module.router, prefix=API_PREFIX)

    @app.get("/redoc", include_in_schema=False)
    async def redoc():
        return get_redoc_html(openapi_url=app.openapi_url, title=f"{app.title} - ReDoc", redoc_js_url=REDOC_JS_URL)

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
        schema.setdefault("components", {}).setdefault("schemas", {})["Problem"] = Problem.model_json_schema()
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi
    return app


app = create_app()
