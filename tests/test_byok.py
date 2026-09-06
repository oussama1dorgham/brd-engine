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


# --- llm_keys.validate_and_list (routes through the provider engine) + masking ---

def test_validate_and_list_returns_models(monkeypatch):
    monkeypatch.setattr(lk.engine, "list_models", lambda provider, api_key, base_url: ["a", "b"])
    assert lk.validate_and_list("openai", "https://u", "k") == ["a", "b"]


def test_validate_and_list_empty_raises_invalidkey(monkeypatch):
    monkeypatch.setattr(lk.engine, "list_models", lambda provider, api_key, base_url: [])
    with pytest.raises(lk.InvalidKey):
        lk.validate_and_list("openai", "https://u", "k")


def test_validate_and_list_provider_error_raises_invalidkey(monkeypatch):
    from backend.providers.base import ProviderError

    def boom(provider, api_key, base_url):
        raise ProviderError("Your API key was rejected.", 401)

    monkeypatch.setattr(lk.engine, "list_models", boom)
    with pytest.raises(lk.InvalidKey):
        lk.validate_and_list("anthropic", None, "bad")


# --- provider adapter layer (framework) ---

def test_registry_resolves_and_lists_providers():
    from backend.providers import registry
    assert registry.is_known("anthropic") and registry.is_known("gemini") and registry.is_known("cohere")
    assert not registry.is_known("nope")
    assert registry.get("nope").key == registry.DEFAULT_PROVIDER   # unknown → default
    assert registry.get("anthropic").key == "anthropic"
    keys = {p["key"] for p in registry.providers_meta()}
    assert {"openai", "anthropic", "gemini", "cohere"} <= keys


def test_split_system_lifts_system_prompt():
    from backend.providers.base import split_system
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"}]
    system, rest = split_system(msgs)
    assert system == "S" and [m["role"] for m in rest] == ["user", "assistant"]


def test_anthropic_payload_lifts_system_and_maps_messages():
    from backend.providers.anthropic import AnthropicAdapter
    body = AnthropicAdapter()._payload(
        "claude-x",
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        temperature=0.0, max_tokens=42, stream=True)
    assert body["system"] == "sys" and body["max_tokens"] == 42 and body["stream"] is True
    assert body["messages"] == [{"role": "user", "content": "hi"}]   # system not in messages


def test_engine_resolve_falls_back_to_system(monkeypatch):
    from backend.generate import engine
    class _Cfg:
        gen_model = "sys-model"
        def require_openrouter(self): return "SYSKEY"
        def require_base_url(self): return "https://sys"
        def need(self, v, n): return v
    monkeypatch.setattr(engine, "settings", _Cfg())
    assert engine._resolve(None, None, None) == ("openai", "SYSKEY", "https://sys")
    assert engine._resolve("anthropic", "K", None) == ("anthropic", "K", None)


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
    monkeypatch.setattr(lk, "active_key_id", lambda uid: 5)
    monkeypatch.setattr(lk, "list_models_for", lambda uid, kid, force=False: ["a", "b", "c"])

    # duplicates, whitespace, and an id the key can't serve ("x")
    out = lk.set_preferred(7, ["b", "b", "x", "a", " a ", "c"])
    assert out == ["b", "a", "c"]                 # deduped, order kept, "x" dropped
    assert sink["params"][0].obj == ["b", "a", "c"]  # stored as jsonb
    assert sink["params"][1] == 5                     # per-key: the active key id
    assert sink["params"][2] == 7                     # user id
    assert sink.get("committed")


def test_set_preferred_keeps_all_when_availability_unknown(monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(lk, "pool", lambda: _FakePool(sink))
    monkeypatch.setattr(lk, "active_key_id", lambda uid: 3)
    monkeypatch.setattr(lk, "list_models_for", lambda uid, kid, force=False: [])  # provider unreachable

    out = lk.set_preferred(1, ["m2", "m1", "m2"])
    assert out == ["m2", "m1"]                    # deduped, but nothing dropped when unknown
