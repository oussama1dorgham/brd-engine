"""Parser: front matter + structure-aware section extraction."""
from backend.ingest.parser import _split_front_matter, parse

MD = """---
title: Test BRD
project: demo
version: v2
status: approved
---

## 1. Objectives
Some objective text.

## 2. Functional Requirements

### FR-1 — Login
Users MUST log in.

### FR-2 — Logout
Users MUST log out.
"""


def test_front_matter_parsed():
    fm, body = _split_front_matter(MD)
    assert fm["project"] == "demo"
    assert fm["version"] == "v2"
    assert "## 1. Objectives" in body
    assert "title:" not in body  # front matter stripped from body


def test_meta_and_sections(tmp_path):
    p = tmp_path / "b.md"
    p.write_text(MD, encoding="utf-8")
    meta, sections = parse(p)

    assert meta.title == "Test BRD"
    assert meta.project == "demo"
    assert meta.version == "v2"

    # a heading with only sub-headings (## 2) carries no body -> not emitted
    req_ids = [s.req_id for s in sections]
    assert "FR-1" in req_ids and "FR-2" in req_ids


def test_req_id_and_section_context(tmp_path):
    p = tmp_path / "b.md"
    p.write_text(MD, encoding="utf-8")
    _, sections = parse(p)

    objectives = next(s for s in sections if s.title.startswith("1. Objectives"))
    assert objectives.req_id is None

    fr1 = next(s for s in sections if s.req_id == "FR-1")
    assert "Functional Requirements" in fr1.section
    assert "MUST log in" in fr1.text
