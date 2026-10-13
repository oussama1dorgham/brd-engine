"""Refinement layer: query routing (front) + grounding guard (back)."""
from backend.chat.answer import ABSTAIN
from backend.chat.refine import SMALLTALK_REPLY, is_grounded, refine_query


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


# --- uncited but source-backed answers (a free model forgot the [n] markers) ---------------------
from types import SimpleNamespace  # noqa: E402

from backend.chat import refine  # noqa: E402

_CHUNKS = [
    {"req_id": "US-1", "section": "s", "project": "p", "text": "Manage Support/Decision Points. Content Consultant can evaluate each support or decision "
             "point as suitable, not suitable, or not applicable. Justification is required."},
    {"req_id": "US-2", "section": "s", "project": "p", "text": "As a Content Officer, I want to view previous meetings in the same meeting series. "
             "Each previous meeting shows the meeting name and date."},
]


def test_uncited_lines_backed_by_one_source_get_that_source():
    draft = ("Based on the provided sources, the user stories are:\n"
             "- The Content Consultant can evaluate each support or decision point as suitable.\n"
             "- Each previous meeting shows the meeting name and date.  \n"
             "The BRDs do not specify any further stories.")
    lines = refine.attach_citations(draft, _CHUNKS).split("\n")
    assert lines[1].endswith("as suitable. [1]")
    assert lines[2] == "- Each previous meeting shows the meeting name and date. [2]  "   # trailing break kept
    assert "[" not in lines[3]                               # not supported by any source → stays uncited
    assert is_grounded("\n".join(lines), len(_CHUNKS))


def test_unsupported_answers_are_not_vouched_for():
    draft = "Payments are refunded within five business days by the finance team."
    assert refine.attach_citations(draft, _CHUNKS) == draft
    assert not is_grounded(refine.attach_citations(draft, _CHUNKS), len(_CHUNKS))


def test_already_cited_lines_are_left_alone():
    draft = "Each previous meeting shows the meeting name and date [1]."
    assert refine.attach_citations(draft, _CHUNKS) == draft


def test_verifier_that_drops_every_citation_keeps_the_draft(monkeypatch):
    monkeypatch.setattr(refine, "settings", SimpleNamespace(refine_verify=True, gen_max_tokens=500))
    draft = "Each previous meeting shows the meeting name and date [2]."
    for reply in ("Each previous meeting shows the meeting name and date.", "User Safety: safe", ""):
        monkeypatch.setattr(refine.engine, "complete_chat", lambda *a, **k: reply)
        assert refine.verify_answer("q", draft, _CHUNKS) == draft, reply
    monkeypatch.setattr(refine.engine, "complete_chat", lambda *a, **k: ABSTAIN)
    assert refine.verify_answer("q", draft, _CHUNKS) == ABSTAIN   # a real "nothing supported" still wins
