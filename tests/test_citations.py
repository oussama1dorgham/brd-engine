"""Citation parsing/validation used to map [n] back to real sources."""
from backend.generate.answer import parse_citations


def test_extracts_valid_citations():
    assert parse_citations("uses SSO [1] and SAML [3].", 5) == [1, 3]


def test_dedups_and_sorts():
    assert parse_citations("[3][1][3][2]", 5) == [1, 2, 3]


def test_drops_out_of_range():
    # only 2 sources supplied; [9] must be discarded
    assert parse_citations("[1] and [9]", 2) == [1]


def test_no_citations_returns_empty():
    assert parse_citations("no brackets here", 5) == []
