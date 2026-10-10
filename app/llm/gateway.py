"""LLM gateway: one call signature, five wire protocols, policy and logging in one place.

Business code calls ``complete(db, provider, purpose, system, user)`` and never sees a vendor SDK.
Swapping models is a configuration change. Outputs are treated as untrusted proposals by callers.
"""
import json
import os
import re
import time
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from app.llm import secrets
from app.llm.catalogue import BY_KEY
from app.models import AppSetting, LLMCall, LLMProvider

TRANSPORT: httpx.BaseTransport | None = None   # tests inject httpx.MockTransport here
CLEARANCE = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]
CLOUD_MAX_CLASSIFICATION = os.environ.get("SCOS_LLM_CLOUD_MAX_CLASSIFICATION", "CONFIDENTIAL")


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    text: str
    latency_ms: int


def protocol(p: LLMProvider) -> str:
    return BY_KEY[p.provider_type][2]


def api_key(p: LLMProvider) -> str | None:
    if p.api_key_env:
        return os.environ.get(p.api_key_env)
    if p.api_key_encrypted:
        return secrets.decrypt(p.api_key_encrypted)
    return None


def allowed_for(p: LLMProvider, classification: str) -> bool:
    """Hybrid-hosting rule: content above the cloud ceiling only goes to PRIVATE-zone endpoints."""
    if p.data_zone == "PRIVATE":
        return True
    return CLEARANCE.index(classification) <= CLEARANCE.index(CLOUD_MAX_CLASSIFICATION)


def _request(p: LLMProvider, system: str, user: str, max_tokens: int):
    base, key, proto = p.base_url.rstrip("/"), api_key(p), protocol(p)
    headers = {"Content-Type": "application/json", **(p.extra or {}).get("headers", {})}
    temp = float(p.temperature or 0)
    if proto == "openai":
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return f"{base}/chat/completions", headers, {
            "model": p.model, "temperature": temp, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if proto == "azure_openai":
        if key:
            headers["api-key"] = key
        ver = (p.extra or {}).get("api_version", "2024-06-01")
        return f"{base}/openai/deployments/{p.model}/chat/completions?api-version={ver}", headers, {
            "temperature": temp, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if proto == "anthropic":
        if key:
            headers["x-api-key"] = key
        headers.setdefault("anthropic-version", (p.extra or {}).get("anthropic_version", "2023-06-01"))
        return f"{base}/v1/messages", headers, {
            "model": p.model, "system": system, "max_tokens": max_tokens, "temperature": temp,
            "messages": [{"role": "user", "content": user}]}
    if proto == "gemini":
        if key:
            headers["x-goog-api-key"] = key
        return f"{base}/models/{p.model}:generateContent", headers, {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temp, "maxOutputTokens": max_tokens}}
    if proto == "ollama":
        return f"{base}/api/chat", headers, {
            "model": p.model, "stream": False, "options": {"temperature": temp},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    raise LLMError(f"Unknown protocol {proto}")


def _parse(proto: str, data: dict) -> str:
    try:
        if proto in ("openai", "azure_openai"):
            return data["choices"][0]["message"]["content"] or ""
        if proto == "anthropic":
            return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        if proto == "gemini":
            return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
        if proto == "ollama":
            return data["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Unexpected response shape from {proto} endpoint") from exc
    raise LLMError(proto)


def complete(db: Session, p: LLMProvider, purpose: str, system: str, user: str,
             max_tokens: int | None = None) -> LLMResult:
    if not p.enabled:
        raise LLMError("Provider is disabled")
    url, headers, body = _request(p, system, user, max_tokens or p.max_tokens)
    t0 = time.monotonic()
    log = LLMCall(provider_id=p.id, provider_name=p.name, model=p.model, purpose=purpose, ok=False,
                  latency_ms=0, prompt_chars=len(system) + len(user))
    try:
        with httpx.Client(timeout=p.timeout_s, transport=TRANSPORT) as client:
            r = client.post(url, headers=headers, json=body)
        if r.status_code >= 400:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
        text = _parse(protocol(p), r.json())
        log.ok, log.response_chars, log.outcome = True, len(text), "ok"
        return LLMResult(text, int((time.monotonic() - t0) * 1000))
    except (httpx.HTTPError, ValueError, LLMError) as exc:
        log.outcome = str(exc)[:300]
        raise LLMError(str(exc)) from exc
    finally:
        log.latency_ms = int((time.monotonic() - t0) * 1000)
        from app.db import SessionLocal   # separate session: logging never commits the caller's work
        with SessionLocal() as s:
            s.add(log)
            s.commit()


def assigned_provider(db: Session, purpose: str) -> LLMProvider | None:
    s = db.get(AppSetting, "llm_assignments")
    pid = (s.value if s else {}).get(purpose)
    p = db.get(LLMProvider, pid) if pid else None
    return p if p and p.enabled else None


def extract_json(text: str) -> dict:
    """Models wrap JSON in prose or fences; take the first balanced object."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError("No JSON object in model output")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError("Model output is not valid JSON") from exc
