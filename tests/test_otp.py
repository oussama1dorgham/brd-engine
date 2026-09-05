"""OTP module: code format, input guards, and the login step-up math.

Pure logic — no DB (the issue/verify DB paths are covered by a live integration run).
"""
import backend.otp as otp


def test_generate_code_is_six_digits():
    for _ in range(50):
        c = otp.generate_code()
        assert len(c) == 6 and c.isdigit()
        assert 0 <= int(c) <= 999999


def test_verify_rejects_nondigit_without_db(monkeypatch):
    # empty / non-numeric codes short-circuit before any DB access
    def boom(): raise AssertionError("must not touch the DB")
    monkeypatch.setattr(otp, "pool", boom)
    assert otp.verify(1, otp.SIGNUP, "") is False
    assert otp.verify(1, otp.SIGNUP, "12ab56") is False
    assert otp.verify(1, otp.SIGNUP, "abcdef") is False


# --- login step-up math (fake DB returns the post-increment count) ---

class _Cur:
    def __init__(self, count): self.count = count
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): pass
    def fetchone(self): return (self.count,)


class _Conn:
    def __init__(self, cur): self._c = cur
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return self._c
    def commit(self): pass


class _Pool:
    def __init__(self, cur): self._c = cur
    def connection(self): return _Conn(self._c)


class _Cfg:
    otp_login_every = 10


def _use(monkeypatch, count):
    monkeypatch.setattr(otp, "settings", _Cfg())
    monkeypatch.setattr(otp, "pool", lambda: _Pool(_Cur(count)))


def test_login_needs_otp_on_the_interval(monkeypatch):
    _use(monkeypatch, 10)          # 10th login
    assert otp.login_needs_otp(1) is True


def test_login_no_otp_off_interval(monkeypatch):
    _use(monkeypatch, 11)
    assert otp.login_needs_otp(1) is False
    _use(monkeypatch, 1)
    assert otp.login_needs_otp(1) is False
