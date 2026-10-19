import asyncio
from types import SimpleNamespace

from bson import ObjectId

from app import db, security
from app.domain import Role
from app.seed import DEMO_USERS, seed_demo_users


class FakeCollection:
    def __init__(self):
        self.docs = []

    async def find_one(self, query, projection=None):
        return next((d for d in self.docs if all(d.get(k) == v for k, v in query.items())), None)

    async def insert_one(self, doc):
        doc["_id"] = ObjectId()
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])


class FakeDb:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection())


def test_demo_accounts_match_the_spec():
    accounts = {u["email"]: (u["password"], u["role"]) for u in DEMO_USERS}
    assert accounts == {
        "student@rut.ru": ("student123", Role.student),
        "operator@rut.ru": ("operator123", Role.operator),
        "admin@rut.ru": ("admin123", Role.admin),
    }


def test_seed_creates_accounts_once(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(db, "get_db", lambda: fake)

    assert asyncio.run(seed_demo_users()) == 3
    assert asyncio.run(seed_demo_users()) == 0  # повторный запуск ничего не дублирует

    users = {u["email"]: u for u in fake[db.USERS].docs}
    assert {e: u["role"] for e, u in users.items()} == {
        "student@rut.ru": "student", "operator@rut.ru": "operator", "admin@rut.ru": "admin"}
    assert all(u["is_active"] for u in users.values())
    # пароли хранятся хешем и подходят
    assert "student123" not in users["student@rut.ru"]["password_hash"]
    assert security.verify_password("operator123", users["operator@rut.ru"]["password_hash"])
    assert not security.verify_password("wrong", users["operator@rut.ru"]["password_hash"])


def test_seed_does_not_overwrite_existing_user(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(db, "get_db", lambda: fake)
    fake[db.USERS].docs.append({"_id": ObjectId(), "email": "admin@rut.ru", "password_hash": "custom", "role": "admin"})

    assert asyncio.run(seed_demo_users()) == 2
    assert fake[db.USERS].docs[0]["password_hash"] == "custom"
