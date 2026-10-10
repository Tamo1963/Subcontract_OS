"""AI provider administration: any LLM, chosen by endpoint, assigned per purpose (ADR-0002)."""
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.db import get_db
from app.llm import catalogue, gateway, secrets
from app.models import AppSetting, LLMCall, LLMProvider, User
from app.security import current_user, require

router = APIRouter(prefix="/api/v1/llm", tags=["AI providers (LLM-agnostic)"])


def out(p: LLMProvider, assignments: dict) -> dict:
    return {"id": p.id, "name": p.name, "provider_type": p.provider_type,
            "provider_label": catalogue.BY_KEY[p.provider_type][1], "protocol": gateway.protocol(p),
            "base_url": p.base_url, "model": p.model, "data_zone": p.data_zone,
            "temperature": float(p.temperature or 0), "max_tokens": p.max_tokens, "timeout_s": p.timeout_s,
            "extra": {k: v for k, v in (p.extra or {}).items() if k != "headers"},
            "extra_header_names": sorted((p.extra or {}).get("headers", {}).keys()),
            "enabled": p.enabled, "key_source": "env:" + p.api_key_env if p.api_key_env else
            ("stored (encrypted)" if p.api_key_encrypted else "none"), "key_hint": p.key_hint,
            "assigned_to": [k for k, v in assignments.items() if v == p.id]}


def _assignments(db) -> dict:
    s = db.get(AppSetting, "llm_assignments")
    return dict(s.value) if s else {}


def _check_url(url: str):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise HTTPException(422, {"code": "INVALID_ENDPOINT", "message": "Endpoint must be an http(s) URL"})


class ProviderIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    provider_type: str
    base_url: str | None = None
    model: str = Field(min_length=1)
    api_key: str | None = None            # write-only
    api_key_env: str | None = None
    data_zone: str | None = None
    temperature: float = 0.0
    max_tokens: int = 800
    timeout_s: int = 60
    extra: dict = {}


class ProviderPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    clear_key: bool = False
    data_zone: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_s: int | None = None
    extra: dict | None = None
    enabled: bool | None = None


@router.get("/catalogue")
def get_catalogue(user: User = Depends(current_user)):
    return {"providers": catalogue.as_dicts(), "purposes": catalogue.PURPOSES,
            "cloud_max_classification": gateway.CLOUD_MAX_CLASSIFICATION}


