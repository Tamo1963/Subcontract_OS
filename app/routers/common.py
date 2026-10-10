from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Subcontract, User
from app.security import require


def get_or_404(db: Session, model, obj_id: str, label: str):
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": f"{label} not found"})
    return obj


def load_subcontract(db: Session, user: User, sub_id: str, action: str) -> Subcontract:
    sub = get_or_404(db, Subcontract, sub_id, "Subcontract")
    require(db, user, action, sub.project_id)
    return sub


def sub_out(s: Subcontract) -> dict:
    return {"id": s.id, "project_id": s.project_id, "main_contract_id": s.main_contract_id,
            "subcontractor_id": s.subcontractor_id, "subcontract_number": s.subcontract_number,
            "scope_summary": s.scope_summary, "discipline": s.discipline, "currency": s.currency,
            "contract_value": str(s.contract_value), "status": s.status, "created_by": s.created_by,
            "executed_at": s.executed_at.isoformat() if s.executed_at else None, "baseline": s.baseline,
            "release_exception": s.release_exception}
