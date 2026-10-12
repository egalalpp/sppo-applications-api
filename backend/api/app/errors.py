"""Единый формат ошибок: application/problem+json (RFC 9457)."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"

_TITLES = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    422: "Unprocessable Content",
    429: "Too Many Requests",
    503: "Service Unavailable",
}


class ApiError(Exception):
    def __init__(self, status: int, detail: str, *, headers: dict | None = None, extra: dict | None = None):
        self.status = status
        self.detail = detail
        self.headers = headers
        self.extra = extra or {}


def problem(request: Request, status: int, detail: str | None, *, headers=None, extra=None) -> JSONResponse:
    body = {
        "type": "about:blank",
        "title": _TITLES.get(status, "Error"),
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        **(extra or {}),
    }
    return JSONResponse(body, status_code=status, headers=headers, media_type=PROBLEM_JSON)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return problem(request, exc.status, exc.detail, headers=exc.headers, extra=exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else None
        return problem(request, exc.status_code, detail, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # Значения полей (input) в ответ не возвращаем: там могут быть пароль или паспорт.
        errors = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return problem(request, 422, "Ошибка валидации запроса", extra={"errors": errors})


# Описание ошибок для OpenAPI, чтобы в документации был виден формат.
def responses(*codes: int) -> dict:
    return {
        code: {
            "description": _TITLES.get(code, "Error"),
            "content": {PROBLEM_JSON: {"schema": {"$ref": "#/components/schemas/Problem"}}},
        }
        for code in codes
    }
