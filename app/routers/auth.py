"""Sign-in, sign-out, password change and user administration."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import audit, auth, config
from app.db import get_db
from app.models import User, UserSession
from app.security import CLEARANCE_ORDER, PERMISSIONS, current_user, require

router = APIRouter(prefix="/api/v1", tags=["authentication & users"])
ALL_ROLES = sorted({r for roles in PERMISSIONS.values() for r in roles})


class LoginIn(BaseModel):
    username: str
    password: str
    issue_bearer_token: bool = False    # for API integrations; browsers use the cookie


def _set_cookie(resp: Response, token: str):
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="strict", secure=config.COOKIE_SECURE,
                    max_age=config.SESSION_HOURS * 3600, path="/")


@router.get("/auth/config")
def auth_config():
    return {"dev_auth": config.ALLOW_DEV_AUTH, "session_hours": config.SESSION_HOURS}


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    fail = HTTPException(401, {"code": "INVALID_CREDENTIALS", "message": "Invalid username or password"})
    user = db.scalar(select(User).where(User.username == body.username.strip()))
    if user is None:
        auth.verify_password(body.password, auth.hash_password("x"))   # equalise timing
        raise fail
    if user.locked_until and auth.aware(user.locked_until) > auth.utcnow():
        audit.record(db, user.id, "auth.login_blocked_locked", "User", user.id)
        db.commit()
        raise HTTPException(423, {"code": "ACCOUNT_LOCKED", "message": "Account temporarily locked; try later or ask an administrator"})
    if not user.is_active or not auth.verify_password(body.password, user.password_hash):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= config.MAX_FAILED_LOGINS:
            user.locked_until = auth.utcnow() + timedelta(minutes=config.LOCKOUT_MINUTES)
            user.failed_logins = 0
            audit.record(db, user.id, "auth.account_locked", "User", user.id)
        audit.record(db, user.id, "auth.login_failed", "User", user.id)
        db.commit()
        raise fail
    token, th, expires = auth.new_session_token()
    db.add(UserSession(token_hash=th, user_id=user.id, expires_at=expires,
                       client=(request.headers.get("user-agent") or "")[:300]))
    user.failed_logins, user.locked_until, user.last_login_at = 0, None, auth.utcnow()
    audit.record(db, user.id, "auth.login", "User", user.id)
    db.commit()
    _set_cookie(response, token)
    out = {"username": user.username, "full_name": user.full_name, "must_change_password": user.must_change_password,
           "expires_at": expires.isoformat()}
    if body.issue_bearer_token:
        out["bearer_token"] = token
    return out


@router.post("/auth/logout")
def logout(request: Request, response: Response, user: User = Depends(current_user), db: Session = Depends(get_db)):
    token = request.cookies.get(auth.COOKIE) or (request.headers.get("authorization") or "")[7:].strip()
    if token:
        db.execute(update(UserSession).where(UserSession.token_hash == auth.token_hash(token))
                   .values(revoked_at=auth.utcnow()))
    audit.record(db, user.id, "auth.logout", "User", user.id)
    db.commit()
    response.delete_cookie(auth.COOKIE, path="/")
    return {"signed_out": True}


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


@router.post("/auth/change-password")
def change_password(body: ChangePasswordIn, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    if user.password_hash and not auth.verify_password(body.current_password, user.password_hash):
        raise HTTPException(401, {"code": "INVALID_CREDENTIALS", "message": "Current password is incorrect"})
    auth.check_policy(body.new_password, user.username)
    if body.new_password == body.current_password:
        raise HTTPException(422, {"code": "WEAK_PASSWORD", "message": "Choose a password different from the current one"})
    user.password_hash, user.must_change_password = auth.hash_password(body.new_password), False
    user.password_changed_at = auth.utcnow()
    # Revoke every other session of this user; keep the current one.
    current = request.cookies.get(auth.COOKIE) or (request.headers.get("authorization") or "")[7:].strip()
    q = update(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
    if current:
        q = q.where(UserSession.token_hash != auth.token_hash(current))
    db.execute(q.values(revoked_at=auth.utcnow()))
    audit.record(db, user.id, "auth.password_changed", "User", user.id)
    db.commit()
    return {"changed": True}


# ---------------------------------------------------------------- user administration
def user_out(u: User) -> dict:
    locked = bool(u.locked_until and auth.aware(u.locked_until) > auth.utcnow())
    return {"username": u.username, "full_name": u.full_name, "roles": u.roles, "clearance": u.clearance,
            "is_active": u.is_active, "must_change_password": u.must_change_password, "locked": locked,
            "has_password": bool(u.password_hash),
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}


@router.get("/users")
def list_users(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    return {"users": [user_out(u) for u in db.scalars(select(User).order_by(User.username))],
            "roles": ALL_ROLES, "clearances": CLEARANCE_ORDER}


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9._-]+$")
    full_name: str = Field(min_length=2)
    roles: list[str] = []
    clearance: str = "CONFIDENTIAL"


def _validate(roles, clearance):
    bad = [r for r in roles if r not in ALL_ROLES]
    if bad:
        raise HTTPException(422, {"code": "UNKNOWN_ROLE", "message": f"Unknown roles: {bad}"})
    if clearance not in CLEARANCE_ORDER:
        raise HTTPException(422, {"code": "UNKNOWN_CLEARANCE", "message": f"Use one of {CLEARANCE_ORDER}"})


@router.post("/users", status_code=201)
def create_user(body: UserIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    _validate(body.roles, body.clearance)
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, {"code": "DUPLICATE_USERNAME", "message": "Username already exists"})
    temp = auth.temporary_password()
    u = User(username=body.username, full_name=body.full_name, roles=body.roles, clearance=body.clearance,
             password_hash=auth.hash_password(temp), must_change_password=True)
    db.add(u)
    db.flush()
    audit.record(db, user.id, "user.created", "User", u.id, after=user_out(u))
    db.commit()
    return {**user_out(u), "temporary_password": temp,
            "note": "Shown once. Give it to the user securely; they must change it at first sign-in."}


@router.post("/users/{username}/reset-password")
def reset_password(username: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    u = db.scalar(select(User).where(User.username == username))
    if not u:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "User not found"})
    temp = auth.temporary_password()
    u.password_hash, u.must_change_password, u.failed_logins, u.locked_until = auth.hash_password(temp), True, 0, None
    db.execute(update(UserSession).where(UserSession.user_id == u.id, UserSession.revoked_at.is_(None))
               .values(revoked_at=auth.utcnow()))
    audit.record(db, user.id, "user.password_reset", "User", u.id)
    db.commit()
    return {"username": username, "temporary_password": temp, "note": "Shown once; all sessions revoked."}


class UserPatch(BaseModel):
    full_name: str | None = None
    roles: list[str] | None = None
    clearance: str | None = None
    is_active: bool | None = None
    unlock: bool = False


@router.patch("/users/{username}")
def update_user(username: str, body: UserPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    u = db.scalar(select(User).where(User.username == username))
    if not u:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "User not found"})
    if u.id == user.id and (body.is_active is False or (body.roles is not None and "system_admin" not in body.roles)):
        raise HTTPException(409, {"code": "SELF_LOCKOUT", "message": "You cannot remove your own admin access"})
    _validate(body.roles or [], body.clearance or u.clearance)
    before = user_out(u)
    for k in ("full_name", "roles", "clearance", "is_active"):
        v = getattr(body, k)
        if v is not None:
            setattr(u, k, v)
    if body.unlock:
        u.locked_until, u.failed_logins = None, 0
    if body.is_active is False:
        db.execute(update(UserSession).where(UserSession.user_id == u.id, UserSession.revoked_at.is_(None))
                   .values(revoked_at=auth.utcnow()))
    audit.record(db, user.id, "user.updated", "User", u.id, before=before, after=user_out(u))
    db.commit()
    return user_out(u)


@router.get("/users/audit")
def user_audit(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    from app.models import AuditEvent
    names = {u.id: u.username for u in db.scalars(select(User))}
    rows = db.scalars(select(AuditEvent).where(AuditEvent.action.like("auth.%") | AuditEvent.action.like("user.%"))
                      .order_by(AuditEvent.id.desc()).limit(100))
    return [{"at": e.occurred_at.isoformat(), "actor": names.get(e.actor_id, e.actor_id), "action": e.action,
             "subject": names.get(e.object_id, e.object_id)} for e in rows]
