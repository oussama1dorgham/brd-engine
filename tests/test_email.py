"""Email sender: dev-log fallback, From resolution, transport selection, fail-open.

Pure logic — no network. A fake SMTP records what would have been sent; the
frozen settings object is swapped module-side (like test_cache) to drive modes.
"""
import backend.email_send as es


class _Cfg:
    """Stand-in for settings; only the fields email_send reads."""
    app_base_url = "http://localhost:8000"
    smtp_host = "smtp.example.com"
    smtp_port = 587
    smtp_user = "mailer@example.com"
    smtp_password = "secret"
    smtp_from = None
    smtp_from_name = None
    smtp_security = "starttls"


class _FakeSMTP:
    """Records construction + calls; used for both SMTP and SMTP_SSL."""
    last = None

    def __init__(self, host, port, timeout=None, context=None):
        _FakeSMTP.last = self
        self.host, self.port, self.context = host, port, context
        self.started_tls = False
        self.logged_in = False
        self.sent = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent = msg


# ---- dev-log mode: no SMTP_HOST => logs and reports success, sends nothing ----

def test_dev_mode_returns_true_without_sending(monkeypatch):
    cfg = _Cfg()
    cfg.smtp_host = None
    monkeypatch.setattr(es, "settings", cfg)
    monkeypatch.setattr(es.smtplib, "SMTP", _boom)          # prove no send is attempted
    monkeypatch.setattr(es.smtplib, "SMTP_SSL", _boom)
    assert es.send_email("u@x.com", "Hi", "body") is True


# ---- From header resolution ----

def test_sender_falls_back_to_user(monkeypatch):
    monkeypatch.setattr(es, "settings", _Cfg())
    assert es._sender() == "mailer@example.com"


def test_sender_uses_display_name(monkeypatch):
    cfg = _Cfg()
    cfg.smtp_from = "no-reply@brd.app"
    cfg.smtp_from_name = "BRD Engine"
    monkeypatch.setattr(es, "settings", cfg)
    assert es._sender() == "BRD Engine <no-reply@brd.app>"


# ---- transport selection ----

def test_starttls_mode_upgrades_and_sends(monkeypatch):
    monkeypatch.setattr(es, "settings", _Cfg())
    monkeypatch.setattr(es.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(es.smtplib, "SMTP_SSL", _boom)      # must NOT be used
    assert es.send_email("u@x.com", "Sub", "Body") is True
    s = _FakeSMTP.last
    assert s.started_tls and s.logged_in == ("mailer@example.com", "secret")
    assert s.sent["To"] == "u@x.com" and s.sent["Message-ID"] and s.sent["Date"]


def test_ssl_mode_uses_smtp_ssl(monkeypatch):
    cfg = _Cfg()
    cfg.smtp_security = "ssl"
    cfg.smtp_port = 465
    monkeypatch.setattr(es, "settings", cfg)
    monkeypatch.setattr(es.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(es.smtplib, "SMTP", _boom)          # implicit TLS => plain SMTP unused
    assert es.send_email("u@x.com", "Sub", "Body") is True
    assert _FakeSMTP.last.port == 465 and _FakeSMTP.last.started_tls is False


def test_none_mode_skips_tls(monkeypatch):
    cfg = _Cfg()
    cfg.smtp_security = "none"
    monkeypatch.setattr(es, "settings", cfg)
    monkeypatch.setattr(es.smtplib, "SMTP", _FakeSMTP)
    assert es.send_email("u@x.com", "Sub", "Body") is True
    assert _FakeSMTP.last.started_tls is False


# ---- fail-open: a broken transport returns False, never raises ----

def _boom(*a, **k):
    raise OSError("connection refused")


def test_send_failure_returns_false(monkeypatch):
    monkeypatch.setattr(es, "settings", _Cfg())
    monkeypatch.setattr(es.smtplib, "SMTP", _boom)
    assert es.send_email("u@x.com", "Sub", "Body") is False
