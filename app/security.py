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


def _session(db: Session, token: str) -> tuple[User | None, UserSession | None]:
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == auth.token_hash(token)))
    if not sess or sess.revoked_at or auth.aware(sess.expires_at) <= auth.utcnow():
        return None, None
    return db.get(User, sess.user_id), sess


def current_user(request: Request, db: Session = Depends(get_db), authorization: str | None = Header(default=None),
                 x_user: str | None = Header(default=None)) -> User:
    user, sess, via_cookie = None, None, False
    if authorization and authorization.lower().startswith("bearer "):
        user, sess = _session(db, authorization[7:].strip())
    elif request.cookies.get(auth.COOKIE):
        (user, sess), via_cookie = _session(db, request.cookies[auth.COOKIE]), True
    elif x_user and config.ALLOW_DEV_AUTH:     # development/test only; disabled by default
        user = db.scalar(select(User).where(User.username == x_user))
    if not user or not user.is_active:
        raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "Sign in required"})
    if via_cookie and request.method not in SAFE_METHODS and request.headers.get("X-SCOS-CSRF") != "1":
        raise HTTPException(403, {"code": "CSRF", "message": "Missing anti-forgery header"})
    if user.must_change_password and user.password_hash and not request.url.path.startswith(PASSWORD_CHANGE_ALLOWED):
        raise HTTPException(403, {"code": "PASSWORD_CHANGE_REQUIRED", "message": "Change your temporary password first"})
    if sess is not None and mfa_enrolment_due(db, user, sess) and not request.url.path.startswith(PASSWORD_CHANGE_ALLOWED):
        raise HTTPException(403, {"code": "MFA_ENROLMENT_REQUIRED", "message": "Set up your authenticator app first"})
    request.state.session_method = sess.method if sess is not None else None
    return user


def mfa_enrolment_due(db: Session, user: User, sess: UserSession | None) -> bool:
    """A password session of someone the policy requires to use an authenticator, who has not set one up."""
    from app import mfa
    return bool(sess is not None and sess.method == "password" and user.password_hash
                and not mfa.enabled(user) and mfa.required_for(db, user))


def global_roles(db: Session, user: User) -> set[str]:
    """Manually granted global roles plus active global roles from directory groups."""
    from app.idp.groups import active_grants
    return set(user.roles or []) | {g.role for g in active_grants(db, user) if g.project_id is None}


def project_roles(db: Session, user: User, project_id: str) -> set[str] | None:
    """Effective roles on a project, or None when the user has no access to it at all."""
    from app.idp.groups import active_grants
    m = db.scalar(select(ProjectMembership).where(ProjectMembership.user_id == user.id,
                                                  ProjectMembership.project_id == project_id))
    grants = active_grants(db, user)
    directory = {g.role for g in grants if g.project_id == project_id}
    if m is None and not directory:
        return None
    return set(m.roles if m else []) | directory | set(user.roles or []) | {g.role for g in grants if g.project_id is None}


def require(db: Session, user: User, action: str, project_id: str | None = None) -> None:
    allowed = PERMISSIONS[action]
    if project_id is None:
        if not (global_roles(db, user) & allowed):
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
    from app.idp.groups import active_grants
    manual = set(db.scalars(select(ProjectMembership.project_id).where(ProjectMembership.user_id == user.id)))
    return sorted(manual | {g.project_id for g in active_grants(db, user) if g.project_id})
