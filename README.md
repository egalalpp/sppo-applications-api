# СППО РУТ (МИИТ): API заявок на материальную поддержку

Прототип REST API для приёма и проверки заявлений студентов на материальную поддержку:
FastAPI, MongoDB, Redis, Celery, S3 (SeaweedFS) и nginx в Docker Compose.

Код, запуск и документация — в [`backend/`](backend/README.md).

```bash
cd backend
cp .env.example .env
docker compose up -d --build
```

Документация API: http://localhost:8080/redoc
