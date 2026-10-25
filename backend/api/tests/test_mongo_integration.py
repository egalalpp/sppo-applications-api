"""Интеграционные тесты на настоящей MongoDB: транзакции, подметальщик очереди, миграции.

Пропускаются, если не задан TEST_MONGO_URI (replica set). Для проверки запасного пути без транзакций
нужен ещё TEST_MONGO_SOLO_URI (одиночный узел). Каждый тест работает в своей временной базе, которая
удаляется после теста. Запуск в docker compose:

    docker run --rm --network sppo_default -e TEST_MONGO_URI="mongodb://root:<пароль>@mongo:27017/?authSource=admin&replicaSet=rs0" \
        -e TEST_MONGO_SOLO_URI="mongodb://solo:27017" sppo-api-test python -m pytest tests/test_mongo_integration.py
"""

import asyncio
import contextlib
import os
import uuid
from datetime import timedelta

import pytest
from bson import ObjectId
from pymongo import AsyncMongoClient
from pymongo.errors import OperationFailure

from app import audit, bootstrap, cache, db
from app.config import get_settings
from app.services import applications as applications_service
from app.services import hashing, ocr_queue, transitions

RS_URI = os.environ.get("TEST_MONGO_URI")
SOLO_URI = os.environ.get("TEST_MONGO_SOLO_URI")

needs_rs = pytest.mark.skipif(not RS_URI, reason="нужен TEST_MONGO_URI (MongoDB как replica set)")
needs_solo = pytest.mark.skipif(not SOLO_URI, reason="нужен TEST_MONGO_SOLO_URI (одиночная MongoDB)")

OPERATOR = {"_id": ObjectId(), "email": "operator@rut.ru", "role": "operator"}
STUDENT = {"_id": ObjectId(), "email": "student@rut.ru", "role": "student"}


@contextlib.asynccontextmanager
async def database(uri: str, *, create_collections: bool = True):
    """Подключает приложение (app.db) к временной базе и удаляет её после теста."""
    name = f"sppo_test_{uuid.uuid4().hex[:8]}"
    old_name = os.environ.get("MONGO_DB")
    os.environ["MONGO_DB"] = name
    get_settings.cache_clear()
    client = AsyncMongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=5000)
    db._client, db._transactions_supported = client, None
    try:
        if create_collections:  # транзакции требуют, чтобы коллекции уже существовали
            for collection in (db.APPLICATIONS, db.AUDIT_LOGS, db.USERS, db.APPLICATION_TYPES):
                await client[name].create_collection(collection)
        yield client[name]
    finally:
        await client.drop_database(name)
        await client.close()
        db._client, db._transactions_supported = None, None
        if old_name is None:
            os.environ.pop("MONGO_DB", None)
        else:
            os.environ["MONGO_DB"] = old_name
        get_settings.cache_clear()


def application(status="in_review", **extra) -> dict:
    now = db.utcnow()
    doc = {
        "schema_version": 5, "applicant_user_id": STUDENT["_id"], "period": "2026-autumn", "basis": "orphan",
        "priority": 1,
        "student": {"full_name": "Иванов Иван Иванович", "birth_date": "2005-03-14", "institute": "ИУЦТ",
                    "group": "ТКИ-211", "course": 3},
        "payout": {"bik": "044525225", "account_number": "40817810700000000001"},
        "form": {"consent_pd_signed": True}, "attachments": [], "checks": [], "status": status,
        "status_history": [], "ocr": ocr_queue.initial_ocr(), "created_at": now, "updated_at": now,
    }
    doc.update(extra)
    return doc


# ------------------------------------------------------------------ транзакции

@needs_rs
def test_transaction_commits_and_rolls_back():
    async def scenario():
        async with database(RS_URI) as mongo:
            assert await db._detect_transactions() is True

            async def commit(session):
                await mongo.applications.insert_one({"name": "committed"}, session=session)

            await db.run_in_transaction(commit)

            async def fail(session):
                await mongo.applications.insert_one({"name": "rolled back"}, session=session)
                raise RuntimeError("boom")

            with pytest.raises(RuntimeError):
                await db.run_in_transaction(fail)
            names = [d["name"] async for d in mongo.applications.find()]
            assert names == ["committed"]

    asyncio.run(scenario())


