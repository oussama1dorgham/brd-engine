"""`openrouter/free` hardening: model choice, reply checks, fallbacks, error messages — all
with fake provider responses (no network)."""
from types import SimpleNamespace

import pytest

from backend.chat.condense import usable_rewrite
from backend.llm import free_router
from backend.llm.providers import openai_compat
from backend.llm.providers.base import ProviderError

OR = "https://openrouter.ai/api/v1"
_REAL_FETCH = free_router._fetch_catalog          # the fixture below replaces it for other tests


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    free_router._reset_for_tests()
    monkeypatch.setattr(free_router, "_fetch_catalog",
                        lambda base: ["poolside/laguna-s-2.1:free", "x/other:free",
                                      "nvidia/nemotron-3-super-120b-a12b:free"])
    yield
    free_router._reset_for_tests()


def test_safety_verdicts_are_recognised_and_answers_are_not():
    for v in ("User Safety: safe", "user safety: unsafe", "Response Safety: safe", "safe", " unsafe ",
              "unsafe\nS1, S10"):
        assert free_router.is_verdict(v), v
    for a in ("Safe mode lets officers preview the schedule [1].", "The roles are Consultant and Officer [1].",
              "content consultant involvement in user stories"):
        assert not free_router.is_verdict(a), a


def test_catalogue_excludes_classifiers_and_non_text_models(monkeypatch):
    data = {"data": [
        {"id": "nvidia/nemotron-3.5-content-safety:free", "context_length": 999999},
        {"id": "meta/llama-guard-4:free", "context_length": 99999},
        {"id": "x/image-gen:free", "context_length": 9},
        {"id": "a/b-chat:free", "context_length": 5000},
        {"id": "a/b-vision:free", "context_length": 9000, "architecture": {"output_modalities": ["image"]}},
        {"id": "a/paid-model", "context_length": 1},
        {"id": "nvidia/nemotron-3-super-120b-a12b:free", "context_length": 100}]}
    monkeypatch.setattr(free_router.httpx, "get", lambda url, timeout: SimpleNamespace(json=lambda: data))
    assert _REAL_FETCH(OR) == ["a/b-chat:free", "nvidia/nemotron-3-super-120b-a12b:free"]


def test_candidates_put_preferred_first_and_skip_cooling_models():
    c = free_router.candidates(OR)
    assert c[:2] == ["nvidia/nemotron-3-super-120b-a12b:free", "poolside/laguna-s-2.1:free"]
    free_router.cool("nvidia/nemotron-3-super-120b-a12b:free", 60)
    assert free_router.candidates(OR)[0] == "poolside/laguna-s-2.1:free"


def test_cooldowns_match_the_failure():
    assert free_router.cooldown_for(403, "only available on agentic harnesses") == 24 * 3600
    assert free_router.cooldown_for(400, "Reasoning is mandatory for this endpoint") == 24 * 3600
    assert free_router.cooldown_for(429, "temporarily rate-limited upstream") == 120
    assert free_router.cooldown_for(504, "Upstream idle timeout exceeded") == 120


def _adapter(monkeypatch, behaviour):
    """behaviour: model -> reply text, or a ProviderError to raise."""
    a = openai_compat.OpenAICompatAdapter()
    calls: list[str] = []

    def complete_one(api_key, base_url, model, messages, temperature, max_tokens):
        calls.append(model)
        r = behaviour[model]
        if isinstance(r, Exception):
            raise r
        return r

    def stream_one(api_key, base_url, model, messages, temperature, max_tokens):
        calls.append(model)
        r = behaviour[model]
        if isinstance(r, Exception):
            raise r
        for i in range(0, len(r), 5):
            yield r[i:i + 5]

    monkeypatch.setattr(a, "_complete_one", complete_one)
    monkeypatch.setattr(a, "_stream_one", stream_one)
    return a, calls


BAD_THEN_GOOD = {
    "nvidia/nemotron-3-super-120b-a12b:free": ProviderError("rejected", 400, raw="Reasoning is mandatory for this endpoint"),
    "poolside/laguna-s-2.1:free": "User Safety: safe",
    "x/other:free": "The roles are Consultant, Chief of Staff and Scheduling Officer [1].",
}


