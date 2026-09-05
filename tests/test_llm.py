"""message_text() must survive malformed responses (the 'NoneType' crash fix)."""
from types import SimpleNamespace

from backend.generate.llm import message_text


def _resp(choices):
    return SimpleNamespace(choices=choices)


def test_normal_response():
    r = _resp([SimpleNamespace(message=SimpleNamespace(content="hello"))])
    assert message_text(r) == "hello"


def test_choices_none():           # the exact crash: choices is None
    assert message_text(_resp(None)) == ""


def test_choices_empty():
    assert message_text(_resp([])) == ""


def test_message_none():
    assert message_text(_resp([SimpleNamespace(message=None)])) == ""


def test_content_none():
    r = _resp([SimpleNamespace(message=SimpleNamespace(content=None))])
    assert message_text(r) == ""