@needs_rs
def test_transition_writes_status_history_and_audit_together():
    async def scenario():
        async with database(RS_URI) as mongo:
            doc = application("pending")
            doc["_id"] = (await mongo.applications.insert_one(doc)).inserted_id
            updated = await transitions.apply_transition(
                doc, to="in_review", actor=OPERATOR, event_type=audit.CLAIM_REVIEW_STARTED)
            assert updated["status"] == "in_review"
            assert updated["status_history"][-1]["status"] == "in_review"
            events = [e async for e in mongo.audit_logs.find()]
            assert len(events) == 1
            event = events[0]
            assert event["eventType"] == "claim_review_started"
            assert event["actor"] == {"type": "operator", "id": str(OPERATOR["_id"]), "role": "operator",
                                      "email": "operator@rut.ru"}
            assert event["resource"] == {"type": "application", "id": str(doc["_id"])}
            assert event["details"] == {"from": "pending", "to": "in_review"}

    asyncio.run(scenario())


@needs_rs
def test_failed_audit_write_rolls_back_the_status_change(monkeypatch):
    """Событие аудита не записалось — статус остаётся прежним: перехода без события не бывает."""
    async def scenario():
        async with database(RS_URI) as mongo:
            doc = application("in_review")
            doc["_id"] = (await mongo.applications.insert_one(doc)).inserted_id
            real_log = audit.log

            async def log_then_fail(actor, event_type, resource_type, resource_id, details=None, *, session=None):
                await real_log(actor, event_type, resource_type, resource_id, details, session=session)
                raise RuntimeError("audit storage failed")

            monkeypatch.setattr(audit, "log", log_then_fail)
            with pytest.raises(RuntimeError):
                await transitions.apply_transition(
                    doc, to="rejected", actor=OPERATOR, event_type=audit.CLAIM_REJECTED,
                    set_fields={"rejection_reason": "Нет справки"}, comment="Нет справки")
            fresh = await mongo.applications.find_one({"_id": doc["_id"]})
            assert fresh["status"] == "in_review" and fresh["status_history"] == []
            assert await mongo.audit_logs.count_documents({}) == 0

    asyncio.run(scenario())


@needs_rs
def test_two_concurrent_transitions_only_one_wins():
    async def scenario():
        async with database(RS_URI) as mongo:
            doc = application("pending")
            doc["_id"] = (await mongo.applications.insert_one(doc)).inserted_id
            results = await asyncio.gather(*[
                transitions.apply_transition(doc, to="in_review", actor=OPERATOR, event_type=audit.CLAIM_REVIEW_STARTED)
                for _ in range(2)])
            assert sorted(r is None for r in results) == [False, True]
            assert await mongo.audit_logs.count_documents({}) == 1

    asyncio.run(scenario())


@needs_rs
def test_transition_to_unexpected_status_returns_none():
    async def scenario():
        async with database(RS_URI) as mongo:
            doc = application("approved")
            doc["_id"] = (await mongo.applications.insert_one(doc)).inserted_id
            assert await transitions.apply_transition(
                doc, to="rejected", actor=OPERATOR, event_type=audit.CLAIM_REJECTED, expected_from="in_review") is None
            assert await mongo.audit_logs.count_documents({}) == 0

    asyncio.run(scenario())


@needs_solo
def test_standalone_mongo_falls_back_to_sequential_writes(caplog):
    async def scenario():
        async with database(SOLO_URI) as mongo:
            doc = application("pending")
            doc["_id"] = (await mongo.applications.insert_one(doc)).inserted_id
            updated = await transitions.apply_transition(
                doc, to="in_review", actor=OPERATOR, event_type=audit.CLAIM_REVIEW_STARTED)
            assert db._transactions_supported is False
            assert updated["status"] == "in_review"
            assert await mongo.audit_logs.count_documents({}) == 1

    with caplog.at_level("WARNING"):
        asyncio.run(scenario())
    assert "transactions are disabled" in caplog.text


