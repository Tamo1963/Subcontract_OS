"""Append-only audit ledger and idempotency helpers."""
import hashlib
import json
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import AuditEvent, IdempotencyRecord


def _clean(v):
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    if isinstance(v, Decimal):
        return str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def record(db: Session, actor_id: str, action: str, obj_type: str, obj_id: str, *, project_id=None,
           before=None, after=None, reason=None) -> None:
    db.add(AuditEvent(actor_id=actor_id, action=action, object_type=obj_type, object_id=obj_id,
                      project_id=project_id, before=_clean(before), after=_clean(after), reason=reason))


def snapshot(obj, fields) -> dict:
    return _clean({f: getattr(obj, f) for f in fields})


def request_hash(payload) -> str:
    return hashlib.sha256(json.dumps(_clean(payload), sort_keys=True).encode()).hexdigest()


def idempotent_lookup(db: Session, key: str | None, actor_id: str, payload) -> dict | None:
    """Return the stored response for a replayed key; reject reuse with a different payload."""
    if not key:
        return None
    rec = db.get(IdempotencyRecord, key)
    if rec is None:
        return None
    if rec.request_hash != request_hash(payload) or rec.actor_id != actor_id:
        raise HTTPException(422, {"code": "IDEMPOTENCY_KEY_REUSED",
                                  "message": "Idempotency key already used with a different request"})
    return rec.response


def idempotent_store(db: Session, key: str | None, actor_id: str, payload, response: dict) -> None:
    if key:
        db.add(IdempotencyRecord(key=key, actor_id=actor_id, request_hash=request_hash(payload),
                                 response=_clean(response)))
