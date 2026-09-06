"""Symmetric encryption for secrets stored at rest (users' provider API keys).

Uses Fernet (AES-128-CBC + HMAC) with a key from LLM_KEY_SECRET. The key lives
only in the environment, never in the DB — so a DB leak alone can't reveal the
stored API keys. If LLM_KEY_SECRET is unset, `available()` is False and the
bring-your-own-key feature stays disabled rather than storing plaintext.
"""
from __future__ import annotations

from cryptography.fernet import Fernet

from .config import settings


def available() -> bool:
    return bool(settings.llm_key_secret)


def _fernet() -> Fernet:
    if not settings.llm_key_secret:
        raise RuntimeError("LLM_KEY_SECRET is not set — cannot encrypt/decrypt secrets.")
    return Fernet(settings.llm_key_secret.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