def test_router_complete_skips_errors_and_verdicts(monkeypatch):
    a, calls = _adapter(monkeypatch, BAD_THEN_GOOD)
    out = a.complete("k", OR, free_router.FREE_ROUTER, [], temperature=0, max_tokens=100)
    assert out.startswith("The roles are") and len(calls) == 3
    assert free_router.candidates(OR) == ["x/other:free"]          # the two failures are cooling down


def test_router_stream_never_shows_a_verdict(monkeypatch):
    a, _ = _adapter(monkeypatch, BAD_THEN_GOOD)
    shown = "".join(a.stream("k", OR, free_router.FREE_ROUTER, [], temperature=0, max_tokens=100))
    assert shown == BAD_THEN_GOOD["x/other:free"] and "Safety" not in shown


def test_daily_account_limit_is_reported_at_once(monkeypatch):
    lim = ProviderError("rate", 429, raw="Rate limit exceeded: free-models-per-day. Add 10 credits")
    a, calls = _adapter(monkeypatch, {m: lim for m in BAD_THEN_GOOD})
    with pytest.raises(ProviderError, match="free-model requests"):
        a.complete("k", OR, free_router.FREE_ROUTER, [], temperature=0, max_tokens=100)
    assert len(calls) == 1                                          # no pointless second model


def test_all_models_failing_gives_a_clear_error(monkeypatch):
    a, _ = _adapter(monkeypatch, {m: "" for m in BAD_THEN_GOOD})
    with pytest.raises(ProviderError, match="No free model could answer"):
        list(a.stream("k", OR, free_router.FREE_ROUTER, [], temperature=0, max_tokens=100))


def test_a_chosen_model_answering_with_a_verdict_is_an_error_not_an_answer(monkeypatch):
    model = "nvidia/nemotron-3.5-content-safety:free"
    a, _ = _adapter(monkeypatch, {model: "User Safety: safe"})
    with pytest.raises(ProviderError, match="content-safety verdict"):
        a.complete("k", OR, model, [], temperature=0, max_tokens=100)
    with pytest.raises(ProviderError, match="content-safety verdict"):
        list(a.stream("k", OR, model, [], temperature=0, max_tokens=100))


def test_a_model_with_mandatory_reasoning_is_retried_with_reasoning_on(monkeypatch):
    a = openai_compat.OpenAICompatAdapter()
    sent: list[dict] = []

    class Err(Exception):
        body = {"message": "Reasoning is mandatory for this endpoint and cannot be disabled."}
        status_code = 400

    def create(**kw):
        sent.append(kw)
        if kw["extra_body"]["reasoning"] == {"enabled": False}:
            raise Err()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok answer"))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(a, "_client", lambda k, b: client)
    assert a.complete("k", OR, "liquid/lfm-2.5-2.6b:free", [], temperature=0, max_tokens=80) == "ok answer"
    assert sent[1]["extra_body"]["reasoning"] == {"effort": "low", "exclude": True}
    assert sent[1]["max_tokens"] >= 2000


def test_a_rewrite_is_used_only_when_it_is_a_query_about_the_conversation():
    q = "how is the content consultant involved in all user stories"
    assert usable_rewrite("content consultant involvement in all user stories of the BRD", q)
    for bad in ("User Safety: safe", "", "We need to rewrite the latest message into a query",
                "The user is asking about consultants", "x" * 600, "weather forecast tomorrow"):
        assert not usable_rewrite(bad, q), bad


def test_error_messages_name_the_real_cause():
    from backend.llm.client import describe_error

    class E(Exception):
        def __init__(self, status, body):
            super().__init__(str(body))
            self.status_code, self.body = status, body
    assert "coding agents" in describe_error(E(403, {"message": "only available on agentic harnesses"}))
    assert "free-model requests" in describe_error(E(429, {"message": "free-models-per-day"}))
    assert "reasoning turned off" in describe_error(E(400, {"message": "Reasoning is mandatory"}))
    assert "key was rejected" not in describe_error(E(403, {"message": "forbidden"}))


# --- review findings ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,raw", [(401, "User not found"), (402, "Insufficient credits"),
                                        (429, "Rate limit exceeded: free-models-per-min"), (403, "forbidden")])