@router.get("/providers")
def list_providers(user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = _assignments(db)
    return [out(p, a) for p in db.scalars(select(LLMProvider).order_by(LLMProvider.name))]


def _apply_key(p: LLMProvider, api_key: str | None, api_key_env: str | None):
    if api_key:
        p.api_key_encrypted, p.key_hint, p.api_key_env = secrets.encrypt(api_key.strip()), secrets.hint(api_key.strip()), None
    if api_key_env:
        p.api_key_env, p.api_key_encrypted, p.key_hint = api_key_env.strip(), None, None


@router.post("/providers", status_code=201)
def create_provider(body: ProviderIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    entry = catalogue.BY_KEY.get(body.provider_type)
    if not entry:
        raise HTTPException(422, {"code": "UNKNOWN_PROVIDER_TYPE", "message": "Choose a provider from the catalogue"})
    base = (body.base_url or entry[3]).strip()
    _check_url(base)
    zone = (body.data_zone or entry[4]).upper()
    if zone not in ("PRIVATE", "CLOUD"):
        raise HTTPException(422, {"code": "INVALID_ZONE", "message": "PRIVATE or CLOUD"})
    if db.scalar(select(LLMProvider).where(LLMProvider.name == body.name)):
        raise HTTPException(409, {"code": "DUPLICATE_NAME", "message": "A provider with this name exists"})
    p = LLMProvider(name=body.name, provider_type=body.provider_type, base_url=base, model=body.model.strip(),
                    data_zone=zone, temperature=body.temperature, max_tokens=body.max_tokens,
                    timeout_s=body.timeout_s, extra=body.extra, created_by=user.id)
    _apply_key(p, body.api_key, body.api_key_env)
    db.add(p)
    db.flush()
    audit.record(db, user.id, "llm.provider_registered", "LLMProvider", p.id,
                 after={k: v for k, v in out(p, {}).items() if k not in ("key_hint",)})
    db.commit()
    return out(p, _assignments(db))


@router.patch("/providers/{pid}")
def update_provider(pid: str, body: ProviderPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    p = db.get(LLMProvider, pid)
    if not p:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Provider not found"})
    before = out(p, {})
    data = body.model_dump(exclude_none=True)
    if "base_url" in data:
        _check_url(data["base_url"])
    if "data_zone" in data and data["data_zone"].upper() not in ("PRIVATE", "CLOUD"):
        raise HTTPException(422, {"code": "INVALID_ZONE", "message": "PRIVATE or CLOUD"})
    for k in ("name", "base_url", "model", "temperature", "max_tokens", "timeout_s", "extra", "enabled"):
        if k in data:
            setattr(p, k, data[k])
    if "data_zone" in data:
        p.data_zone = data["data_zone"].upper()
    if body.clear_key:
        p.api_key_encrypted = p.key_hint = p.api_key_env = None
    _apply_key(p, body.api_key, body.api_key_env)
    audit.record(db, user.id, "llm.provider_updated", "LLMProvider", p.id, before=before, after=out(p, {}))
    db.commit()
    return out(p, _assignments(db))


@router.delete("/providers/{pid}")
def delete_provider(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    p = db.get(LLMProvider, pid)
    if not p:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Provider not found"})
    a = _assignments(db)
    if p.id in a.values():
        raise HTTPException(409, {"code": "IN_USE", "message": "Unassign the provider first"})
    audit.record(db, user.id, "llm.provider_removed", "LLMProvider", p.id, before=out(p, {}))
    db.delete(p)
    db.commit()
    return {"deleted": pid}


@router.post("/providers/{pid}/test")
def test_provider(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    p = db.get(LLMProvider, pid)
    if not p:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Provider not found"})
    try:
        r = gateway.complete(db, p, "connection_test", "You are a connectivity check.",
                             'Reply with the JSON {"status": "ok"} and nothing else.', max_tokens=20)
        return {"ok": True, "latency_ms": r.latency_ms, "sample": r.text[:200]}
    except gateway.LLMError as exc:
        return {"ok": False, "error": str(exc)[:300]}


class AssignmentsIn(BaseModel):
    flowdown: str | None = None
    knowledge: str | None = None


@router.get("/assignments")
def get_assignments(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {k: _assignments(db).get(k) for k in catalogue.PURPOSES}


@router.put("/assignments")
def set_assignments(body: AssignmentsIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    new = body.model_dump()
    for purpose, pid in new.items():
        if pid and not db.get(LLMProvider, pid):
            raise HTTPException(422, {"code": "UNKNOWN_PROVIDER", "message": f"{purpose}: provider not found"})
    before = _assignments(db)
    s = db.get(AppSetting, "llm_assignments")
    if s:
        s.value = new
    else:
        db.add(AppSetting(key="llm_assignments", value=new))
    audit.record(db, user.id, "llm.assignments_changed", "AppSetting", "llm_assignments", before=before, after=new)
    db.commit()
    return new


@router.get("/calls")
def calls(limit: int = 100, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    return [{"at": c.at.isoformat(), "provider": c.provider_name, "model": c.model, "purpose": c.purpose,
             "ok": c.ok, "latency_ms": c.latency_ms, "prompt_chars": c.prompt_chars,
             "response_chars": c.response_chars, "outcome": c.outcome}
            for c in db.scalars(select(LLMCall).order_by(LLMCall.id.desc()).limit(min(limit, 500)))]


@router.get("/audit")
def llm_audit(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    from app.models import AuditEvent
    users = {u.id: u.username for u in db.scalars(select(User))}
    rows = db.scalars(select(AuditEvent).where(AuditEvent.object_type.in_(["LLMProvider", "AppSetting"]))
                      .order_by(AuditEvent.id.desc()).limit(100))
    return [{"at": e.occurred_at.isoformat(), "actor": users.get(e.actor_id, e.actor_id), "action": e.action,
             "after": e.after} for e in rows]
