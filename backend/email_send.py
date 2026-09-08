"""Email sender for the auth flows (verification / password reset).

Transport is chosen by _transport():
  resend        EMAIL_PROVIDER=resend + RESEND_API_KEY — Resend HTTPS API (port
                443). From-address must be on a Resend-verified domain.
  elasticemail  EMAIL_PROVIDER=elasticemail + ELASTICEMAIL_API_KEY — Elastic Email
                v4 HTTPS API (port 443). Supports single-sender verification (no
                domain required). Use either on hosts that block outbound SMTP
                (Render & most PaaS free tiers).
  smtp          SMTP_HOST is set — real SMTP mail (see SMTP_SECURITY below).
  dev     neither configured — the message (incl. any link/code) is logged to the
          server console so auth flows work in development without a provider.

For the SMTP transport, TLS is chosen by SMTP_SECURITY:
  starttls  plain connect on SMTP_PORT (usually 587), then upgrade with STARTTLS
  ssl       implicit TLS from the first byte on SMTP_PORT (usually 465)
  none      no TLS at all — for local dev relays (MailHog, etc.)

send_email() returns True on success (or in dev-log mode) and False on failure;
it never raises, so a delivery problem can't break the request that triggered it.
"""
from __future__ import annotations

import html as _html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from .config import settings

log = logging.getLogger("brd.email")

BRAND = "BRD Retrieval Engine"


def app_base_url() -> str:
    """Base URL used to build verification / reset links."""
    return settings.app_base_url


def _sender() -> str:
    """From header for both transports: EMAIL_FROM, else SMTP_FROM/SMTP_USER."""
    addr = settings.email_from or settings.smtp_from or settings.smtp_user or "no-reply@brd.local"
    name = settings.email_from_name or settings.smtp_from_name
    return formataddr((name, addr)) if name else addr


def _transport() -> str:
    """Which transport send_email() uses: 'resend', 'elasticemail', 'smtp', or 'dev'."""
    if settings.email_provider == "resend" and settings.resend_api_key:
        return "resend"
    if settings.email_provider == "elasticemail" and settings.elasticemail_api_key:
        return "elasticemail"
    if settings.smtp_host:
        return "smtp"
    return "dev"


def _connect() -> smtplib.SMTP:
    """Open an SMTP connection per SMTP_SECURITY. Caller closes it."""
    host, port = settings.smtp_host, settings.smtp_port
    if settings.smtp_security == "ssl":
        return smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
    conn = smtplib.SMTP(host, port, timeout=15)
    if settings.smtp_security == "starttls":
        conn.ehlo()
        conn.starttls(context=ssl.create_default_context())
        conn.ehlo()
    return conn  # 'none' => plain, no TLS


def _send_via_smtp(to: str, subject: str, body: str, html: str | None) -> None:
    msg = EmailMessage()
    msg["From"] = _sender()
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)      # deliverability: real Date + Message-ID
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    with _connect() as s:
        if settings.smtp_user and settings.smtp_password:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)