def test_key_and_account_errors_are_reported_as_is_and_cool_nothing(monkeypatch, status, raw):
    err = ProviderError("the key / account message", status, raw=raw)
    a, calls = _adapter(monkeypatch, {m: err for m in BAD_THEN_GOOD})
    with pytest.raises(ProviderError, match="the key / account message"):
        a.complete("k", OR, free_router.FREE_ROUTER, [], temperature=0, max_tokens=100)
    assert len(calls) == 1
    assert free_router.candidates(OR)[0] == "nvidia/nemotron-3-super-120b-a12b:free"   # nobody cooled


def test_router_exhaustion_is_not_retried_by_the_engine(monkeypatch):
    from backend.llm import engine
    from backend.llm.providers import registry
    busy = ProviderError("busy", 429, raw="temporarily rate-limited upstream")
    a, calls = _adapter(monkeypatch, {m: busy for m in BAD_THEN_GOOD})
    monkeypatch.setattr(registry, "get", lambda p: a)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    with pytest.raises(ProviderError, match="No free model could answer"):
        engine.complete_chat([], provider="openai", api_key="k", base_url=OR, model=free_router.FREE_ROUTER)
    assert len(calls) == 3                                   # one pass over the pool, not 3 × 3


def test_the_router_and_catalogue_only_trust_openrouter_itself(monkeypatch):
    assert free_router.is_router(free_router.FREE_ROUTER, "https://openrouter.ai/api/v1")
    for fake in ("https://openrouter.ai.attacker.tld/v1", "https://evil.example/openrouter.ai/v1", None):
        assert not free_router.is_router(free_router.FREE_ROUTER, fake), fake
    got: list[str] = []
    monkeypatch.setattr(free_router.httpx, "get",
                        lambda url, timeout: got.append(url) or SimpleNamespace(json=lambda: {"data": []}))
    _REAL_FETCH("https://anything.example/v1")
    assert got == [free_router.CATALOG_URL]


def test_catalogue_failure_or_empty_falls_back_to_the_preferred_list(monkeypatch):
    def boom(base):
        raise RuntimeError("catalogue down")
    monkeypatch.setattr(free_router, "_fetch_catalog", boom)
    free_router._reset_for_tests()
    assert free_router.candidates(OR) == list(free_router.PREFERRED)
    monkeypatch.setattr(free_router, "_fetch_catalog", lambda base: [])
    free_router._reset_for_tests()
    assert free_router.candidates(OR) == list(free_router.PREFERRED)


def test_safety_domain_answers_are_not_verdicts():
    for a in ("Safety: safe operating limits are defined in FR-3 [1].", "Safety: unsafe conditions trigger an alarm [2]",
              "Safe.", "Unsafe"):
        assert not free_router.is_verdict(a), a
    assert free_router.is_verdict("User Safety: safe\nResponse Safety: safe")


def test_other_providers_stream_straight_through_and_keep_partial_answers(monkeypatch):
    a = openai_compat.OpenAICompatAdapter()

    def stream_one(api_key, base_url, model, messages, temperature, max_tokens):
        yield "Hel"
        yield "lo"
        raise ProviderError("dropped", 500)

    monkeypatch.setattr(a, "_stream_one", stream_one)
    got: list[str] = []
    with pytest.raises(ProviderError):
        for piece in a.stream("k", "https://api.openai.com/v1", "gpt-x", [], temperature=0, max_tokens=10):
            got.append(piece)
    assert got == ["Hel", "lo"]                              # token by token, as before
    got = []
    with pytest.raises(ProviderError):
        for piece in a.stream("k", OR, "some/model", [], temperature=0, max_tokens=10):
            got.append(piece)
    assert "".join(got) == "Hello"                           # held back, but not lost on error


def test_good_rewrites_are_kept():
    assert usable_rewrite("The user roles and permissions in the payments BRD", "what about the user roles?")
    assert usable_rewrite("Payment refund approval permissions", "ماذا عن صلاحيات الموافقة على الاسترداد؟")
    assert not usable_rewrite("what does this have", "what does this cover")   # filler words only


def test_server_quota_message_names_the_server_not_the_user(monkeypatch):
    from backend.llm import engine
    e = ProviderError("x", 429, raw="Rate limit exceeded: free-models-per-day")
    assert "server's shared free-model quota" in str(engine._explain_fallback(e, None))
    assert engine._explain_fallback(e, "user-key") is e
