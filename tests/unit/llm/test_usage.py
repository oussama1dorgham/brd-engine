"""llm.usage — OpenRouter key status parsing + caching, Voyage budget snapshot (no network)."""
import pytest

from backend.core.voyage import Budget
from backend.llm import usage


def test_parse_key_status_reads_free_allowance_and_credits():
    out = usage.parse_key_status({"is_free_tier": True, "usage": 0.00066, "usage_daily": 0, "limit": None,
                                  "limit_remaining": None,
                                  "free_model_daily_requests": {"used": 7, "limit": 50, "remaining": 43}})
    assert out["available"] and out["is_free_tier"]
    assert out["free_daily"] == {"used": 7, "limit": 50, "remaining": 43}
    assert out["credits"]["used_total"] == 0.00066 and out["credits"]["limit"] is None


def test_parse_key_status_without_free_field():
    assert usage.parse_key_status({"usage": 1.5})["free_daily"] is None      # older / paid keys


def test_non_openrouter_keys_report_nothing():
    assert usage.openrouter_status("sk-x", "https://api.openai.com/v1") is None
    assert usage.openrouter_status(None) is None
    assert usage.is_openrouter("https://openrouter.ai/api/v1")


class _Resp:
    def __init__(self, data): self._d = data
    def raise_for_status(self): pass
    def json(self): return {"data": self._d}


def test_status_is_cached_and_forced_refresh_is_throttled(monkeypatch):
    calls = []
    monkeypatch.setattr(usage, "_cache", {})
    monkeypatch.setattr(usage.httpx, "get", lambda url, **k: calls.append(url) or _Resp(
        {"free_model_daily_requests": {"used": len(calls), "limit": 50, "remaining": 50 - len(calls)}}))
    clock = [1000.0]
    monkeypatch.setattr(usage.time, "monotonic", lambda: clock[0])
    a = usage.openrouter_status("k", "https://openrouter.ai/api/v1")
    b = usage.openrouter_status("k", "https://openrouter.ai/api/v1")
    assert len(calls) == 1 and a["free_daily"]["used"] == b["free_daily"]["used"] == 1   # cached
    usage.openrouter_status("k", "https://openrouter.ai/api/v1", force=True)
    assert len(calls) == 1                                        # forced, but < 10 s ago
    clock[0] += 11
    assert usage.openrouter_status("k", "https://openrouter.ai/api/v1", force=True)["free_daily"]["used"] == 2
    clock[0] += 61
    usage.openrouter_status("k", "https://openrouter.ai/api/v1")
    assert len(calls) == 3                                        # stale after a minute
    assert calls[0].endswith("/api/v1/key")


def test_status_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(usage, "_cache", {})
    def boom(*a, **k):
        raise usage.httpx.ConnectError("down")
    monkeypatch.setattr(usage.httpx, "get", boom)
    out = usage.openrouter_status("k2", "https://openrouter.ai/api/v1")
    assert out["available"] is False and "error" in out


def test_budget_snapshot_counts_the_current_window():
    t = [0.0]
    b = Budget(3, 10_000, 1, clock=lambda: t[0])
    b.acquire("query", 120)
    b.acquire("ingest", 800)
    s = b.snapshot()
    assert (s["rpm_used"], s["rpm_limit"], s["tpm_used"], s["tpm_limit"]) == (2, 3, 920, 10_000)
    assert s["window_resets_in"] == pytest.approx(60.0) and s["blocked_for"] == 0
    t[0] = 61.0
    assert b.snapshot()["rpm_used"] == 0                          # the window rolled over