@needs_solo
def test_standalone_mongo_audit_failure_does_not_fail_the_request(monkeypatch, caplog):
    """Без транзакций сбой журнала только логируется: уже выполненный переход не откатывается."""
    async def scenario():
        async with database(SOLO_URI) as mongo:
            doc = application("pending")
            doc["_id"] = (await mongo.applications.insert_one(doc)).inserted_id

            async def broken_insert(*args, **kwargs):
                raise OperationFailure("audit collection is read-only", 121)

            monkeypatch.setattr(type(mongo[db.AUDIT_LOGS]), "insert_one", broken_insert)
            updated = await transitions.apply_transition(
                doc, to="in_review", actor=OPERATOR, event_type=audit.CLAIM_REVIEW_STARTED)
            assert updated["status"] == "in_review"

    with caplog.at_level("ERROR"):
        asyncio.run(scenario())
    assert "was not saved" in caplog.text


@needs_rs
def test_transaction_rejected_by_server_falls_back(monkeypatch):
    """Сервер ответил IllegalOperation (code 20) — пробуем без транзакции и запоминаем это."""
    async def scenario():
        async with database(RS_URI) as mongo:
            async def refuse(*args, **kwargs):
                raise OperationFailure("Transaction numbers are only allowed on a replica set member or mongos", 20)

            monkeypatch.setattr(db._client, "start_session", refuse)

            async def work(session):
                assert session is None
                return "done without a transaction"

            assert await db.run_in_transaction(work) == "done without a transaction"
            assert db._transactions_supported is False

    asyncio.run(scenario())


# ------------------------------------------------------------------ подметальщик очереди

@pytest.fixture
def broker(monkeypatch):
    """Подменяет постановку задачи в Celery и длину очереди: тесты не требуют Redis."""
    state = {"queued": [], "length": 0, "fail": False}

    async def fake_enqueue(app_id):
        if state["fail"]:
            raise ConnectionError("broker is down")
        state["queued"].append(app_id)
        return f"task-{len(state['queued'])}"

    async def fake_length():
        return state["length"]

    monkeypatch.setattr(applications_service, "enqueue_processing", fake_enqueue)
    monkeypatch.setattr(cache, "queue_length", fake_length)
    return state


async def put(mongo, status, ocr_state, age_minutes=0, **extra):
    updated_at = db.utcnow() - timedelta(minutes=age_minutes) if ocr_state != "not_started" else None
    ocr = {**ocr_queue.initial_ocr(), "state": ocr_state, "updated_at": updated_at}
    doc = application(status, ocr=ocr, attachments=[{"attachment_id": "a", "ocr": {"status": "not_started"}}], **extra)
    doc["_id"] = (await mongo.applications.insert_one(doc)).inserted_id
    return doc["_id"]


@needs_rs
def test_sweeper_queues_only_what_needs_it(broker):
    async def scenario():
        async with database(RS_URI) as mongo:
            unsent = await put(mongo, "pending", "not_started")             # не попала в очередь при отправке
            draft = await put(mongo, "draft", "not_started")                # черновик: ждать нечего
            fresh = await put(mongo, "pending", "queued", age_minutes=1)    # только что поставлена
            lost = await put(mongo, "pending", "queued", age_minutes=60)    # очередь потеряна (перезапуск Redis)
            stuck = await put(mongo, "in_review", "running", age_minutes=60)  # воркер упал
            done = await put(mongo, "approved", "not_started")              # решение принято

            assert await ocr_queue.sweep_once() == 3
            assert sorted(broker["queued"]) == sorted([unsent, lost, stuck])
            for app_id in (unsent, lost, stuck):
                doc = await mongo.applications.find_one({"_id": app_id})
                assert doc["ocr"]["state"] == "queued" and doc["ocr"]["task_id"].startswith("task-")
                assert doc["attachments"][0]["ocr"]["status"] == "queued"
            for app_id in (draft, fresh, done):
                assert app_id not in broker["queued"]
            events = [e async for e in mongo.audit_logs.find({"eventType": "ocr_queued"})]
            assert len(events) == 3 and all(e["actor"]["id"] == "sweeper" and e["actor"]["type"] == "system" for e in events)

            assert await ocr_queue.sweep_once() == 0  # повторный проход ничего не дублирует

    asyncio.run(scenario())


