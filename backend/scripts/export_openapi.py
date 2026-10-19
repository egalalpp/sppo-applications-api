"""Выгружает OpenAPI-схему приложения в docs/openapi.yaml.

Запуск (нужны зависимости из api/requirements-dev.txt):  python scripts/export_openapi.py
Альтернатива — скачать http://localhost:8000/openapi.json из запущенного стека.
"""

import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

# Для генерации схемы подключения к БД не нужны — подставляем заглушки.
for name, value in {
    "MONGO_APP_USER": "x", "MONGO_APP_PASSWORD": "x", "REDIS_PASSWORD": "x",
    "JWT_SECRET": "x" * 32, "S3_ACCESS_KEY": "x", "S3_SECRET_KEY": "x",
}.items():
    os.environ.setdefault(name, value)

from app.main import app  # noqa: E402

schema = app.openapi()
schema["servers"] = [{"url": "http://localhost:8000", "description": "docker compose (nginx)"}]

out = ROOT / "docs" / "openapi.yaml"
out.write_text(yaml.safe_dump(schema, allow_unicode=True, sort_keys=False), encoding="utf-8")
print(f"Written {out} ({len(schema['paths'])} paths)")
