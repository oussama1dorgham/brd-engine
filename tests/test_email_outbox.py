"""Email outbox: enqueue cooldown/fail-open, send routing, sweeper idempotency.

Pure logic — no DB. A fake pool/cursor stands in for Postgres so we exercise the
decision paths (the atomic-claim SQL itself is validated by a live integration run).
"""
import backend.email_outbox as ob


# --- fake DB plumbing ------------------------------------------------------

class _Cur:
    def __init__(self, results):
        self.results = list(results)
        self.executed = []
        self.rowcount = 0

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.results.pop(0) if self.results else None


class _Conn:
    def __init__(self, cur): self._cur = cur
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return self._cur


class _Pool:
    def __init__(self, cur): self._cur = cur
    def connection(self): return _Conn(self._cur)


def _use_cursor(monkeypatch, cur):
    monkeypatch.setattr(ob, "pool", lambda: _Pool(cur))


# --- enqueue ---------------------------------------------------------------

def test_enqueue_inserts_when_no_cooldown(monkeypatch):
    cur = _Cur([None, (777,)])          # cooldown SELECT -> none; INSERT -> id 777
    _use_cursor(monkeypatch, cur)
    rid = ob.enqueue("a@b.com", "Sub", "Body", send_now=False)
    assert rid == 777
    kinds = [sql.split()[0].lower() for sql, _ in cur.executed]
    assert kinds == ["select", "insert"]


def test_enqueue_suppressed_within_cooldown(monkeypatch):
    cur = _Cur([(1,)])                   # cooldown SELECT -> a dead-letter row exists
    _use_cursor(monkeypatch, cur)
    rid = ob.enqueue("burned@b.com", "Sub", "Body", send_now=False)
    assert rid is None
    assert [sql.split()[0].lower() for sql, _ in cur.executed] == ["select"]  # never INSERTs


def test_enqueue_is_fail_open(monkeypatch):
    def boom(): raise RuntimeError("db down")
    monkeypatch.setattr(ob, "pool", boom)
    assert ob.enqueue("a@b.com", "Sub", "Body", send_now=False) is None


def test_enqueue_fires_immediate_send(monkeypatch):
    cur = _Cur([None, (5,)])
    _use_cursor(monkeypatch, cur)
    started = {}
    class _T:
        def __init__(self, target=None, args=(), **k): started["target"], started["args"] = target, args
        def start(self): started["ran"] = True
    monkeypatch.setattr(ob.threading, "Thread", _T)
    ob.enqueue("a@b.com", "Sub", "Body", send_now=True)
    assert started["ran"] and started["target"] is ob._try_send_now and started["args"] == (5,)


# --- send routing (success / retry / dead-letter decided in _finish) --------

def test_send_claimed_records_success(monkeypatch):
    calls = []
    monkeypatch.setattr(ob, "send_email", lambda *a, **k: True)
    monkeypatch.setattr(ob, "_finish", lambda *a: calls.append(a))
    row = (5, "a@b.com", "Sub", "Body", "<html>", 2, 5)   # attempts=2, max=5
    assert ob._send_claimed(row) is True
    assert calls == [(5, True, 2, 5, None)]


def test_send_claimed_records_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(ob, "send_email", lambda *a, **k: False)
    monkeypatch.setattr(ob, "_finish", lambda *a: calls.append(a))
    row = (9, "a@b.com", "Sub", "Body", None, 5, 5)       # attempts==max -> will dead-letter
    assert ob._send_claimed(row) is False
    assert calls == [(9, False, 5, 5, "send failed")]


# --- sweeper ---------------------------------------------------------------

def test_start_sweeper_is_idempotent(monkeypatch):
    monkeypatch.setattr(ob, "_started", False)
    count = {"n": 0}
    class _T:
        def __init__(self, *a, **k): pass
        def start(self): count["n"] += 1
    monkeypatch.setattr(ob.threading, "Thread", _T)
    ob.start_sweeper()
    ob.start_sweeper()
    assert count["n"] == 1