@needs_rs
def test_sweeper_respects_queue_capacity(broker):
    async def scenario():
        async with database(RS_URI) as mongo:
            for _ in range(3):
                await put(mongo, "pending", "not_started")
            broker["length"] = get_settings().max_queue_length - 1  # место ровно для одной
            assert await ocr_queue.sweep_once() == 1
            broker["length"] = get_settings().max_queue_length      # очередь полна
            assert await ocr_queue.sweep_once() == 0

    asyncio.run(scenario())


@needs_rs
def test_sweeper_survives_a_broker_outage_and_retries(broker):
    async def scenario():
        async with database(RS_URI) as mongo:
            app_id = await put(mongo, "pending", "not_started")
            broker["fail"] = True
            assert await ocr_queue.sweep_once() == 0  # брокер недоступен: без исключения
            doc = await mongo.applications.find_one({"_id": app_id})
            assert doc["ocr"]["state"] == "not_started" and doc["ocr"]["updated_at"] is None
            assert doc["attachments"][0]["ocr"]["status"] == "not_started"
            broker["fail"] = False
            assert await ocr_queue.sweep_once() == 1  # брокер вернулся: подхватили

    asyncio.run(scenario())


@needs_rs
def test_submit_never_fails_because_of_the_queue(broker):
    """try_enqueue_ocr вызывается после отправки заявки и ничего не бросает, что бы ни случилось с очередью."""
    async def scenario():
        async with database(RS_URI) as mongo:
            app_id = await put(mongo, "pending", "not_started")
            doc = await mongo.applications.find_one({"_id": app_id})

            broker["fail"] = True
            assert await ocr_queue.try_enqueue_ocr(doc) is False   # брокер недоступен
            broker["fail"], broker["length"] = False, get_settings().max_queue_length
            assert await ocr_queue.try_enqueue_ocr(doc) is False   # очередь переполнена
            assert (await mongo.applications.find_one({"_id": app_id}))["status"] == "pending"
            assert broker["queued"] == []

            broker["length"] = 0
            assert await ocr_queue.try_enqueue_ocr(doc) is True
            assert broker["queued"] == [app_id]

    asyncio.run(scenario())


@needs_rs
def test_two_enqueues_of_the_same_application_create_one_task(broker):
    async def scenario():
        async with database(RS_URI) as mongo:
            app_id = await put(mongo, "pending", "not_started")
            doc = await mongo.applications.find_one({"_id": app_id})
            results = await asyncio.gather(ocr_queue.enqueue_ocr(doc, actor=OPERATOR), ocr_queue.enqueue_ocr(doc, actor=OPERATOR))
            assert sorted(r is None for r in results) == [False, True]
            assert broker["queued"] == [app_id]

    asyncio.run(scenario())


# ------------------------------------------------------------------ миграции

LEGACY_APP = {
    "schema_version": 3, "period": "2026-autumn", "basis": "orphan",
    "student": {"full_name": "Иванов Иван Иванович", "full_name_as_written": "Иванова Ивана Ивановича",
                "birth_date": "2005-03-14", "institute": "ИУЦТ", "group": "ТКИ-211", "course": 3,
                "passport": {"series": "4510", "number": "123456"}},
    "payout": {"bik": "044525225", "account_number": "40817810700000000001"},
    "form": {"applicant_signed": True, "signed_date": "2026-10-20", "consent_pd_signed": True},
    "attachments": [], "checks": [], "status": "need_more_docs",
    "status_history": [
        {"status": "pending", "at": None, "by": "x", "by_role": "student", "comment": None},
        {"status": "need_more_docs", "at": None, "by": "x", "by_role": "moderator", "comment": "Приложите справку"}],
    "processing": {"state": "done", "task_id": "t1", "updated_at": None, "error": None},
}


