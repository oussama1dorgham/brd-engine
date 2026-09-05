"""Cache module: key stability + the fail-open / disabled invariants.

Pure logic — no real DB. The DB-touching paths are exercised by swapping the
module-level `settings`/`pool` references (settings is a frozen dataclass) so we
prove the cache never raises into a caller and honours CACHE_ENABLED.
"""
import pytest

from backend import cache


# ---- _key: a content hash that is the cache's notion of "sameness" ----

def test_key_is_deterministic():
    assert cache._key(cache.EMBED, ["hi", "voyage-4-lite"]) == \
           cache._key(cache.EMBED, ["hi", "voyage-4-lite"])


def test_key_ignores_dict_order():
    # json.dumps(sort_keys=True) -> mapping order must not change the key
    assert cache._key(cache.RETRIEVE, {"a": 1, "b": 2}) == \
           cache._key(cache.RETRIEVE, {"b": 2, "a": 1})


def test_key_separates_namespaces():
    # identical inputs in different layers must never collide
    assert cache._key(cache.EMBED, ["x"]) != cache._key(cache.RETRIEVE, ["x"])


def test_key_differs_on_different_inputs():
    assert cache._key(cache.EMBED, ["x", "m1"]) != cache._key(cache.EMBED, ["x", "m2"])
    # list order IS significant (ranked chunk ids are ordered)
    assert cache._key(cache.ANSWER, [[1, 2]]) != cache._key(cache.ANSWER, [[2, 1]])


# ---- disabled: CACHE_ENABLED=0 makes every op a cheap no-op ----

class _Off:
    cache_enabled = False
    cache_ttl_seconds = 0


def test_disabled_get_returns_none(monkeypatch):
    monkeypatch.setattr(cache, "settings", _Off())
    assert cache.get(cache.EMBED, ["anything"]) is None


def test_disabled_set_is_noop(monkeypatch):
    monkeypatch.setattr(cache, "settings", _Off())
    # must not touch the DB at all; a raising pool would prove it did
    monkeypatch.setattr(cache, "pool", _boom)
    cache.set(cache.EMBED, ["k"], [1, 2, 3])  # no exception = pool never called


def test_disabled_bust_returns_zero(monkeypatch):
    monkeypatch.setattr(cache, "settings", _Off())
    assert cache.bust_project("es") == 0


# ---- fail-open: a broken DB degrades to a miss, never an exception ----

class _On:
    cache_enabled = True
    cache_ttl_seconds = 10


def _boom(*a, **k):
    raise RuntimeError("db down")


def test_failopen_get_on_db_error(monkeypatch):
    monkeypatch.setattr(cache, "settings", _On())
    monkeypatch.setattr(cache, "pool", _boom)
    assert cache.get(cache.RETRIEVE, ["q", 1, "es"]) is None


def test_failopen_set_never_raises(monkeypatch):
    monkeypatch.setattr(cache, "settings", _On())
    monkeypatch.setattr(cache, "pool", _boom)
    cache.set(cache.RETRIEVE, ["q", 1, "es"], [{"chunk_id": 1}], owner_id=1, project="es")


def test_failopen_bust_returns_zero_on_error(monkeypatch):
    monkeypatch.setattr(cache, "settings", _On())
    monkeypatch.setattr(cache, "pool", _boom)
    assert cache.bust_project("es") == 0


def test_failopen_prune_returns_zero_on_error(monkeypatch):
    monkeypatch.setattr(cache, "settings", _On())
    monkeypatch.setattr(cache, "pool", _boom)
    assert cache.prune_expired() == 0
