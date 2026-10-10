"""Authorisation (blueprint Section 9).

MVP authentication: the caller identifies with an ``X-User`` header (username). This stands in
for the enterprise identity provider + MFA and must be replaced before any non-demo use.
Authorisation is real: every call checks the action permission, the project membership and the
document classification in the API/data layer - never only in the UI.
"""
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ProjectMembership, User

CLEARANCE_ORDER = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]

# action -> roles allowed to perform it
PERMISSIONS: dict[str, set[str]] = {
    "registry.write": {"registry_manager"},
    "registry.read": {"registry_manager", "project_coordinator", "contracts_manager", "commercial_manager",
                      "legal_reviewer", "auditor", "qaqc_manager", "hse_manager", "construction_manager"},
    "project.admin": {"project_coordinator", "system_admin"},
    "project.read": {"project_coordinator", "contracts_manager", "commercial_manager", "legal_reviewer",
                     "auditor", "qaqc_manager", "hse_manager", "construction_manager", "registry_manager",
                     "project_controls", "contracts_approver"},
    "document.upload": {"project_coordinator", "contracts_manager", "commercial_manager", "qaqc_manager",
                        "hse_manager", "construction_manager"},
    "document.review": {"contracts_manager", "legal_reviewer"},
    "subcontract.draft": {"contracts_manager"},
    "subcontract.review": {"contracts_manager", "legal_reviewer", "commercial_manager"},
    "subcontract.approve": {"contracts_approver"},
    "flowdown.decide": {"contracts_manager", "legal_reviewer"},
    "flowdown.exception": {"contracts_approver"},
    "obligation.manage": {"contracts_manager", "project_coordinator"},
    "evidence.submit": {"contracts_manager", "qaqc_manager", "hse_manager", "construction_manager",
                        "project_coordinator"},
    "evidence.review": {"contracts_manager", "qaqc_manager", "legal_reviewer"},
    "amendment.approve": {"contracts_approver"},
    "audit.read": {"auditor", "contracts_approver", "project_coordinator"},
    "user.admin": {"system_admin"},
}


def current_user(x_user: str | None = Header(default=None), db: Session = Depends(get_db)) -> User:
    if not x_user:
        raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "X-User header required"})
    user = db.scalar(select(User).where(User.username == x_user))
    if not user or not user.is_active:
        raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "Unknown or inactive user"})
    return user


def project_roles(db: Session, user: User, project_id: str) -> set[str] | None:
    m = db.scalar(select(ProjectMembership).where(ProjectMembership.user_id == user.id,
                                                  ProjectMembership.project_id == project_id))
    return None if m is None else set(m.roles) | set(user.roles)


def require(db: Session, user: User, action: str, project_id: str | None = None) -> None:
    allowed = PERMISSIONS[action]
    if project_id is None:
        if not (set(user.roles) & allowed):
            raise HTTPException(403, {"code": "FORBIDDEN", "message": f"Missing permission {action}"})
        return
    roles = project_roles(db, user, project_id)
    if roles is None:
        raise HTTPException(403, {"code": "NO_PROJECT_ACCESS", "message": "No access to this project"})
    if not (roles & allowed):
        raise HTTPException(403, {"code": "FORBIDDEN", "message": f"Missing permission {action}"})


def can_see_classification(user: User, classification: str) -> bool:
    return CLEARANCE_ORDER.index(classification) <= CLEARANCE_ORDER.index(user.clearance)


def accessible_project_ids(db: Session, user: User) -> list[str]:
    return list(db.scalars(select(ProjectMembership.project_id).where(ProjectMembership.user_id == user.id)))
