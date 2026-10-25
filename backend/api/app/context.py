"""Контекст запроса: идентификатор, который попадает в журнал аудита."""

from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
