"""Кэш — ускорение, а не источник правды: недоступный Redis не должен ломать запросы и тормозить их."""

import asyncio

import pytest

from app import cache


class DeadRedis:
    def __init__(self):
        self.calls = 0

    async def get(self, key):
        self.calls += 1
        raise ConnectionError("redis is down")

    set = delete = llen = get


@pytest.fixture
def dead(monkeypatch):
    redis = DeadRedis()
    monkeypatch.setattr(cache, "_cache", redis)
    monkeypatch.setattr(cache, "_broker", redis)
    return redis


def test_unavailable_redis_means_cache_miss_not_an_error(dead):
    async def scenario():
        assert await cache.get_json("k") is None
        await cache.set_json("k", {"a": 1}, 5)  # не бросает
        await cache.delete("k")                  # не бросает

    asyncio.run(scenario())


def test_breaker_skips_redis_after_the_first_failure(dead):
    """Одна неудача — и в течение DOWN_COOLDOWN_SEC остальные обращения не ждут таймаутов."""
    async def scenario():
        await cache.get_json("a")
        assert dead.calls == 1 and cache.is_down()
        for _ in range(5):
            await cache.get_json("b")
            await cache.set_json("b", 1, 5)
            await cache.delete("b")
        assert dead.calls == 1
        assert cache.queue_available() is False
        with pytest.raises(ConnectionError):
            await cache.queue_length()
        assert dead.calls == 1

    asyncio.run(scenario())


def test_breaker_retries_after_the_cooldown(dead, monkeypatch):
    async def scenario():
        await cache.get_json("a")
        assert dead.calls == 1
        monkeypatch.setattr(cache.time, "monotonic", lambda: cache._down_until + 1)  # пауза прошла
        assert cache.is_down() is False
        await cache.get_json("a")
        assert dead.calls == 2

    asyncio.run(scenario())


def test_queue_available_without_a_redis_broker(monkeypatch):
    monkeypatch.setattr(cache, "_broker", None)  # например RabbitMQ: недоступность кэша очередь не затрагивает
    cache.mark_down("test", ConnectionError())
    assert cache.queue_available() is True
