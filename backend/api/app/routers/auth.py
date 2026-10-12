from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm

from app import db, security
from app.deps import get_current_user
from app.domain import Role
from app.errors import ApiError, responses
from app.schemas import RefreshIn, RegisterIn, TokenPair, UserOut
from app.serializers import user_out
from app.services.users import create_user

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_tokens(user: dict) -> dict:
    user_id, role = str(user["_id"]), user["role"]
    access, expires_in = security.create_token(user_id, role, security.ACCESS)
    refresh, _ = security.create_token(user_id, role, security.REFRESH)
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "expires_in": expires_in}


@router.post("/register", response_model=UserOut, status_code=201, responses=responses(409, 422),
             summary="Регистрация студента")
async def register(body: RegisterIn):
    # Роль при самостоятельной регистрации всегда student: выбрать её нельзя.
    user = await create_user(body.email, body.password, body.full_name, Role.student)
    return user_out(user)


@router.post("/login", response_model=TokenPair, responses=responses(401, 403),
             summary="Вход: email в поле username, пароль в password (OAuth2 form)")
async def login(form: OAuth2PasswordRequestForm = Depends()):
    user = await db.get_db()[db.USERS].find_one({"email": form.username.strip().lower()})
    if not security.verify_password(form.password, user["password_hash"] if user else None):
        raise ApiError(401, "Неверный email или пароль", headers={"WWW-Authenticate": "Bearer"})
    if not user["is_active"]:
        raise ApiError(403, "Учётная запись заблокирована")
    return _issue_tokens(user)


@router.post("/refresh", response_model=TokenPair, responses=responses(401, 403),
             summary="Обновить пару токенов по refresh-токену")
async def refresh(body: RefreshIn):
    payload = security.decode_token(body.refresh_token, security.REFRESH)
    user_id = db.parse_oid(payload.get("sub")) if payload else None
    user = await db.get_db()[db.USERS].find_one({"_id": user_id}) if user_id else None
    if user is None:
        raise ApiError(401, "Refresh-токен недействителен или истёк")
    if not user["is_active"]:
        raise ApiError(403, "Учётная запись заблокирована")
    return _issue_tokens(user)


@router.get("/me", response_model=UserOut, responses=responses(401), summary="Текущий пользователь")
async def me(user: dict = Depends(get_current_user)):
    return user_out(user)
