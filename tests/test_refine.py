"""Refinement layer: query routing (front) + grounding guard (back)."""
from backend.generate.answer import ABSTAIN
from backend.generate.refine import SMALLTALK_REPLY, is_grounded, refine_query


def test_greeting_routes_to_smalltalk():
    kind, payload = refine_query("hi")
    assert kind == "smalltalk"
    assert payload == SMALLTALK_REPLY


def test_arabic_greeting_routes_to_smalltalk():
    kind, _ = refine_query("مرحبا")
    assert kind == "smalltalk"


def test_real_question_passes_through_normalized():
    kind, payload = refine_query("  What are the   objectives? ")
    assert kind == "answer"
    assert payload == "What are the objectives?"   # whitespace collapsed


def test_long_message_starting_with_hi_is_not_smalltalk():
    kind, _ = refine_query("hi, can you explain the objectives of the directives system in detail?")
    assert kind == "answer"


def test_is_grounded_true_when_cited():
    assert is_grounded("Uses SSO [1].", 3) is True


def test_is_grounded_false_when_uncited():
    assert is_grounded("Uses SSO.", 3) is False


def test_is_grounded_true_when_abstains():
    assert is_grounded(ABSTAIN, 3) is True
