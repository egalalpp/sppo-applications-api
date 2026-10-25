"""Сквозная проверка запущенного стека. Нужен только Python 3.10+ (без сторонних библиотек).

    python scripts/smoke_test.py
    python scripts/smoke_test.py --base-url http://localhost:8000 --mailpit-url http://localhost:8025

Сценарий A: студент подаёт заявку со сканами, воркер распознаёт их Tesseract'ом, оператор
разбирает проверки и одобряет; студент получает письма «принята» и «одобрена».
Сценарий B: одно вложение не читается (сбой OCR) — заявка всё равно открывается у оператора,
распознавание можно перезапустить, оператор отклоняет заявку с причиной.
Сценарий C: очередь оператора сортируется по приоритету основания, затем по времени подачи.
Журнал аудита (audit_logs) содержит события студента, оператора и системы.
Всё время проверяется, что студент не видит ничего, связанного с распознаванием.

С флагом --chaos добавляется сценарий D: пока Redis (очередь) остановлен, студент всё равно отправляет
заявку и получает 202, а после запуска Redis подметальщик сам ставит заявку на распознавание.
Для него нужен docker compose в PATH; тест останавливает и снова запускает сервис redis.

Файлы для загрузки — учебные образцы с вымышленными данными из scripts/fixtures
(создаются скриптом make_fixtures.py). Письма читаются из Mailpit; если он недоступен,
проверки почты пропускаются.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Должны совпадать с тем, что нарисовано на образцах (scripts/make_fixtures.py)
BIK = "044525225"
ACCOUNT = "40817810700000000001"
RUN = uuid.uuid4().bytes  # хвост после PNG-данных: у каждого запуска свой sha256, иначе сработает проверка дублей

# Всё, что относится к распознаванию: студенту такие поля показывать нельзя
OCR_KEYS = {"ocr", "checks", "processing", "accuracy", "ocr_state", "ocr_accuracy", "confidence",
            "text_preview", "seal", "signature", "engine", "task_id", "notes",
            # служебные поля, которых у студента тоже быть не должно
            "priority", "passport_hash", "account_number_hash"}


class Api:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def call(self, method, path, *, token=None, json_body=None, form=None, files=None, headers=None, expect=None):
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
        for attempt in range(8):
            request = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
            try:
                with urllib.request.urlopen(request, timeout=30) as resp:
                    status, body, response_headers = resp.status, resp.read(), resp.headers
            except urllib.error.HTTPError as err:
                status, body, response_headers = err.code, err.read(), err.headers
            if status != 429:
                break
            # Лимит nginx на вход и регистрацию — 10 запросов в минуту: ждём, а не падаем
            note(f"nginx ограничивает частоту запросов (429) на {path}, жду 7 с")
            time.sleep(7)
        parsed = json.loads(body) if body and "json" in (response_headers.get("Content-Type") or "") else body
        if expect is not None and status != expect:
            fail(f"{method} {path}: ожидали {expect}, получили {status}: {parsed}")
        return status, parsed, response_headers


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


def note(message: str):
    print(f"[ -- ] {message}")


def read_env() -> dict:
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"')
    return env


def valid_inn() -> str:
    d = [7, 7, 0, 7, 0, 8, 3, 8, 9, 3]
    d.append(sum(a * b for a, b in zip([7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d)) % 11 % 10)
    d.append(sum(a * b for a, b in zip([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d)) % 11 % 10)
    return "".join(map(str, d))


def fixture(name: str, unique: str = "") -> bytes:
    return (FIXTURES / name).read_bytes() + RUN + unique.encode()  # хвост после IEND декодеры игнорируют


def assert_student_blind(value, where: str):
    """В ответе студенту нет ни одного ключа, связанного с распознаванием и проверками."""
    if isinstance(value, dict):
        leaked = OCR_KEYS & set(value)
        if leaked:
            fail(f"{where}: студент видит {sorted(leaked)}")
        for item in value.values():
            assert_student_blind(item, where)
    elif isinstance(value, list):
        for item in value:
            assert_student_blind(item, where)


def login(api: Api, email: str, password: str) -> str:
    _, body, _ = api.call("POST", "/api/v1/auth/login", form={"username": email, "password": password}, expect=200)
    return body["access_token"]


def new_student(api: Api, label: str) -> tuple[str, str]:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    api.call("POST", "/api/v1/auth/register", json_body={
        "email": email, "password": "student-pass-123", "full_name": "Ivanov Ivan"}, expect=201)
    return email, login(api, email, "student-pass-123")


def application_body(basis: str, email: str, **payout) -> dict:
    return {
        "basis": basis,
        "student": {
            "full_name": "Иванов Иван Иванович", "birth_date": "2005-03-14", "institute": "ИУЦТ",
            "group": "ТКИ-211", "course": 3, "email": email, "inn": valid_inn(),
            "passport": {"series": "4510", "number": "123456"},
        },
        "payout": {"bank": "ПАО Сбербанк", "bik": BIK, "account_number": ACCOUNT, **payout},
        "form": {"applicant_signed": True, "consent_pd_signed": True},
    }


def upload(api, token, app_id, doc_type, content, filename, ctype="image/png", expect=201):
    return api.call("POST", f"/api/v1/applications/{app_id}/attachments", token=token, form={"type": doc_type},
                    files={"file": (filename, content, ctype)}, expect=expect)


def wait_ocr(api, token, app_id, states, timeout=180):
    deadline = time.time() + timeout
    while True:
        _, st, _ = api.call("GET", f"/api/v1/applications/{app_id}/status", token=token, expect=200)
        if st["ocr"]["state"] in states:
            return st
        if time.time() > deadline:
            fail(f"распознавание не завершилось за {timeout} с: {st}")
        time.sleep(2)


class Mailbox:
    """Письма из Mailpit (REST API). Если Mailpit недоступен, проверки почты пропускаются."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")
        try:
            urllib.request.urlopen(f"{self.url}/api/v1/info", timeout=3).read()
            self.available = True
        except OSError:
            self.available = False

    def _get(self, path: str):
        with urllib.request.urlopen(self.url + path, timeout=10) as resp:
            return json.loads(resp.read())

    def wait_for(self, to: str, subject_part: str, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            query = urllib.parse.quote(f'to:"{to}"')
            for message in self._get(f"/api/v1/search?query={query}").get("messages", []):
                if subject_part in message["Subject"]:
                    return self._get(f"/api/v1/message/{message['ID']}")
            time.sleep(2)
        fail(f"письмо «{subject_part}» для {to} не пришло за {timeout} с")


def compose(*command):
    docker = shutil.which("docker")
    if not docker:
        fail("--chaos: docker не найден в PATH")
    result = subprocess.run([docker, "compose", *command], cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        fail(f"docker compose {' '.join(command)}: {result.stderr.strip()}")


def chaos_scenario(api, admin, operator):
    """Очередь (Redis) «спит»: студент не должен этого заметить, а заявка не должна потеряться."""
    print("\n--- Сценарий D: Redis остановлен ---")
    email, student = new_student(api, "student-chaos")
    _, app, _ = api.call("POST", "/api/v1/applications", token=student, json_body=application_body("dormitory", email),
                         expect=201)
    app_id = app["id"]
    for doc_type, name in (("passport_registration", "passport.png"), ("union_card", "certificate.png"),
                           ("bank_requisites_screenshot", "bank.png")):
        upload(api, student, app_id, doc_type, fixture(name, "chaos" + doc_type), name)

    compose("stop", "redis")
    try:
        note("Redis остановлен: очередь недоступна")
        started = time.time()
        status, submitted, _ = api.call("POST", f"/api/v1/applications/{app_id}/submit", token=student)
        elapsed = time.time() - started
        if status != 202 or submitted != {"id": app_id, "status": "pending"}:
            fail(f"отправка при остановленной очереди: {status} {submitted}")
        _, view, _ = api.call("GET", f"/api/v1/applications/{app_id}/status", token=student, expect=200)
        if view["status"] != "pending":
            fail(f"студент видит {view}")
        assert_student_blind(view, "статус при остановленной очереди")
        ok(f"студент отправил заявку при остановленной очереди за {elapsed:.1f} с: 202 и статус pending, без 503")
        _, op_view, _ = api.call("GET", f"/api/v1/applications/{app_id}", token=operator, expect=200)
        if op_view["ocr"]["state"] != "not_started":
            fail(f"ожидали ocr.state=not_started, получили {op_view['ocr']}")
        ok("оператор видит заявку: распознавание ещё не поставлено в очередь (ocr.state=not_started)")
    finally:
        compose("start", "redis")
    note("Redis запущен: жду, пока подметальщик поставит заявку на распознавание")

    st = wait_ocr(api, operator, app_id, {"ready_for_review", "ocr_failed"}, timeout=240)
    if st["ocr"]["state"] != "ready_for_review":
        fail(f"после возвращения Redis: {st['ocr']}")
    _, found, _ = api.call("GET", f"/api/v1/admin/audit-logs?resource_id={app_id}&event_type=ocr_queued", token=admin,
                           expect=200)
    queued_by = {e["actor"]["id"] for e in found["items"]}
    if "sweeper" not in queued_by:
        fail(f"заявку поставил не подметальщик: {queued_by}")
    ok("подметальщик поставил заявку в очередь сам, воркер её распознал: ocr.state=ready_for_review")


def main():
    parser = argparse.ArgumentParser()
    env = read_env()
    parser.add_argument("--base-url", default=f"http://localhost:{env.get('HTTP_PORT', '8000')}")
    parser.add_argument("--mailpit-url", default="http://localhost:8025")
    parser.add_argument("--chaos", action="store_true",
                        help="сценарий D: остановить Redis на время отправки заявки (нужен docker compose)")
    args = parser.parse_args()
    api, mailbox = Api(args.base_url), Mailbox(args.mailpit_url)

    # ---------- Инфраструктура ----------
    instances = {api.call("GET", "/health", expect=200)[2].get("X-Instance") for _ in range(6)}
    ok(f"nginx -> реплики api: {sorted(instances)}")
    _, ready, _ = api.call("GET", "/ready", expect=200)
    ok(f"/ready: {ready['checks']}")
    api.call("GET", "/api/v1/benefit-types", expect=200)
    _, types, h = api.call("GET", "/api/v1/benefit-types", expect=200)
    ok(f"справочник: {len(types)} оснований, X-Cache={h.get('X-Cache')}")
    status, _, _ = api.call("GET", "/api/v1/benefit-types", headers={"If-None-Match": h.get("ETag")})
    if status != 304:
        fail(f"ETag: ожидали 304, получили {status}")
    ok("ETag/If-None-Match -> 304 Not Modified")
    if not mailbox.available:
        note("Mailpit недоступен: проверки писем пропущены (SMTP_HOST/Brevo — настоящая почта)")

    # ---------- Администратор и оператор ----------
    demo = env.get("SEED_DEMO_USERS", "false").lower() == "true"
    admin_email, admin_password = ("admin@rut.ru", "admin123") if demo else (env.get("ADMIN_EMAIL"), env.get("ADMIN_PASSWORD"))
    if not admin_email or not admin_password:
        fail("нет учётки администратора: SEED_DEMO_USERS=true или ADMIN_EMAIL/ADMIN_PASSWORD в .env")
    admin = login(api, admin_email, admin_password)
    ok(f"вошёл администратор {admin_email}")
    if demo:
        login(api, "student@rut.ru", "student123")
        login(api, "operator@rut.ru", "operator123")
        ok("демо-учётки student@rut.ru и operator@rut.ru работают")
    op_email = f"operator-{uuid.uuid4().hex[:8]}@example.com"
    _, op_user, _ = api.call("POST", "/api/v1/admin/users", token=admin, json_body={
        "email": op_email, "password": "operator-pass-123", "full_name": "Петрова Мария", "role": "operator"}, expect=201)
    if op_user["role"] != "operator":
        fail(f"роль {op_user['role']}")
    api.call("POST", "/api/v1/admin/users", token=admin, json_body={
        "email": f"old-{uuid.uuid4().hex[:6]}@example.com", "password": "moderator-pass-1", "full_name": "Старая Роль",
        "role": "moderator"}, expect=422)
    operator = login(api, op_email, "operator-pass-123")
    ok(f"админ создал оператора {op_email}; роли moderator больше нет")

    # ======================= Сценарий A =======================
    print("\n--- Сценарий A: заявка проходит распознавание и одобряется ---")
    email, student = new_student(api, "student-a")
    for path in ("/api/v1/operator/queue", "/api/v1/admin/users"):
        status, _, _ = api.call("GET", path, token=student)
        if status != 403:
            fail(f"студент получил доступ к {path}: {status}")
    ok("студент -> /operator/queue и /admin/users: 403")

    body = application_body("orphan", email)
    key = {"Idempotency-Key": uuid.uuid4().hex}
    _, app, _ = api.call("POST", "/api/v1/applications", token=student, json_body=body, headers=key, expect=201)
    _, again, _ = api.call("POST", "/api/v1/applications", token=student, json_body=body, headers=key, expect=200)
    if again["id"] != app["id"]:
        fail("Idempotency-Key не сработал")
    app_id = app["id"]
    assert_student_blind(app, "создание заявки")
    ok(f"заявка создана: {app_id}")

    status, err, _ = api.call("POST", f"/api/v1/applications/{app_id}/submit", token=student)
    if status != 422 or "missing_slots" not in err:
        fail(f"отправка без документов: ожидали 422 со списком, получили {status}: {err}")
    ok(f"без обязательных документов отправить нельзя: не хватает {err['missing_slots']}")

    status, _, _ = api.call("POST", "/api/v1/applications", token=student, json_body=body)
    if status != 409:
        fail(f"вторая заявка по тому же основанию: ожидали 409, получили {status}")
    ok("вторая заявка по тому же основанию в периоде: 409")

    docs = [
        ("passport_registration", "passport.png"), ("union_card", "certificate.png"),
        ("bank_requisites_screenshot", "bank.png"), ("birth_certificate_student", "certificate.png"),
        ("death_certificate", "certificate.png"),
    ]
    for doc_type, name in docs:
        upload(api, student, app_id, doc_type, fixture(name, doc_type), name)
    upload(api, student, app_id, "other", b"MZ\x90\x00 exe", "x.png", expect=415)
    ok("загружено 5 вложений; файл с чужой сигнатурой отклонён: 415")

    _, attachments, _ = api.call("GET", f"/api/v1/applications/{app_id}/attachments", token=student, expect=200)
    assert_student_blind(attachments, "список вложений")
    _, link, _ = api.call("GET", f"/api/v1/applications/{app_id}/attachments/{attachments[0]['attachment_id']}"
                                 "/download-url", token=student, expect=200)
    with urllib.request.urlopen(link["url"], timeout=10) as resp:
        if resp.status != 200:
            fail("presigned URL не открылся")
    ok("presigned URL на скачивание работает")

    status, submitted, _ = api.call("POST", f"/api/v1/applications/{app_id}/submit", token=student)
    if status != 202 or set(submitted) != {"id", "status"} or submitted["status"] != "pending":
        fail(f"отправка: {status} {submitted}")
    ok("отправлено: ответ студенту — только id и status")

    st = wait_ocr(api, operator, app_id, {"ready_for_review", "ocr_failed"})
    if st["ocr"]["state"] != "ready_for_review":
        fail(f"распознавание завершилось с ошибкой: {st}")
    ok(f"воркер распознал заявку Tesseract'ом: ocr.state={st['ocr']['state']}, точность {st['ocr']['accuracy']}%")

    # --- студент ничего не знает про распознавание ---
    _, s_app, _ = api.call("GET", f"/api/v1/applications/{app_id}", token=student, expect=200)
    _, s_status, _ = api.call("GET", f"/api/v1/applications/{app_id}/status", token=student, expect=200)
    _, s_list, _ = api.call("GET", "/api/v1/applications", token=student, expect=200)
    _, s_hist, _ = api.call("GET", f"/api/v1/applications/{app_id}/history", token=student, expect=200)
    for name, payload in (("заявка", s_app), ("статус", s_status), ("список", s_list), ("история", s_hist)):
        assert_student_blind(payload, name)
    for method, path in (("GET", f"/api/v1/applications/{app_id}/checks"),
                         ("POST", f"/api/v1/applications/{app_id}/ocr/restart"),
                         ("GET", f"/api/v1/tasks/{st['ocr'].get('task_id') or 'x'}")):
        status, _, _ = api.call(method, path, token=student)
        if status != 403:
            fail(f"студенту доступно {method} {path}: {status}")
    ok("студент не видит OCR, проверки и задачи: ответы без этих полей, /checks, /ocr/restart, /tasks — 403")
    if any("by" in h for h in s_hist):
        fail("студент видит id сотрудников в истории")

    if mailbox.available:
        letter = mailbox.wait_for(email, "принята")
        for word in ("ocr", "распознав", "tesseract"):
            if word in letter["Text"].lower():
                fail(f"в письме студенту есть «{word}»")
        ok(f"письмо «{letter['Subject']}» пришло в Mailpit")

    # --- оператор ---
    _, queue, _ = api.call("GET", "/api/v1/operator/queue?ocr_state=ready_for_review&limit=100", token=operator, expect=200)
    mine = next((i for i in queue["items"] if i["id"] == app_id), None)
    if not mine or mine["ocr_accuracy"] is None:
        fail(f"заявки нет в очереди оператора с ocr_state и точностью: {mine}")
    ok(f"очередь оператора: ocr_state={mine['ocr_state']}, ocr_accuracy={mine['ocr_accuracy']}%")
    _, op_app, _ = api.call("GET", f"/api/v1/applications/{app_id}", token=operator, expect=200)
    by_code = {c["code"]: c for c in op_app["checks"]}
    expected = ("fio_form_vs_passport", "passport_number_vs_scan", "bank_requisites_vs_scan", "bank_owner_confirmed",
                "inn_checksum", "bank_account_checksum")
    bad = {c: by_code[c]["status"] for c in expected if by_code[c]["status"] != "auto_ok"}
    if bad:
        fail(f"автопроверки по сканам не сошлись: {bad}")
    ok("автопроверки по сканам: ФИО, серия/номер паспорта, БИК и счёт, владелец счёта — auto_ok")
    passport_ocr = next(a["ocr"] for a in op_app["attachments"] if a["type"] == "passport_registration")
    cert_ocr = next(a["ocr"] for a in op_app["attachments"] if a["type"] == "union_card")
    if passport_ocr["engine"] != "tesseract" or not passport_ocr.get("confidence"):
        fail(f"паспорт: {passport_ocr}")
    if "есть печать" not in cert_ocr["notes"] or "есть подпись" not in cert_ocr["notes"]:
        fail(f"справка: ожидали «есть печать» и «есть подпись», получили {cert_ocr['notes']}")
    ok(f"оператор видит результат по вложениям: паспорт {passport_ocr['confidence']}%, справка — {cert_ocr['notes']}")

    api.call("POST", f"/api/v1/applications/{app_id}/transitions", token=operator, json_body={"to": "in_review"}, expect=200)
    status, body409, _ = api.call("POST", f"/api/v1/applications/{app_id}/transitions", token=operator,
                                  json_body={"to": "approved"})
    if status != 409:
        fail(f"одобрение с нерешёнными проверками: ожидали 409, получили {status}")
    ok(f"одобрить нельзя, пока есть нерешённые проверки: {body409['blocking_checks']}")
    status, _, _ = api.call("POST", f"/api/v1/applications/{app_id}/transitions", token=operator,
                            json_body={"to": "rejected"})
    if status != 422:
        fail(f"отказ без причины: ожидали 422, получили {status}")
    ok("отказ без rejection_reason: 422")
    for code in body409["blocking_checks"]:
        api.call("PATCH", f"/api/v1/applications/{app_id}/checks/{code}", token=operator,
                 json_body={"status": "manual_ok", "comment": "проверено вручную"}, expect=200)
    _, final, _ = api.call("POST", f"/api/v1/applications/{app_id}/transitions", token=operator,
                           json_body={"to": "approved", "comment": "внутренняя заметка"}, expect=200)
    ok(f"оператор закрыл проверки и одобрил заявку: {final['status']}")

    _, st, _ = api.call("GET", f"/api/v1/applications/{app_id}/status", token=student, expect=200)
    if st["status"] != "approved":
        fail(f"студент видит статус {st['status']}")
    _, hist, _ = api.call("GET", f"/api/v1/applications/{app_id}/history", token=student, expect=200)
    if any("внутренняя заметка" in json.dumps(h, ensure_ascii=False) for h in hist):
        fail("студент видит внутреннюю заметку оператора")
    ok("история для студента: " + " -> ".join(h["status"] for h in hist))
    if mailbox.available:
        ok(f"письмо «{mailbox.wait_for(email, 'одобрена')['Subject']}» пришло в Mailpit")

    # ======================= Сценарий B =======================
    print("\n--- Сценарий B: сбой распознавания не мешает оператору, отказ с причиной ---")
    email_b, student_b = new_student(api, "student-b")
    _, app_b, _ = api.call("POST", "/api/v1/applications", token=student_b, json_body=application_body("dormitory", email_b),
                           expect=201)
    b_id = app_b["id"]
    broken = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64 + RUN  # сигнатура верная, картинка битая
    upload(api, student_b, b_id, "passport_registration", broken, "passport.png")
    upload(api, student_b, b_id, "union_card", fixture("certificate.png", "b-union"), "union.png")
    upload(api, student_b, b_id, "bank_requisites_screenshot", fixture("bank.png", "b-bank"), "bank.png")
    api.call("POST", f"/api/v1/applications/{b_id}/submit", token=student_b, expect=202)
    ok("студент отправил заявку с нечитаемым сканом паспорта: для него это обычная отправка")

    st = wait_ocr(api, operator, b_id, {"ready_for_review", "ocr_failed"})
    if st["ocr"]["state"] != "ocr_failed":
        fail(f"ожидали ocr_failed, получили {st['ocr']}")
    _, op_b, _ = api.call("GET", f"/api/v1/applications/{b_id}", token=operator, expect=200)
    failed = next(a for a in op_b["attachments"] if a["type"] == "passport_registration")["ocr"]
    codes = {c["code"]: c["status"] for c in op_b["checks"]}
    if failed["status"] != "failed" or codes["inn_checksum"] != "auto_ok" or codes["fio_form_vs_passport"] != "needs_review":
        fail(f"после сбоя: {failed}, {codes}")
    ok(f"ocr_failed: заявка открывается, проверки без OCR посчитаны, остальные ждут оператора ({failed['error']})")
    _, s_status, _ = api.call("GET", f"/api/v1/applications/{b_id}/status", token=student_b, expect=200)
    assert_student_blind(s_status, "статус после сбоя OCR")
    if s_status["status"] != "pending":
        fail(f"студент видит {s_status}")
    ok("студент по-прежнему видит просто «на рассмотрении»")

    _, restart, _ = api.call("POST", f"/api/v1/applications/{b_id}/ocr/restart", token=operator, expect=202)
    if restart["ocr"]["state"] not in ("queued", "running"):  # воркер мог подхватить задачу ещё до ответа
        fail(f"перезапуск OCR: {restart}")
    wait_ocr(api, operator, b_id, {"ocr_failed"})
    ok("«Перезапустить OCR»: задача поставлена в очередь и отработала (скан по-прежнему битый — снова ocr_failed)")

    api.call("POST", f"/api/v1/applications/{b_id}/transitions", token=operator, json_body={"to": "in_review"}, expect=200)
    reason = "Скан паспорта не читается, приложите чёткое фото"
    api.call("POST", f"/api/v1/applications/{b_id}/transitions", token=operator,
             json_body={"to": "rejected", "rejection_reason": reason}, expect=200)
    _, st, _ = api.call("GET", f"/api/v1/applications/{b_id}/status", token=student_b, expect=200)
    if st["status"] != "rejected" or st.get("rejection_reason") != reason:
        fail(f"студент не видит причину отказа: {st}")
    ok("оператор отклонил заявку: студент видит статус и причину отказа")
    if mailbox.available:
        letter = mailbox.wait_for(email_b, "отклонена")
        if reason not in letter["Text"]:
            fail("в письме об отказе нет причины")
        ok("письмо «отклонена» с причиной пришло в Mailpit")
    api.call("POST", "/api/v1/applications", token=student_b, json_body=application_body("dormitory", email_b), expect=201)
    ok("после отказа студент может подать новую заявку по тому же основанию")

    # ======================= Сценарий C =======================
    print("\n--- Сценарий C: очередь оператора по приоритету основания ---")
    # dormitory (приоритет 12) подаём первой и ещё раз последней, disabled (приоритет 2) — посередине
    queued = []
    for label, basis, extra_doc in (("c-dorm-1", "dormitory", None), ("c-disabled", "disabled", "disability_certificate"),
                                    ("c-dorm-2", "dormitory", None)):
        email_c, token_c = new_student(api, label)
        _, app_c, _ = api.call("POST", "/api/v1/applications", token=token_c,
                               json_body=application_body(basis, email_c), expect=201)
        for doc_type, name in (("passport_registration", "passport.png"), ("union_card", "certificate.png"),
                               ("bank_requisites_screenshot", "bank.png")) + (((extra_doc, "certificate.png"),) if extra_doc else ()):
            upload(api, token_c, app_c["id"], doc_type, fixture(name, label + doc_type), name)
        api.call("POST", f"/api/v1/applications/{app_c['id']}/submit", token=token_c, expect=202)
        queued.append((app_c["id"], basis, token_c))
        time.sleep(1.1)  # submitted_at различается хотя бы на секунду
    _, queue, _ = api.call("GET", "/api/v1/operator/queue?limit=100", token=operator, expect=200)
    order = [i["id"] for i in queue["items"] if i["id"] in {q[0] for q in queued}]
    expected_order = [queued[1][0], queued[0][0], queued[2][0]]  # disabled(2), затем dormitory в порядке подачи
    if order != expected_order:
        fail(f"порядок очереди {order}, ожидали {expected_order}")
    items = {i["id"]: i for i in queue["items"]}
    if [items[i]["priority"] for i in order] != [2, 12, 12]:
        fail(f"приоритеты в очереди: {[items[i]['priority'] for i in order]}")
    ok("очередь: сначала disabled (приоритет 2), затем dormitory (12) в порядке подачи; оператор видит priority")
    for app_c, _, token_c in queued:
        _, view, _ = api.call("GET", f"/api/v1/applications/{app_c}", token=token_c, expect=200)
        assert_student_blind(view, "заявка студента (приоритет и хэши скрыты)")
    ok("студент не видит priority и хэши паспорта и счёта")

    # ---------- Журнал аудита ----------
    def events_for(resource_id):
        _, found, _ = api.call("GET", f"/api/v1/admin/audit-logs?resource_id={resource_id}&limit=100", token=admin, expect=200)
        return list(reversed(found["items"]))  # от старых к новым

    log_a = events_for(app_id)
    types_a = [e["eventType"] for e in log_a]
    expected = ["claim_created", "attachment_uploaded", "claim_submitted", "ocr_queued", "ocr_done",
                "claim_review_started", "check_set", "claim_approved"]
    remaining = iter(types_a)
    if not all(event in remaining for event in expected):  # проверка «подпоследовательности»: события идут в этом порядке
        fail(f"журнал заявки A: {types_a}")
    by_type = {e["eventType"]: e for e in log_a}
    if by_type["claim_submitted"]["actor"]["type"] != "student" or by_type["ocr_done"]["actor"] != {
            "type": "system", "id": "ocr-worker", "role": "system", "email": None}:
        fail(f"актёры в журнале: {by_type['claim_submitted']['actor']}, {by_type['ocr_done']['actor']}")
    approved = by_type["claim_approved"]
    if approved["actor"]["type"] != "operator" or approved["details"].get("to") != "approved" or not approved.get("request_id"):
        fail(f"claim_approved: {approved}")
    ok(f"журнал аудита заявки A ({len(log_a)} событий): " + ", ".join(dict.fromkeys(types_a)))

    log_b = events_for(b_id)
    types_b = [e["eventType"] for e in log_b]
    rejected = next((e for e in log_b if e["eventType"] == "claim_rejected"), None)
    if "ocr_failed" not in types_b or "ocr_restarted" not in types_b or not rejected:
        fail(f"журнал заявки B: {types_b}")
    if rejected["details"]["rejection_reason"] != reason or rejected["details"]["from"] != "in_review":
        fail(f"claim_rejected: {rejected['details']}")
    ok(f"журнал аудита заявки B: ocr_failed, ocr_restarted и claim_rejected с причиной отказа ({len(log_b)} событий)")
    _, stats, _ = api.call("GET", "/api/v1/admin/stats", token=admin, expect=200)
    ok(f"статистика: {stats['applications_by_status']}, роли {stats['users_by_role']}, очередь {stats['queue_length']}")

    if args.chaos:
        chaos_scenario(api, admin, operator)

    print("\nВсё работает.")


if __name__ == "__main__":
    main()
