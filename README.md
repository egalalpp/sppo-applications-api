# СППО РУТ (МИИТ): API заявок на материальную поддержку

REST API для приёма и проверки заявлений студентов на материальную поддержку: FastAPI, MongoDB, Redis, Celery,
распознавание сканов Tesseract, почтовые уведомления, S3 (SeaweedFS / Cloudflare R2) и nginx в Docker Compose.

Код, запуск и документация — в [`backend/`](backend/README.md). Деплой на бесплатные тарифы
(MongoDB Atlas, Cloudflare R2, Render, Brevo) — [`backend/docs/DEPLOY.md`](backend/docs/DEPLOY.md),
Blueprint для Render — [`render.yaml`](render.yaml).

```bash
cd backend
cp .env.example .env
docker compose up -d --build
```

API: http://localhost:8000 · документация: http://localhost:8000/redoc · письма (Mailpit): http://localhost:8025
