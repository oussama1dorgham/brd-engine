"""Anthropic (Claude) native adapter — the main provider that is NOT
OpenAI-compatible. Uses the Messages API over httpx (no extra SDK dependency):
  GET  /v1/models          -> available model ids (listed dynamically)
  POST /v1/messages        -> completion (system lifted out; roles user/assistant)
  POST /v1/messages stream -> SSE content_block_delta text pieces
"""
from __future__ import annotations

import json
from typing import Iterator

import httpx

from .base import ProviderError, split_system

_BASE = "https://api.anthropic.com"
_VERSION = "2023-06-01"


def _headers(api_key: str) -> dict:
    return {"x-api-key": api_key, "anthropic-version": _VERSION, "content-type": "application/json"}


def _detail(body: str) -> str:
    try:
        return json.loads(body).get("error", {}).get("message", "")
    except Exception:
        return ""


def _friendly(status: int, detail: str = "") -> str:
    if status == 429:
        return "Claude is rate-limited right now — wait a moment and retry."
    if status in (401, 403):
        return "Your Anthropic API key was rejected. Re-check it in Settings → Custom AI model."
    if status == 404:
        return "That Claude model isn't available. Pick another model."
    if status == 400:
        return "Anthropic rejected the request (unsupported model or parameters)."
    if status == 402:
        return "Your Anthropic account is out of credit."
    if status == 529:
        return "Anthropic is overloaded right now — retry shortly."
    return f"Anthropic returned an error ({status})." + (f" {detail}" if detail else "")


class AnthropicAdapter:
    key = "anthropic"
    label = "Anthropic (Claude)"
    needs_base_url = False
    default_base_url = None

    def _base(self, base_url: str | None) -> str:
        return (base_url or _BASE).rstrip("/")

    def _payload(self, model, messages, temperature, max_tokens, stream) -> dict:
        system, rest = split_system(messages)
        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": m["role"], "content": m["content"]} for m in rest],
        }
        if system:
            body["system"] = system
        if stream:
            body["stream"] = True
        return body

    def list_models(self, api_key: str, base_url: str | None) -> list[str]:
        try:
            r = httpx.get(f"{self._base(base_url)}/v1/models", headers=_headers(api_key), timeout=20)
        except httpx.HTTPError as e:
            raise ProviderError("Couldn't reach Anthropic — check your connection.") from e
        if r.status_code != 200:
            raise ProviderError(_friendly(r.status_code, _detail(r.text)), r.status_code)
        data = r.json().get("data", [])
        return sorted({m.get("id") for m in data if m.get("id")})

    def complete(self, api_key, base_url, model, messages, *, temperature, max_tokens) -> str:
        try:
            r = httpx.post(f"{self._base(base_url)}/v1/messages", headers=_headers(api_key),
                           json=self._payload(model, messages, temperature, max_tokens, False), timeout=120)
        except httpx.HTTPError as e:
            raise ProviderError("Couldn't reach Anthropic — check your connection.") from e
        if r.status_code != 200:
            raise ProviderError(_friendly(r.status_code, _detail(r.text)), r.status_code)
        blocks = r.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    def stream(self, api_key, base_url, model, messages, *, temperature, max_tokens) -> Iterator[str]:
        payload = self._payload(model, messages, temperature, max_tokens, True)
        try:
            with httpx.stream("POST", f"{self._base(base_url)}/v1/messages",
                              headers=_headers(api_key), json=payload, timeout=120) as r:
                if r.status_code != 200:
                    body = r.read().decode("utf-8", "ignore")
                    raise ProviderError(_friendly(r.status_code, _detail(body)), r.status_code)
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        ev = json.loads(data)
                    except Exception:
                        continue
                    if ev.get("type") == "content_block_delta":
                        d = ev.get("delta", {})
                        if d.get("type") == "text_delta" and d.get("text"):
                            yield d["text"]
        except httpx.HTTPError as e:
            raise ProviderError("Couldn't reach Anthropic — check your connection.") from e