def _send_via_resend(to: str, subject: str, body: str, html: str | None) -> None:
    """POST to Resend's HTTPS API (port 443) — works where outbound SMTP is blocked."""
    import json
    import urllib.error
    import urllib.request

    payload: dict = {"from": _sender(), "to": [to], "subject": subject, "text": body}
    if html:
        payload["html"] = html
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        # Cloudflare fronts api.resend.com and blocks the default "Python-urllib/x"
        # User-Agent as a bot signature (403, CF code 1010) — send a real UA.
        headers={"Authorization": f"Bearer {settings.resend_api_key}",
                 "Content-Type": "application/json",
                 "User-Agent": f"{BRAND}/1.0 (+https://resend.com)",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.HTTPError as e:  # surface Resend's message (e.g. unverified from-domain)
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Resend API {e.code}: {detail}") from e


def _send_via_elasticemail(to: str, subject: str, body: str, html: str | None) -> None:
    """POST to Elastic Email's v4 HTTPS API (port 443) — works where SMTP is blocked.
    Elastic Email allows single-sender verification, so a verified from-address need
    not be on a fully DNS-verified domain."""
    import json
    import urllib.error
    import urllib.request

    content_body = [{"ContentType": "PlainText", "Content": body}]
    if html:
        content_body.append({"ContentType": "HTML", "Content": html})
    payload = {"Recipients": [{"Email": to}],
               "Content": {"From": _sender(), "Subject": subject, "Body": content_body}}
    req = urllib.request.Request(
        "https://api.elasticemail.com/v4/emails",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"X-ElasticEmail-ApiKey": settings.elasticemail_api_key or "",
                 "Content-Type": "application/json",
                 "User-Agent": f"{BRAND}/1.0",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.HTTPError as e:  # surface EE's message (e.g. unverified sender)
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Elastic Email API {e.code}: {detail}") from e


def send_email(to: str, subject: str, body: str, html: str | None = None) -> bool:
    """Send one email. Returns True on success / dev-log, False on failure. Never raises."""
    transport = _transport()
    if transport == "dev":
        log.info("EMAIL (dev — no transport configured, not actually sent):\n"
                 "  to:      %s\n  subject: %s\n  %s", to, subject, body.replace("\n", "\n  "))
        return True
    try:
        if transport == "resend":
            _send_via_resend(to, subject, body, html)
        elif transport == "elasticemail":
            _send_via_elasticemail(to, subject, body, html)
        else:
            _send_via_smtp(to, subject, body, html)
        # NB: never log the subject/body — OTP codes live in the subject line.
        log.info("sent email to %s via %s", to, transport)
        return True
    except Exception:  # noqa: BLE001 — email failure must not break the request
        log.exception("failed to send email to %s via %s", to, transport)
        return False


# --- Branded templates -----------------------------------------------------
# Email-safe HTML: table layout + inline CSS (mail clients strip <style>/external
# CSS). Everything interpolated is HTML-escaped. Plain-text is always sent too.

def render_action_email(heading: str, lead: str, button_label: str, url: str, footer: str) -> str:
    h, l, b, u, f = (_html.escape(x) for x in (heading, lead, button_label, url, footer))
    return f"""\
<!doctype html><html><body style="margin:0;padding:0;background:#f4f5f7;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;">
    <tr><td align="center" style="padding:32px 16px;">
      <table role="presentation" width="480" cellpadding="0" cellspacing="0"
             style="max-width:480px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;
                    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
        <tr><td style="background:#1f2937;padding:20px 28px;color:#ffffff;font-size:16px;font-weight:600;">
          {_html.escape(BRAND)}
        </td></tr>
        <tr><td style="padding:28px;">
          <h1 style="margin:0 0 12px;font-size:20px;color:#111827;">{h}</h1>
          <p style="margin:0 0 24px;font-size:14px;line-height:22px;color:#374151;">{l}</p>
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td style="border-radius:8px;background:#2563eb;">
              <a href="{u}" style="display:inline-block;padding:12px 22px;font-size:14px;font-weight:600;
                 color:#ffffff;text-decoration:none;border-radius:8px;">{b}</a>
            </td>
          </tr></table>
          <p style="margin:24px 0 0;font-size:12px;line-height:18px;color:#6b7280;">
            Or paste this link into your browser:<br>
            <a href="{u}" style="color:#2563eb;word-break:break-all;">{u}</a>
          </p>
        </td></tr>
        <tr><td style="padding:18px 28px;border-top:1px solid #eef0f3;font-size:12px;color:#9ca3af;">{f}</td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def build_verification(link: str, hours: int) -> tuple[str, str, str]:
    """(subject, plain, html) for the email-verification message."""
    subject = f"Verify your email — {BRAND}"
    plain = (f"Welcome to {BRAND}! Confirm your email to finish setting up your account:\n\n"
             f"{link}\n\nThis link expires in {hours} hours.")
    html = render_action_email(
        "Confirm your email",
        f"Welcome to {BRAND}! Click the button below to verify your address and finish setting up your account.",
        "Verify email", link, f"This link expires in {hours} hours. If you didn't sign up, you can ignore this email.")
    return subject, plain, html


def build_reset(link: str, hours: int) -> tuple[str, str, str]:
    """(subject, plain, html) for the password-reset message."""
    subject = f"Reset your password — {BRAND}"
    plain = (f"Reset your password with this link:\n\n{link}\n\n"
             f"It expires in {hours} hour(s). If you didn't request this, ignore this email.")
    html = render_action_email(
        "Reset your password",
        "We received a request to reset your password. Click the button below to choose a new one.",
        "Reset password", link, f"This link expires in {hours} hour(s). If you didn't request this, ignore this email.")
    return subject, plain, html


def render_code_email(heading: str, lead: str, code: str, footer: str) -> str:
    h, l, c, f = (_html.escape(x) for x in (heading, lead, code, footer))
    return f"""\
<!doctype html><html><body style="margin:0;padding:0;background:#f4f5f7;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;">
    <tr><td align="center" style="padding:32px 16px;">
      <table role="presentation" width="480" cellpadding="0" cellspacing="0"
             style="max-width:480px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;
                    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
        <tr><td style="background:#1f2937;padding:20px 28px;color:#ffffff;font-size:16px;font-weight:600;">
          {_html.escape(BRAND)}
        </td></tr>
        <tr><td style="padding:28px;">
          <h1 style="margin:0 0 12px;font-size:20px;color:#111827;">{h}</h1>
          <p style="margin:0 0 20px;font-size:14px;line-height:22px;color:#374151;">{l}</p>
          <div style="font-size:34px;font-weight:700;letter-spacing:10px;color:#111827;
                      background:#f3f4f6;border-radius:10px;padding:18px 0;text-align:center;">{c}</div>
        </td></tr>
        <tr><td style="padding:18px 28px;border-top:1px solid #eef0f3;font-size:12px;color:#9ca3af;">{f}</td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def build_code(kind: str, code: str, minutes: int) -> tuple[str, str, str]:
    """(subject, plain, html) for an OTP code email. kind: 'signup'|'login'|'reset'."""
    headings = {
        "signup": ("Confirm your email", "Use this code to verify your email and finish signing up."),
        "login": ("Your login code", "Use this code to finish signing in."),
        "reset": ("Your password reset code", "Use this code to reset your password."),
    }
    heading, lead = headings.get(kind, ("Your verification code", "Use this code to continue."))
    subject = f"{code} is your {BRAND} code"
    footer = f"This code expires in {minutes} minutes. If you didn't request it, you can ignore this email."
    plain = f"{lead}\n\nYour code: {code}\n\n{footer}"
    html = render_code_email(heading, lead, code, footer)
    return subject, plain, html
