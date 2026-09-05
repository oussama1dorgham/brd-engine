"""Condense: turn 1 (no history) must not call the LLM."""
from backend.generate.condense import condense


def test_empty_history_returns_question_unchanged():
    q = "What protocol does it use?"
    # No history -> returned verbatim, no network call.
    assert condense([], q) == q
