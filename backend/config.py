"""Central configuration for the BRD Retrieval Engine.

Everything tunable — provider base URL and every model name — comes from `.env`,
with NO hardcoded fallbacks, so `.env` is the single source of truth and a
missing/misspelled variable fails loudly instead of silently using a default.

Keys and model names are read at import; they're validated at first use via the
require_*/need helpers, so DB-only work (Phase 0) runs before any key exists.

    python -m backend.config          # print the resolved config (secrets masked)
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


@dataclass(frozen=True)
class Settings:
    # Required for everything
    database_url: str = _required("DATABASE_URL")

    # Credentials — optional at import, validated at first use
    voyage_api_key: str | None = os.getenv("VOYAGE_API_KEY") or None
    openrouter_api_key: str | None = os.getenv("OPENROUTER_API_KEY") or None

    # Provider + model choices — ALL from .env, no code-side defaults
    openrouter_base_url: str | None = os.getenv("OPENROUTER_BASE_URL") or None
    embed_model_docs: str | None = os.getenv("EMBED_MODEL_DOCS") or None
    embed_model_query: str | None = os.getenv("EMBED_MODEL_QUERY") or None
    rerank_model: str | None = os.getenv("RERANK_MODEL") or None
    gen_model: str | None = os.getenv("GEN_MODEL") or None
    # Max output tokens for a generated answer. 600 (~450 words) truncated longer
    # grounded answers mid-sentence; raise here or via GEN_MAX_TOKENS.
    gen_max_tokens: int = int(os.getenv("GEN_MAX_TOKENS", "1500") or "1500")

    # Refinement layer: gated LLM answer-verification (extra call; best on a strong model)
    refine_verify: bool = (os.getenv("REFINE_VERIFY", "0").strip().lower() in ("1", "true", "yes"))

    # Caching: memoize the paid/rate-limited remote stages (embed/retrieve/answer)
    # in Postgres. Advisory + fail-open — see backend/cache.py. On by default.
    cache_enabled: bool = (os.getenv("CACHE_ENABLED", "1").strip().lower() in ("1", "true", "yes"))
    # Safety-net TTL (seconds) for corpus-dependent namespaces; 0 = no age limit
    # (busting on ingest is the primary invalidation). 'embed' is immutable, never expires.
    cache_ttl_seconds: int = int(os.getenv("CACHE_TTL_SECONDS", "0") or "0")

    # Email transport selection. 'smtp' (default) uses the SMTP_* settings below;
    # 'resend' sends over Resend's HTTPS API (RESEND_API_KEY) — needed on hosts
    # (Render/most PaaS free tiers) that block outbound SMTP ports. With neither a
    # usable transport nor SMTP_HOST, send falls back to dev-log mode.
    email_provider: str = os.getenv("EMAIL_PROVIDER", "smtp").strip().lower()
    resend_api_key: str | None = os.getenv("RESEND_API_KEY") or None
    # From address for BOTH transports. Falls back to SMTP_FROM/SMTP_USER for
    # backward compat. NOTE: Resend requires this to be a verified-domain address
    # (or onboarding@resend.dev for testing) — a plain gmail.com from is rejected.
    email_from: str | None = os.getenv("EMAIL_FROM") or None
    email_from_name: str | None = os.getenv("EMAIL_FROM_NAME") or None

    # Email / SMTP — all optional. No SMTP_HOST => dev mode: the message (incl. any
    # link) is logged to the server console instead of being sent.
    app_base_url: str = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    smtp_host: str | None = os.getenv("SMTP_HOST") or None
    smtp_port: int = int(os.getenv("SMTP_PORT", "587") or "587")
    smtp_user: str | None = os.getenv("SMTP_USER") or None
    smtp_password: str | None = os.getenv("SMTP_PASSWORD") or None
    smtp_from: str | None = os.getenv("SMTP_FROM") or None          # falls back to smtp_user
    smtp_from_name: str | None = os.getenv("SMTP_FROM_NAME") or None  # display name
    # Transport security: 'starttls' (587), 'ssl' (465, implicit TLS), or 'none' (dev relays).
    smtp_security: str = os.getenv("SMTP_SECURITY", "starttls").strip().lower()

    # Email outbox / retry (durable send — see backend/email_outbox.py).
    # Retry is a FIXED interval (kinder UX than exponential backoff), not a growing delay.
    email_max_attempts: int = int(os.getenv("EMAIL_MAX_ATTEMPTS", "5") or "5")
    email_retry_interval_seconds: int = int(os.getenv("EMAIL_RETRY_INTERVAL_SECONDS", "180") or "180")
    email_sweep_interval_seconds: int = int(os.getenv("EMAIL_SWEEP_INTERVAL_SECONDS", "30") or "30")
    email_lease_seconds: int = int(os.getenv("EMAIL_LEASE_SECONDS", "300") or "300")
    email_recipient_cooldown_seconds: int = int(os.getenv("EMAIL_RECIPIENT_COOLDOWN_SECONDS", "3600") or "3600")

    # OTP (emailed 6-digit codes) for signup verification, password reset, and
    # periodic login step-up. See backend/otp.py.
    otp_ttl_minutes: int = int(os.getenv("OTP_TTL_MINUTES", "10") or "10")
    otp_max_attempts: int = int(os.getenv("OTP_MAX_ATTEMPTS", "5") or "5")
    otp_login_every: int = int(os.getenv("OTP_LOGIN_EVERY", "10") or "10")  # require a login code every Nth login

    # Custom LLM (bring-your-own-key): Fernet key used to encrypt users' provider
    # API keys at rest. Unset => the BYOK feature is disabled (falls back to GEN_MODEL).
    # Generate one: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    llm_key_secret: str | None = os.getenv("LLM_KEY_SECRET") or None

    @staticmethod
    def need(value: str | None, name: str) -> str:
        """Return value, or raise a clear error naming the missing .env variable."""
        if not value:
            raise RuntimeError(f"{name} is not set in .env")
        return value

    def require_voyage(self) -> str:
        return self.need(self.voyage_api_key, "VOYAGE_API_KEY")

    def require_openrouter(self) -> str:
        return self.need(self.openrouter_api_key, "OPENROUTER_API_KEY")

    def require_base_url(self) -> str:
        return self.need(self.openrouter_base_url, "OPENROUTER_BASE_URL")


settings = Settings()


if __name__ == "__main__":
    def _mask(v: str | None) -> str:
        if not v:
            return "<unset>"
        return f"{v[:6]}…{v[-4:]}" if len(v) > 12 else v

    def _mask_url(url: str) -> str:
        # hide the password in postgresql://user:pass@host/db
        if "@" in url and "://" in url:
            head, tail = url.split("://", 1)
            creds, _, rest = tail.partition("@")
            if ":" in creds:
                user = creds.split(":", 1)[0]
                return f"{head}://{user}:***@{rest}"
        return url

    print("Resolved configuration (source: .env)\n")
    print(f"  DATABASE_URL        = {_mask_url(settings.database_url)}")
    print(f"  VOYAGE_API_KEY      = {_mask(settings.voyage_api_key)}")
    print(f"  OPENROUTER_API_KEY  = {_mask(settings.openrouter_api_key)}")
    print(f"  OPENROUTER_BASE_URL = {settings.openrouter_base_url or '<unset>'}")
    print(f"  EMBED_MODEL_DOCS    = {settings.embed_model_docs or '<unset>'}")
    print(f"  EMBED_MODEL_QUERY   = {settings.embed_model_query or '<unset>'}")
    print(f"  RERANK_MODEL        = {settings.rerank_model or '<unset>'}")
    print(f"  GEN_MODEL           = {settings.gen_model or '<unset>'}")
    print(f"  CACHE_ENABLED       = {settings.cache_enabled}")
    print(f"  CACHE_TTL_SECONDS   = {settings.cache_ttl_seconds}")
    print(f"  APP_BASE_URL        = {settings.app_base_url}")
    print(f"  SMTP_HOST           = {settings.smtp_host or '<unset — dev log mode>'}")
    print(f"  SMTP_PORT           = {settings.smtp_port}")
    print(f"  SMTP_SECURITY       = {settings.smtp_security}")
    print(f"  SMTP_USER           = {_mask(settings.smtp_user)}")
    print(f"  SMTP_PASSWORD       = {_mask(settings.smtp_password)}")
    print(f"  SMTP_FROM           = {settings.smtp_from or settings.smtp_user or '<unset>'}")
    print(f"  EMAIL_MAX_ATTEMPTS  = {settings.email_max_attempts}")
    print(f"  EMAIL_RETRY_INTERVAL= {settings.email_retry_interval_seconds}s (fixed)")
    print(f"  OTP_TTL_MINUTES     = {settings.otp_ttl_minutes}")
    print(f"  OTP_LOGIN_EVERY     = {settings.otp_login_every}")
