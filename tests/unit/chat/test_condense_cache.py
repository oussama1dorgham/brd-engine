"""Cache keys ignore trivial wording differences; the follow-up rewrite is cached safely."""
import pytest

from backend.chat import condense as C
from backend.core.cache import norm_text
from backend.llm.providers.base import ProviderError

HIST = [{"role": "user", "content": "all the users in the brd"},
        {"role": "assistant", "content": "The roles are Consultant and Chief of Staff [1]."}]
Q = "what can the consultant do?"


def test_norm_text_ignores_case_spacing_and_trailing_punctuation():
    assert norm_text("What are the  Roles?") == norm_text("what are the roles") == "what are the roles"
    assert norm_text("ما هي الأدوار؟") == norm_text("ما هي الأدوار")
    assert norm_text("UC-C01.") == "uc-c01"
    assert norm_text("roles of UC-C01") != norm_text("roles of UC-C02")    # meaning is never merged
    assert norm_text(None) == ""


@pytest.fixture
def store(monkeypatch):
    rows: dict = {}
    monkeypatch.setattr(C.cache, "get", lambda ns, parts: rows.get((ns, repr(parts))))
    monkeypatch.setattr(C.cache, "set", lambda ns, parts, v, **k: rows.__setitem__((ns, repr(parts)), v))
    return rows


def _llm(monkeypatch, reply):
    calls = []
    def fake(*a, **k):
        calls.append(k.get("model"))
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(C.engine, "complete_chat", fake)
    return calls


def test_a_repeat_follow_up_skips_the_llm_and_gets_the_same_query(store, monkeypatch):
    calls = _llm(monkeypatch, "Consultant permissions and responsibilities")
    first = C.condense(HIST, Q, model="m")
    second = C.condense(HIST, "What can the consultant do", model="m")   # trivially different wording
    assert first == second == "Consultant permissions and responsibilities"
    assert calls == ["m"]


def test_key_includes_history_and_model(store, monkeypatch):
    calls = _llm(monkeypatch, "Consultant permissions and responsibilities")
    C.condense(HIST, Q, model="m")
    C.condense(HIST, Q, model="other")
    C.condense(HIST[:1] + [{"role": "assistant", "content": "The roles are Consultant [2]."}], Q, model="m")
    assert len(calls) == 3


@pytest.mark.parametrize("reply", ["User Safety: safe", "", ProviderError("busy", 429)])
def test_unusable_rewrites_and_errors_are_not_cached(store, monkeypatch, reply):
    _llm(monkeypatch, reply)
    assert C.condense(HIST, Q, model="m") == Q
    assert store == {}


def test_no_history_never_touches_the_cache(store, monkeypatch):
    calls = _llm(monkeypatch, "x")
    assert C.condense([], Q, model="m") == Q and calls == [] and store == {}
