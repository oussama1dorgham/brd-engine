"""Pure-logic tests for backend/use_case_testing.py (no DB): upload/serving hardening,
the content-hash retest rule, and requirement coverage roll-up."""
import pytest

from backend.use_cases import testing as uct


@pytest.mark.parametrize("raw,want", [
    ("image/png", "image/png"),
    ("IMAGE/PNG; charset=binary", "image/png"),
    ("text/html", "text/html"),
    ("", "application/octet-stream"),
    ("not a mime", "application/octet-stream"),
    ("image/png\r\nX-Evil: 1", "application/octet-stream"),
])
def test_clean_mime(raw, want):
    assert uct.clean_mime(raw) == want


def test_kind_is_derived_server_side():
    assert uct.kind_for_mime("image/png") == "image"
    assert uct.kind_for_mime("image/svg+xml") == "file"      # svg can carry script
    assert uct.kind_for_mime("video/mp4") == "video"
    assert uct.kind_for_mime("text/html") == "file"


def test_only_passive_types_render_inline():
    assert uct.inline_ok("image/png") and uct.inline_ok("application/pdf")
    assert not uct.inline_ok("text/html")
    assert not uct.inline_ok("image/svg+xml")
    assert not uct.inline_ok("application/octet-stream")


def test_clean_filename_strips_paths_and_quotes():
    assert uct.clean_filename("../../etc/passwd") == "passwd"
    assert uct.clean_filename("C:\\x\\a\"b.png") == "ab.png"
    assert uct.clean_filename("bad\r\nname.txt") == "badname.txt"
    assert uct.clean_filename("   ") is None
    assert len(uct.clean_filename("a" * 500)) == 200


def test_content_disposition_is_injection_safe():
    h = uct.content_disposition('ré"sumé.pdf', inline=False)
    assert h.startswith("attachment; ")
    assert '"' not in h.split('filename="')[1].split('"')[0]    # ascii fallback has no quote
    assert "filename*=UTF-8''r%C3%A9%22sum%C3%A9.pdf" in h
    assert uct.content_disposition(None, inline=True).startswith('inline; filename="proof"')


@pytest.mark.parametrize("url", ["https://jira.example.com/BUG-1", "http://x.io/a?b=c"])
def test_valid_link_accepts_http(url):
    assert uct.valid_link(url) == url


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "ftp://x", "https://",
                                 "", "https://a b.com", "https://x.io/" + "a" * 3000])
def test_valid_link_rejects(url):
    with pytest.raises(uct.TestingError):
        uct.valid_link(url)


def test_is_stale_uses_the_content_hash():
    assert uct.is_stale("passed", "h1", "h2")
    assert uct.is_stale("failed", "h1", "h2")
    assert not uct.is_stale("passed", "h1", "h1")            # identical definition (e.g. restored)
    assert not uct.is_stale("in_progress", "h1", "h2")       # not a verdict
    assert not uct.is_stale("not_tested", None, "h2")
    assert not uct.is_stale("passed", None, "h2")            # never pinned


@pytest.mark.parametrize("statuses,stale,want", [
    ([], 0, "uncovered"),
    (["passed", "failed"], 0, "failed"),
    (["passed", "blocked"], 0, "blocked"),
    (["passed", "passed"], 0, "passed"),
    (["passed", "passed"], 1, "retest"),
    (["not_tested", "not_tested"], 0, "not_run"),
    (["passed", "not_tested"], 0, "partial"),
    (["in_progress"], 0, "partial"),
])
def test_requirement_coverage(statuses, stale, want):
    assert uct._coverage(statuses, stale) == want


# --- run-over-run comparison ---

def test_classify_run_change_covers_every_move():
    from backend.use_cases.testing import classify_run_change as c
    assert c("passed", "failed") == "regressed" and c("passed", "blocked") == "regressed"
    assert c("failed", "blocked") == "still_failing" and c("not_tested", "failed") == "new_failure"
    assert c(None, "failed") == "new_failure"
    assert c("failed", "passed") == "fixed" and c("passed", "passed") == "still_passing"
    assert c("not_tested", "passed") == "newly_passing" and c(None, "passed") == "newly_passing"
    assert c("passed", "not_tested") == "not_rerun" and c("blocked", "in_progress") == "not_rerun"
    assert c("not_tested", "not_tested") == "not_run" and c(None, "in_progress") == "not_run"
    assert c("passed", None) == "dropped"
