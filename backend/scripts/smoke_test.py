"""Сквозная проверка запущенного стека: полный путь заявки от студента до одобрения.

Нужен только Python 3.10+ (без сторонних библиотек). Запуск после `docker compose up -d --build`:
    python scripts/smoke_test.py
    python scripts/smoke_test.py --base-url http://localhost:8080

Учётка администратора берётся из .env (ADMIN_EMAIL / ADMIN_PASSWORD).
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Минимальные файлы: тип определяется сервером по сигнатуре. Хвост уникален для каждого
# запуска, иначе повторные прогоны поймают проверку дублей (одинаковый sha256).
RUN = uuid.uuid4().bytes
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64 + RUN
PDF = b"%PDF-1.4\n%smoke\n" + RUN


class Api:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def call(self, method, path, *, token=None, json_body=None, form=None, files=None, headers=None, expect=None):
        url = self.base + path
        hdrs = dict(headers or {})
        data = None
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        if json_body is not None:
            data = json.dumps(json_body).encode()
            hdrs["Content-Type"] = "application/json"
        elif form is not None and files is None:
            data = urllib.parse.urlencode(form).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        elif files is not None:
            data, content_type = _multipart(form or {}, files)
            hdrs["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, body, rh = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as err:
            status, body, rh = err.code, err.read(), err.headers
        parsed = json.loads(body) if body and "json" in (rh.get("Content-Type") or "") else body
        if expect is not None and status != expect:
            fail(f"{method} {path}: ожидали {expect}, получили {status}: {parsed}")
        return status, parsed, rh


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, (filename, content, ctype) in files.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n".encode() + content + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def fail(message: str):
    print(f"\n[FAIL] {message}")
    sys.exit(1)


def ok(message: str):
    print(f"[ OK ] {message}")


def read_env() -> dict:
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


def valid_inn() -> str:
    d = [7, 7, 0, 7, 0, 8, 3, 8, 9, 3]
    d.append(sum(a * b for a, b in zip([7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d)) % 11 % 10)
    d.append(sum(a * b for a, b in zip([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d)) % 11 % 10)
    return "".join(map(str, d))


def valid_account(bik: str) -> str:
    base = "4081781000" + f"{uuid.uuid4().int % 10**9:09d}"  # 19 цифр, 9-я позиция — ключ
    for key in "0123456789":
        acc = base[:8] + key + base[8:]
        digits = bik[-3:] + acc
        if sum(int(c) * w % 10 for c, w in zip(digits, [7, 1, 3] * 8)) % 10 == 0:
            return acc
    raise RuntimeError("unreachable")


def login(api: Api, email: str, password: str) -> str:
    _, body, _ = api.call("POST", "/api/v1/auth/login", form={"username": email, "password": password}, expect=200)
    return body["access_token"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080")
    args = parser.parse_args()
    api = Api(args.base_url)
    env = read_env()

    # 1. Балансировка
    instances = {api.call("GET", "/health", expect=200)[2].get("X-Instance") for _ in range(6)}
    ok(f"nginx -> реплики api: {sorted(instances)}")
    _, ready, _ = api.call("GET", "/ready", expect=200)
    ok(f"/ready: {ready['checks']}")

    # 2. Кэш справочника
    api.call("GET", "/api/v1/benefit-types", expect=200)
    _, types, h = api.call("GET", "/api/v1/benefit-types", expect=200)
    ok(f"справочник: {len(types)} оснований, X-Cache={h.get('X-Cache')}")
    status, _, _ = api.call("GET", "/api/v1/benefit-types", headers={"If-None-Match": h.get("ETag")})
    if status != 304:
        fail(f"ETag: ожидали 304, получили {status}")
    ok("ETag/If-None-Match -> 304 Not Modified")

    # 3. Студент
    email = f"student-{uuid.uuid4().hex[:8]}@example.com"
    password = "student-pass-123"
    api.call("POST", "/api/v1/auth/register", json_body={"email": email, "password": password,
                                                         "full_name": "Иванов Иван Иванович"}, expect=201)
    student = login(api, email, password)
    ok(f"студент зарегистрирован и вошёл: {email}")

    status, _, _ = api.call("GET", "/api/v1/admin/users", token=student)
    if status != 403:
        fail(f"студент получил доступ к админке: {status}")
    ok("студент -> /admin/users: 403")

    # 4. Заявка (с Idempotency-Key)
    bik = "044525225"
    application = {
        "basis": "orphan",
        "student": {
            "full_name": "Иванов Иван Иванович",
            "full_name_as_written": "Иванова Ивана Ивановича",
            "birth_date": "2005-03-14", "institute": "ИУЦТ", "group": "ТКИ-211", "course": 3,
            "inn": valid_inn(), "passport": {"series": "4510", "number": "123456"},
        },
        "payout": {"bank": "ПАО Сбербанк", "bik": bik, "account_number": valid_account(bik)},
        "form": {"applicant_signed": True, "consent_pd_signed": True},
    }
    key = {"Idempotency-Key": uuid.uuid4().hex}
    _, app, _ = api.call("POST", "/api/v1/applications", token=student, json_body=application, headers=key, expect=201)
    _, again, _ = api.call("POST", "/api/v1/applications", token=student, json_body=application, headers=key, expect=200)
    if again["id"] != app["id"]:
        fail("Idempotency-Key не сработал")
    app_id = app["id"]
    ok(f"заявка создана: {app_id} (повтор с тем же Idempotency-Key вернул её же)")

    # 5. Вложения
    for doc_type in ("passport_registration", "union_card", "bank_requisites_screenshot",
                     "birth_certificate_student", "death_certificate"):
        content = PDF + doc_type.encode() if doc_type == "death_certificate" else PNG + doc_type.encode()
        name, ctype = ("doc.pdf", "application/pdf") if content.startswith(b"%PDF") else ("doc.png", "image/png")
        api.call("POST", f"/api/v1/applications/{app_id}/attachments", token=student,
                 form={"type": doc_type}, files={"file": (name, content, ctype)}, expect=201)
    ok("загружено 5 вложений")
    status, _, _ = api.call("POST", f"/api/v1/applications/{app_id}/attachments", token=student,
                            form={"type": "other"}, files={"file": ("x.exe", b"MZ\x90\x00", "image/png")})
    if status != 415:
        fail(f"exe под видом png: ожидали 415, получили {status}")
    ok("файл с чужой сигнатурой отклонён: 415")

    _, att_list, _ = api.call("GET", f"/api/v1/applications/{app_id}/attachments", token=student, expect=200)
    _, link, _ = api.call("GET", f"/api/v1/applications/{app_id}/attachments/{att_list[0]['attachment_id']}"
                                 "/download-url", token=student, expect=200)
    with urllib.request.urlopen(link["url"], timeout=10) as resp:
        if resp.status != 200:
            fail("presigned URL не открылся")
    ok("presigned URL на скачивание работает")

    # 6. Отправка и ожидание Celery
    _, submitted, _ = api.call("POST", f"/api/v1/applications/{app_id}/submit", token=student, expect=202)
    ok(f"отправлено, task_id={submitted['task_id']}")
    deadline = time.time() + 120
    while True:
        _, st, h = api.call("GET", f"/api/v1/applications/{app_id}/status", token=student, expect=200)
        if st["processing"]["state"] == "done":
            break
        if st["processing"]["state"] == "failed" or time.time() > deadline:
            fail(f"обработка не завершилась: {st}")
        time.sleep(2)
    ok("воркер Celery обработал заявку")
    _, checks, _ = api.call("GET", f"/api/v1/applications/{app_id}/checks", token=student, expect=200)
    for c in checks:
        print(f"         {c['code']:<28} {c['status']:<13} {c.get('score') or ''}")

    # 7. Админ создаёт модератора
    admin_email, admin_password = env.get("ADMIN_EMAIL"), env.get("ADMIN_PASSWORD")
    if not admin_email or not admin_password:
        fail("в .env нет ADMIN_EMAIL / ADMIN_PASSWORD")
    admin = login(api, admin_email, admin_password)
    mod_email = f"moderator-{uuid.uuid4().hex[:8]}@example.com"
    api.call("POST", "/api/v1/admin/users", token=admin, json_body={
        "email": mod_email, "password": "moderator-pass-123", "full_name": "Петров Пётр", "role": "moderator",
    }, expect=201)
    moderator = login(api, mod_email, "moderator-pass-123")
    ok(f"админ создал модератора {mod_email}")

    # 8. Модерация
    _, queue, _ = api.call("GET", "/api/v1/moderation/queue?limit=100", token=moderator, expect=200)
    ok(f"в очереди модератора заявок: {queue['total']}")
    api.call("POST", f"/api/v1/applications/{app_id}/transitions", token=moderator,
             json_body={"to": "in_review"}, expect=200)
    status, body, _ = api.call("POST", f"/api/v1/applications/{app_id}/transitions", token=moderator,
                               json_body={"to": "approved"})
    if status != 409:
        fail(f"одобрение с нерешёнными проверками: ожидали 409, получили {status}")
    ok(f"одобрить нельзя, пока есть нерешённые проверки: {body.get('blocking_checks')}")
    for code in body["blocking_checks"]:
        api.call("PATCH", f"/api/v1/applications/{app_id}/checks/{code}", token=moderator,
                 json_body={"status": "manual_ok", "comment": "проверено вручную"}, expect=200)
    _, final, _ = api.call("POST", f"/api/v1/applications/{app_id}/transitions", token=moderator,
                           json_body={"to": "approved", "comment": "Одобрено"}, expect=200)
    ok(f"модератор закрыл проверки и одобрил заявку: {final['status']}")

    # 9. Студент видит результат, админ — журнал
    _, st, _ = api.call("GET", f"/api/v1/applications/{app_id}/status", token=student, expect=200)
    if st["status"] != "approved":
        fail(f"студент видит статус {st['status']}")
    _, hist, _ = api.call("GET", f"/api/v1/applications/{app_id}/history", token=student, expect=200)
    ok("история: " + " -> ".join(h["status"] for h in hist))
    _, events, _ = api.call("GET", f"/api/v1/admin/audit-events?target_id={app_id}", token=admin, expect=200)
    ok(f"в журнале аудита по заявке событий: {events['total']}")
    _, stats, _ = api.call("GET", "/api/v1/admin/stats", token=admin, expect=200)
    ok(f"статистика: {stats['applications_by_status']}, очередь {stats['queue_length']}/{stats['max_queue_length']}")

    print("\nВсё работает.")


if __name__ == "__main__":
    main()
