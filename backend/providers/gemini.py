"""Google Gemini (native) adapter — Generative Language API over httpx.

Note: Gemini also works through the OpenAI-compatible adapter (base_url
".../v1beta/openai/"); this native adapter is for users who paste a plain Google
AI Studio key and want the models listed for them.
"""
from __future__ import annotations

import json
from typing import Iterator

import httpx

from .base import ProviderError, split_system

_BASE = "https://generativelanguage.googleapis.com"


def _friendly(status: int, detail: str = "") -> str:
    if status == 429:
        return "Gemini is rate-limited right now — wait a moment and retry."
    if status in (401, 403):
        return "Your Google API key was rejected. Re-check it in Settings → Custom AI model."
    if status == 404:
        return "That Gemini model isn't available. Pick another model."
    if status == 400:
        return "Google rejected the request (unsupported model or parameters)."
    return f"Gemini returned an error ({status})." + (f" {detail}" if detail else "")


def _detail(body: str) -> str:
    try:
        return json.loads(body).get("error", {}).get("message", "")
    except Exception:
        return ""


def _contents(messages: list[dict]) -> tuple[dict | None, list[dict]]:
    system, rest = split_system(messages)
    contents = [{"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]}]} for m in rest]
    sys_instr = {"parts": [{"text": system}]} if system else None
    return sys_instr, contents


class GeminiAdapter:
    key = "gemini"
    label = "Google Gemini"
    needs_base_url = False
    default_base_url = None

    def _base(self, base_url: str | None) -> str:
        return (base_url or _BASE).rstrip("/")

    def list_models(self, api_key: str, base_url: str | None) -> list[str]:
        try:
            r = httpx.get(f"{self._base(base_url)}/v1beta/models",
                          params={"key": api_key, "pageSize": 200}, timeout=20)
        except httpx.HTTPError as e:
            raise ProviderError("Couldn't reach Google — check your connection.") from e
        if r.status_code != 200:
            raise ProviderError(_friendly(r.status_code, _detail(r.text)), r.status_code)
        out = []
        for m in r.json().get("models", []):
            if "generateContent" in (m.get("supportedGenerationMethods") or []):
                out.append((m.get("name") or "").removeprefix("models/"))
        return sorted(x for x in out if x)

    def _body(self, messages, temperature, max_tokens) -> dict:
        sys_instr, contents = _contents(messages)
        body: dict = {"contents": contents,
                      "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
        if sys_instr:
            body["systemInstruction"] = sys_instr
        return body

    def complete(self, api_key, base_url, model, messages, *, temperature, max_tokens) -> str:
        url = f"{self._base(base_url)}/v1beta/models/{model}:generateContent"
        try:
            r = httpx.post(url, params={"key": api_key},
                           json=self._body(messages, temperature, max_tokens), timeout=120)
        except httpx.HTTPError as e:
            raise ProviderError("Couldn't reach Google — check your connection.") from e
        if r.status_code != 200:
            raise ProviderError(_friendly(r.status_code, _detail(r.text)), r.status_code)
        cands = r.json().get("candidates", [])
        if not cands:
            return ""
        parts = cands[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)

    def stream(self, api_key, base_url, model, messages, *, temperature, max_tokens) -> Iterator[str]:
        url = f"{self._base(base_url)}/v1beta/models/{model}:streamGenerateContent"
        try:
            with httpx.stream("POST", url, params={"key": api_key, "alt": "sse"},
                              json=self._body(messages, temperature, max_tokens), timeout=120) as r:
                if r.status_code != 200:
                    body = r.read().decode("utf-8", "ignore")
                    raise ProviderError(_friendly(r.status_code, _detail(body)), r.status_code)
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    try:
                        ev = json.loads(data)
                    except Exception:
                        continue
                    for cand in ev.get("candidates", []):
                        for p in cand.get("content", {}).get("parts", []):
                            if p.get("text"):
                                yield p["text"]
        except httpx.HTTPError as e:
            raise ProviderError("Couldn't reach Google — check your connection.") from e
