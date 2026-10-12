"""Справочник оснований (категорий льгот) — публичное чтение с кэшированием.

Три уровня кэша:
1. Redis на сервере (заголовок X-Cache: HIT/MISS) — не ходим в MongoDB;
2. ETag + If-None-Match — клиент получает 304 без тела;
3. Cache-Control — браузер может вообще не делать запрос 5 минут.
"""

import hashlib
import json

from fastapi import APIRouter, Request, Response

from app.errors import ApiError, responses
from app.schemas import BenefitTypeOut
from app.services import benefit_types

router = APIRouter(prefix="/benefit-types", tags=["benefit-types"])

CACHE_CONTROL = "public, max-age=300"


def _cached_response(request: Request, response: Response, payload, cache_hit: bool):
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL, "X-Cache": "HIT" if cache_hit else "MISS"}
    # nginx при gzip превращает ETag в слабый (W/"..."), поэтому префикс отбрасываем.
    client_etags = {t.strip().removeprefix("W/") for t in request.headers.get("if-none-match", "").split(",")}
    if etag in client_etags:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return payload


@router.get("", response_model=list[BenefitTypeOut], summary="Список оснований и обязательных документов")
async def list_benefit_types(request: Request, response: Response, include_inactive: bool = False):
    items, hit = await benefit_types.list_all()
    if not include_inactive:
        items = [t for t in items if t["is_active"]]
    return _cached_response(request, response, items, hit)


@router.get("/{code}", response_model=BenefitTypeOut, responses=responses(404), summary="Одно основание")
async def get_benefit_type(code: str, request: Request, response: Response):
    items, hit = await benefit_types.list_all()
    item = next((t for t in items if t["code"] == code), None)
    if item is None:
        raise ApiError(404, "Основание не найдено")
    return _cached_response(request, response, item, hit)
