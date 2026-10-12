"""Зависимости FastAPI: текущий пользователь и проверка ролей (RBAC)."""

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

from app import db, security
from app.domain import STAFF_ROLES, Role
from app.errors import ApiError

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

_UNAUTHORIZED = {"WWW-Authenticate": "Bearer"}


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    payload = security.decode_token(token, security.ACCESS)
    if payload is None:
        raise ApiError(401, "Токен недействителен или истёк", headers=_UNAUTHORIZED)
    user_id = db.parse_oid(payload.get("sub"))
    user = await db.get_db()[db.USERS].find_one({"_id": user_id}) if user_id else None
    if user is None:
        raise ApiError(401, "Пользователь не найден", headers=_UNAUTHORIZED)
    if not user.get("is_active", False):
        raise ApiError(403, "Учётная запись заблокирована")
    # Роль берётся из БД, а не из токена: смена роли или блокировка действуют сразу.
    return user


def require_roles(*roles: Role):
    async def dependency(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in roles:
            raise ApiError(403, "Недостаточно прав")
        return user

    return dependency


require_staff = require_roles(*STAFF_ROLES)
require_admin = require_roles(Role.admin)
require_student = require_roles(Role.student)


def is_staff(user: dict) -> bool:
    return user["role"] in STAFF_ROLES
