"""Authorisation (blueprint Section 9).

Authentication is session-based (see app/auth.py). Authorisation is enforced on every call: action
permission, project membership, document classification and segregation of duties are checked in the
API/data layer - never only in the UI.
"""
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, config
from app.db import get_db
from app.models import ProjectMembership, User, UserSession

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PASSWORD_CHANGE_ALLOWED = ("/api/v1/auth/", "/api/v1/me")

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


def _session_user(db: Session, token: str) -> User | None:
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == auth.token_hash(token)))
    if not sess or sess.revoked_at or auth.aware(sess.expires_at) <= auth.utcnow():
        return None
    return db.get(User, sess.user_id)


def current_user(request: Request, db: Session = Depends(get_db), authorization: str | None = Header(default=None),
                 x_user: str | None = Header(default=None)) -> User:
    user, via_cookie = None, False
    if authorization and authorization.lower().startswith("bearer "):
        user = _session_user(db, authorization[7:].strip())
    elif request.cookies.get(auth.COOKIE):
        user, via_cookie = _session_user(db, request.cookies[auth.COOKIE]), True
    elif x_user and config.ALLOW_DEV_AUTH:     # development/test only; disabled by default
        user = db.scalar(select(User).where(User.username == x_user))
    if not user or not user.is_active:
        raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "Sign in required"})
    if via_cookie and request.method not in SAFE_METHODS and request.headers.get("X-SCOS-CSRF") != "1":
        raise HTTPException(403, {"code": "CSRF", "message": "Missing anti-forgery header"})
    if user.must_change_password and user.password_hash and not request.url.path.startswith(PASSWORD_CHANGE_ALLOWED):
        raise HTTPException(403, {"code": "PASSWORD_CHANGE_REQUIRED", "message": "Change your temporary password first"})
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