@needs_rs
def test_migration_upgrades_legacy_data_under_the_new_validator():
    """Данные прежней версии мигрируют уже под новым валидатором: priority проставляется первым."""
    async def scenario():
        async with database(RS_URI, create_collections=False) as mongo:
            now = db.utcnow().replace(microsecond=0)  # MongoDB хранит время с точностью до миллисекунды
            history = [{**h, "at": now} for h in LEGACY_APP["status_history"]]
            legacy = {**LEGACY_APP, "applicant_user_id": STUDENT["_id"], "created_at": now, "updated_at": now,
                      "status_history": history}
            await mongo.users.insert_one({"email": "mod@rut.ru", "role": "moderator", "password_hash": "x",
                                          "full_name": "Мод Ератор", "is_active": True, "created_at": now})
            await mongo.application_types.insert_one({"_id": "orphan", "form_no": 1, "title": "Сирота", "requires": [],
                                                      "is_active": True})
            await mongo.applications.insert_one(legacy)
            await mongo.audit_events.insert_many([
                {"at": now, "actor_id": OPERATOR["_id"], "actor_email": "mod@rut.ru", "actor_role": "moderator",
                 "action": "application.transition", "target_type": "application", "target_id": "abc",
                 "details": {"from": "in_review", "to": "rejected", "comment": "Нет справки"}},
                {"at": now, "actor_id": OPERATOR["_id"], "actor_email": "mod@rut.ru", "actor_role": "moderator",
                 "action": "application.check_decision", "target_type": "application", "target_id": "abc",
                 "details": {"code": "inn_checksum"}}])

            await bootstrap.ensure_collections()  # новый валидатор: priority обязателен
            await bootstrap.migrate()
            await bootstrap.migrate()  # идемпотентно

            assert (await mongo.users.find_one({"email": "mod@rut.ru"}))["role"] == "operator"
            assert (await mongo.application_types.find_one({"_id": "orphan"}))["priority"] == 1
            app = await mongo.applications.find_one({})
            assert app["priority"] == 1
            assert app["status"] == "rejected" and app["rejection_reason"] == "Приложите справку"
            assert app["ocr"]["state"] == "ready_for_review" and "processing" not in app
            assert app["submitted_at"] == now
            assert app["form"].get("applicant_signed") is None and app["form"]["consent_pd_at"] == now
            assert "full_name_as_written" not in app["student"]
            assert app["student"]["passport_hash"] == hashing.passport_hash("4510", "123456")
            assert app["payout"]["account_number_hash"] == hashing.account_hash("40817810700000000001")

            logs = [e async for e in mongo.audit_logs.find().sort("eventType", 1)]
            assert [e["eventType"] for e in logs] == ["check_set", "claim_rejected"]
            assert logs[1]["actor"]["type"] == "operator" and logs[1]["details"]["rejection_reason"] == "Нет справки"
            assert await mongo.audit_logs.count_documents({}) == 2  # повторная миграция не дублирует

    asyncio.run(scenario())


@needs_rs
def test_validator_requires_a_reason_for_rejection():
    async def scenario():
        async with database(RS_URI, create_collections=False) as mongo:
            await bootstrap.ensure_collections()
            doc = application("rejected", rejection_reason=None)
            with pytest.raises(OperationFailure) as error:
                await mongo.applications.insert_one(doc)
            assert error.value.code == 121  # Document failed validation
            await mongo.applications.insert_one(application("rejected", rejection_reason="Нет справки"))
            await mongo.applications.insert_one(application("approved"))

    asyncio.run(scenario())


@needs_rs
def test_rehash_after_pepper_change():
    async def scenario():
        async with database(RS_URI) as mongo:
            doc = application("pending")
            doc["student"]["passport"] = {"series": "4510", "number": "123456"}
            hashing.apply_hashes(doc["student"], doc["payout"], pepper="old-pepper-old-pepper")
            await mongo.applications.insert_one(doc)
            assert await bootstrap.backfill_hashes() == 0  # хэши есть: ничего не делаем
            assert await bootstrap.backfill_hashes(force=True) == 1  # пересчёт с текущим HASH_PEPPER
            fresh = await mongo.applications.find_one({})
            assert fresh["student"]["passport_hash"] == hashing.passport_hash("4510", "123456")
            assert fresh["student"]["passport_hash"] != hashing.passport_hash("4510", "123456", "old-pepper-old-pepper")

    asyncio.run(scenario())
