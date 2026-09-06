"""Bring-your-own-key LLM routing: per-key client caching, chat routing, key store.

Pure logic — no network. The OpenAI client is faked.
"""
import httpx
import pytest
from openai import RateLimitError

import backend.generate.llm as llm
import backend.llm_keys as lk


def _rate_error(msg: str) -> RateLimitError:
    resp = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    return RateLimitError(msg, response=resp, body=None)


class _FakeOpenAI:
    def __init__(self, api_key=None, base_url=None, **k):
        self.api_key = api_key
        self.base_url = base_url


def test_byok_client_cached_per_key(monkeypatch):
    monkeypatch.setattr(llm, "OpenAI", _FakeOpenAI)
    llm._byok_clients.clear()
    a = llm.client("k1", "https://u1")
    b = llm.client("k1", "https://u1")
    c = llm.client("k2", "https://u2")
    assert a is b, "same key/url must reuse the same client"
    assert a is not c, "different key/url must be a different client"
    assert a.api_key == "k1" and a.base_url == "https://u1"


def test_chat_routes_through_byok(monkeypatch):
    seen = {}

    class _Completions:
        def create(self, **kw):
            seen.update(kw)
            return object()

    class _Client:
        chat = type("C", (), {"completions": _Completions()})()

    def fake_client(api_key=None, base_url=None):
        seen["api_key"], seen["base_url"] = api_key, base_url
        return _Client()

    monkeypatch.setattr(llm, "client", fake_client)
    llm.chat([{"role": "user", "content": "hi"}], model="m1", api_key="K", base_url="U")
    assert seen["api_key"] == "K" and seen["base_url"] == "U" and seen["model"] == "m1"


# --- llm_keys.validate_and_list + masking ---

class _Model:
    def __init__(self, i): self.id = i


def test_validate_and_list_returns_sorted_unique(monkeypatch):
    class _Models:
        def list(self): return type("R", (), {"data": [_Model("b"), _Model("a"), _Model("a")]})()

    class _OK:
        def __init__(self, **k): self.models = _Models()

    monkeypatch.setattr(lk, "OpenAI", _OK)
    assert lk.validate_and_list("https://u", "k") == ["a", "b"]


def test_validate_and_list_raises_invalidkey(monkeypatch):
    class _Boom:
        def __init__(self, **k): pass
        class models:  # noqa: N801
            @staticmethod
            def list(): raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(lk, "OpenAI", _Boom)
    with pytest.raises(lk.InvalidKey):
        lk.validate_and_list("https://u", "bad")


def test_mask():
    assert lk._mask("sk-abcd1234efgh") == "sk-…efgh"
    assert lk._mask("short") == "…rt"


# --- llm.chat rate-limit handling (fail fast on hard limits) ---

def test_is_hard_limit_detects_daily():
    assert llm._is_hard_limit(_rate_error("Rate limit exceeded: free-models-per-day. Add credits"))
    assert llm._is_hard_limit(_rate_error("daily quota reached"))
    assert not llm._is_hard_limit(_rate_error("Too many requests, please slow down"))


def _fake_client_raising(err: RateLimitError, counter: dict | None = None):
    class _C:
        class chat:
            class completions:
                @staticmethod
                def create(**k):
                    if counter is not None:
                        counter["n"] = counter.get("n", 0) + 1
                    raise err
    return _C


def test_chat_fails_fast_on_hard_limit(monkeypatch):
    slept: list = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(llm, "client", lambda *a, **k: _fake_client_raising(_rate_error("free-models-per-day exceeded")))
    with pytest.raises(RateLimitError):
        llm.chat([{"role": "user", "content": "hi"}], model="m")
    assert slept == [], "a hard/daily limit must not sleep-retry (that caused the multi-minute spin)"


def test_chat_retries_briefly_on_transient(monkeypatch):
    slept: list = []
    calls: dict = {}
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(llm, "client", lambda *a, **k: _fake_client_raising(_rate_error("temporarily rate limited"), calls))
    with pytest.raises(RateLimitError):
        llm.chat([{"role": "user", "content": "hi"}], model="m", max_retries=3)
    assert calls["n"] == 3                       # tried, then gave up
    assert slept and all(s <= 12 for s in slept)  # short, bounded backoff — never minutes


# --- llm_keys.set_preferred (dedup / order / intersect) — DB faked ---

class _FakeCur:
    def __init__(self, sink): self.sink = sink
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.sink["sql"] = sql; self.sink["params"] = params


class _FakeConn:
    def __init__(self, sink): self.sink = sink
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return _FakeCur(self.sink)
    def commit(self): self.sink["committed"] = True


class _FakePool:
    def __init__(self, sink): self.sink = sink
    def connection(self): return _FakeConn(self.sink)


def test_set_preferred_dedups_and_intersects(monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(lk, "pool", lambda: _FakePool(sink))
    monkeypatch.setattr(lk, "list_models", lambda uid, force=False: ["a", "b", "c"])

    # duplicates, whitespace, and an id the key can't serve ("x")
    out = lk.set_preferred(7, ["b", "b", "x", "a", " a ", "c"])
    assert out == ["b", "a", "c"]                 # deduped, order kept, "x" dropped
    assert sink["params"][0].obj == ["b", "a", "c"]  # stored as jsonb
    assert sink["params"][1] == 7
    assert sink.get("committed")


def test_set_preferred_keeps_all_when_availability_unknown(monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(lk, "pool", lambda: _FakePool(sink))
    monkeypatch.setattr(lk, "list_models", lambda uid, force=False: [])  # provider unreachable

    out = lk.set_preferred(1, ["m2", "m1", "m2"])
    assert out == ["m2", "m1"]                    # deduped, but nothing dropped when unknown
