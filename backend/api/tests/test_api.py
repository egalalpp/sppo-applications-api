"""Проверки API, которым не нужны MongoDB и Redis: маршруты, авторизация, формат ошибок, схемы."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas import StudentIn, TransitionIn

client = TestClient(app)


def test_openapi_has_main_routes():
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/v1/auth/login",
        "/api/v1/benefit-types",
        "/api/v1/applications",
        "/api/v1/applications/{app_id}/submit",
        "/api/v1/applications/{app_id}/status",
        "/api/v1/applications/{app_id}/attachments",
        "/api/v1/operator/queue",
        "/api/v1/applications/{app_id}/transitions",
        "/api/v1/applications/{app_id}/ocr/restart",
        "/api/v1/admin/users",
        "/api/v1/admin/stats",
    ):
        assert path in paths, path
    assert "/api/v1/moderation/queue" not in paths  # модератор переименован в оператора


def test_health_has_instance_header():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["X-Instance"]


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/applications"),
    ("GET", "/api/v1/operator/queue"),
    ("POST", "/api/v1/applications/6acd52dda70b810daca703cf/ocr/restart"),
    ("GET", "/api/v1/applications/6acd52dda70b810daca703cf/checks"),
    ("GET", "/api/v1/tasks/abc"),
])
def test_protected_routes_require_token(method, path):
    response = client.request(method, path)
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"


def test_invalid_token_rejected():
    response = client.get("/api/v1/admin/users", headers={"Authorization": "Bearer garbage"})
    assert response.status_code == 401


def test_validation_error_does_not_echo_password():
    response = client.post("/api/v1/auth/register",
                           json={"email": "not-an-email", "password": "s3cret-value", "full_name": "Тест"})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert "s3cret-value" not in response.text


def test_rejection_requires_a_reason():
    with pytest.raises(ValidationError, match="rejection_reason"):
        TransitionIn(to="rejected")
    with pytest.raises(ValidationError):
        TransitionIn(to="rejected", rejection_reason="  ")
    assert TransitionIn(to="rejected", rejection_reason="Справка просрочена").rejection_reason
    assert TransitionIn(to="approved").rejection_reason is None
    with pytest.raises(ValidationError, match="только при"):
        TransitionIn(to="approved", rejection_reason="лишнее")


def test_need_more_docs_status_is_gone():
    with pytest.raises(ValidationError):
        TransitionIn(to="need_more_docs", rejection_reason="x")


def student(**over):
    data = dict(full_name="Иванов Иван Иванович", birth_date="2005-03-14", institute="ИУЦТ", group="ТКИ-211",
                course=3, email="student@rut.ru")
    return {**data, **over}


def test_student_email_is_required_for_notifications():
    assert StudentIn(**student()).email == "student@rut.ru"
    data = student()
    del data["email"]
    with pytest.raises(ValidationError, match="email"):
        StudentIn(**data)


def test_genitive_form_of_name_is_optional():
    assert StudentIn(**student()).full_name_as_written is None
    assert StudentIn(**student(full_name_as_written="Иванова Ивана Ивановича")).full_name_as_written
