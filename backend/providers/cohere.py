"""Cohere (native) adapter — Chat v2 API over httpx.

  GET  /v1/models (chat-capable) -> model ids
  POST /v2/chat                  -> completion
  POST /v2/chat (stream)         -> SSE content-delta text pieces
"""
from __future__ import annotations

import json
from typing import Iterator

import httpx

from .base import ProviderError

_BASE = "https://api.cohere.com"


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "content-type": "application/json", "accept": "application/json"}


def _friendly(status: int, detail: str = "") -> str:
    if status == 429:
        return "Cohere is rate-limited right now — wait a moment and retry."
    if status in (401, 403):
        return "Your Cohere API key was rejected. Re-check it in Settings → Custom AI model."
    if status == 404:
        return "That Cohere model isn't available. Pick another model."
    if status == 400:
        return "Cohere rejected the request (unsupported model or parameters)."
    return f"Cohere returned an error ({status})." + (f" {detail}" if detail else "")


def _detail(body: str) -> str:
    try:
        return json.loads(body).get("message", "")
    except Exception:
        return ""


class CohereAdapter:
    key = "cohere"
    label = "Cohere"
    needs_base_url = False
    default_base_url = None

    def _base(self, base_url: str | None) -> str:
        return (base_url or _BASE).rstrip("/")

    def list_models(self, api_key: str, base_url: str | None) -> list[str]:
        try:
            r = httpx.get(f"{self._base(base_url)}/v1/models",
                          headers=_headers(api_key), params={"page_size": 200}, timeout=20)
        except httpx.HTTPError as e:
            raise ProviderError("Couldn't reach Cohere — check your connection.") from e
        if r.status_code != 200:
            raise ProviderError(_friendly(r.status_code, _detail(r.text)), r.status_code)
        out = []
        for m in r.json().get("models", []):
            if "chat" in (m.get("endpoints") or []) and m.get("name"):
                out.append(m["name"])
        return sorted(out)

    def _body(self, model, messages, temperature, max_tokens, stream) -> dict:
        body = {
            "model": model,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if stream:
            body["stream"] = True
        return body

    def complete(self, api_key, base_url, model, messages, *, temperature, max_tokens) -> str:
        try:
            r = httpx.post(f"{self._base(base_url)}/v2/chat", headers=_headers(api_key),
                           json=self._body(model, messages, temperature, max_tokens, False), timeout=120)
        except httpx.HTTPError as e:
            raise ProviderError("Couldn't reach Cohere — check your connection.") from e
        if r.status_code != 200:
            raise ProviderError(_friendly(r.status_code, _detail(r.text)), r.status_code)
        parts = r.json().get("message", {}).get("content", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text")

    def stream(self, api_key, base_url, model, messages, *, temperature, max_tokens) -> Iterator[str]:
        try:
            with httpx.stream("POST", f"{self._base(base_url)}/v2/chat", headers=_headers(api_key),
                              json=self._body(model, messages, temperature, max_tokens, True), timeout=120) as r:
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
                    if ev.get("type") == "content-delta":
                        txt = ev.get("delta", {}).get("message", {}).get("content", {}).get("text")
                        if txt:
                            yield txt
        except httpx.HTTPError as e:
            raise ProviderError("Couldn't reach Cohere — check your connection.") from e
