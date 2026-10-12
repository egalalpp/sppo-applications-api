"""Проверки API, которым не нужны MongoDB и Redis: маршруты, авторизация, формат ошибок."""

from fastapi.testclient import TestClient

from app.main import app

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
        "/api/v1/moderation/queue",
        "/api/v1/applications/{app_id}/transitions",
        "/api/v1/admin/users",
        "/api/v1/admin/stats",
    ):
        assert path in paths, path


def test_health_has_instance_header():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["X-Instance"]


def test_protected_route_requires_token():
    response = client.get("/api/v1/applications")
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["status"] == 401


def test_invalid_token_rejected():
    response = client.get("/api/v1/admin/users", headers={"Authorization": "Bearer garbage"})
    assert response.status_code == 401


def test_validation_error_does_not_echo_password():
    response = client.post("/api/v1/auth/register",
                           json={"email": "not-an-email", "password": "s3cret-value", "full_name": "Тест"})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert "s3cret-value" not in response.text
