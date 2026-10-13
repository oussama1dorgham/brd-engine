"""An abstention is never cached or replayed: with a flaky model it is a miss, not a fact."""
from backend.chat import session as S
from backend.chat.answer import ABSTAIN

CHUNK = {"chunk_id": 1, "req_id": "US-1", "section": "s", "project": "p", "score": 0.9,
         "text": "The Content Consultant can evaluate each support or decision point."}


def _session(monkeypatch, cached, reply):
    stored = []
    monkeypatch.setattr(S, "retrieve", lambda *a, **k: [dict(CHUNK)])
    monkeypatch.setattr(S, "verify_answer", lambda q, text, *a, **k: text)
    monkeypatch.setattr(S.cache, "get", lambda ns, parts: cached)
    monkeypatch.setattr(S.cache, "set", lambda ns, parts, value, **k: stored.append(value["answer"]))
    s = S.ChatSession(project="p", owner_id=1, persist=False)
    monkeypatch.setattr(s, "_generate", lambda *a, **k: reply)
    return s, stored


def test_a_cached_abstention_is_ignored_and_the_question_is_answered(monkeypatch):
    s, stored = _session(monkeypatch, {"answer": ABSTAIN}, "The consultant evaluates each point [1].")
    assert s.ask("what does the content consultant do?")["answer"] == "The consultant evaluates each point [1]."
    assert stored == ["The consultant evaluates each point [1]."]


def test_a_fresh_abstention_is_not_cached(monkeypatch):
    s, stored = _session(monkeypatch, None, ABSTAIN)
    assert s.ask("what does the content consultant do?")["answer"] == ABSTAIN
    assert stored == []


def test_a_cached_real_answer_is_still_replayed(monkeypatch):
    s, stored = _session(monkeypatch, {"answer": "Cached [1]."}, "never generated")
    assert s.ask("what does the content consultant do?")["answer"] == "Cached [1]."
