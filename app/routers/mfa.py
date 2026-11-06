"""Authenticator app (TOTP) enrolment, recovery codes and administrator reset (ADR-0007)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import audit, auth, mfa
from app.db import get_db
from app.models import MfaChallenge, MfaRecoveryCode, User, UserSession
from app.security import current_user, require

router = APIRouter(prefix="/api/v1", tags=["authenticator app"])


def _current_token(request: Request) -> str:
    return request.cookies.get(auth.COOKIE) or (request.headers.get("authorization") or "")[7:].strip()


def _revoke_other_sessions(db: Session, user: User, request: Request) -> None:
    q = update(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
    token = _current_token(request)
    if token:
        q = q.where(UserSession.token_hash != auth.token_hash(token))
    db.execute(q.values(revoked_at=auth.utcnow()))


def _upgrade_current_session(db: Session, request: Request) -> None:
    """After enrolling, the session in use has proven both factors."""
    token = _current_token(request)
    if token:
        db.execute(update(UserSession).where(UserSession.token_hash == auth.token_hash(token),
                                             UserSession.method == "password").values(method="password+totp"))


def _check_password(user: User, password: str) -> None:
    if not user.password_hash:
        raise HTTPException(409, {"code": "NO_PASSWORD", "message": "This account signs in with a company login; "
                                  "its provider applies multi-factor authentication"})
    if not auth.verify_password(password, user.password_hash):
        raise HTTPException(401, {"code": "INVALID_CREDENTIALS", "message": "Password is incorrect"})


def _check_code(db: Session, user: User, code: str, allow_recovery: bool = True) -> None:
    if allow_recovery and mfa.looks_like_recovery(code):
        if mfa.use_recovery_code(db, user, code):
            return
    else:
        step = mfa.verify(mfa.decrypt(user.totp_secret_encrypted), code, user.totp_last_step)
        if step is not None:
            user.totp_last_step = step
            return
    raise HTTPException(401, {"code": "INVALID_MFA_CODE", "message": "That code is not valid (or was already used)"})


def _drop(db: Session, user: User) -> None:
    """Remove the authenticator, its recovery codes and any half-finished sign-in step."""
    mfa.clear(user)
    for r in db.scalars(select(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id)):
        db.delete(r)
    db.execute(update(MfaChallenge).where(MfaChallenge.user_id == user.id, MfaChallenge.used_at.is_(None))
               .values(used_at=auth.utcnow()))


@router.get("/me/mfa")
def my_mfa(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"enabled": mfa.enabled(user), "enabled_at": user.totp_enabled_at.isoformat() if user.totp_enabled_at else None,
            "recovery_codes_left": mfa.recovery_left(db, user) if mfa.enabled(user) else 0,
            "required": mfa.required_for(db, user), "available": bool(user.password_hash), "policy": mfa.policy(db)}


class PasswordIn(BaseModel):
    password: str


@router.post("/me/mfa/totp/setup")
def setup(body: PasswordIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Step 1: re-check the password and issue a new secret (shown once, as a QR code and as text)."""
    _check_password(user, body.password)
    if mfa.enabled(user):
        raise HTTPException(409, {"code": "MFA_ALREADY_ENABLED", "message": "Turn the current authenticator off first"})
    secret = mfa.new_secret()
    user.totp_pending_encrypted = mfa.encrypt(secret)
    audit.record(db, user.id, "auth.mfa_setup_started", "User", user.id)
    db.commit()
    uri = mfa.otpauth_uri(secret, user.username)
    return {"secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)), "otpauth_uri": uri,
            "qr_svg": mfa.qr_svg(uri), "issuer": mfa.ISSUER, "account": user.username,
            "digits": mfa.DIGITS, "period_seconds": mfa.PERIOD}


class CodeIn(BaseModel):
    code: str


@router.post("/me/mfa/totp/confirm")
def confirm(body: CodeIn, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Step 2: prove the app was set up by entering its current code. Issues recovery codes (shown once)."""
    if not user.totp_pending_encrypted:
        raise HTTPException(409, {"code": "NO_PENDING_SETUP", "message": "Start the set-up again"})
    secret = mfa.decrypt(user.totp_pending_encrypted)
    step = mfa.verify(secret, body.code, None)
    if step is None:
        raise HTTPException(401, {"code": "INVALID_MFA_CODE", "message": "That code does not match; check the phone's clock and try again"})
    user.totp_secret_encrypted, user.totp_pending_encrypted = user.totp_pending_encrypted, None
    user.totp_enabled_at, user.totp_last_step = auth.utcnow(), step
    codes = mfa.replace_recovery_codes(db, user)
    _revoke_other_sessions(db, user, request)
    _upgrade_current_session(db, request)
    audit.record(db, user.id, "auth.mfa_enabled", "User", user.id)
    db.commit()
    return {"enabled": True, "recovery_codes": codes,
            "note": "Shown once. Keep them somewhere safe: each works once if the phone is lost. Other sessions were signed out."}


class DisableIn(BaseModel):
    password: str
    code: str


@router.post("/me/mfa/totp/disable")
def disable(body: DisableIn, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _check_password(user, body.password)
    if not mfa.enabled(user):
        raise HTTPException(409, {"code": "MFA_NOT_ENABLED", "message": "No authenticator is set up"})
    if mfa.required_for(db, user):
        raise HTTPException(409, {"code": "MFA_REQUIRED", "message": "Your organisation requires an authenticator app; "
                                  "set up a new one instead, or ask an administrator to reset it"})
    _check_code(db, user, body.code)
    _drop(db, user)
    audit.record(db, user.id, "auth.mfa_disabled", "User", user.id)
    db.commit()
    return {"enabled": False}


@router.post("/me/mfa/recovery-codes")
def regenerate(body: CodeIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not mfa.enabled(user):
        raise HTTPException(409, {"code": "MFA_NOT_ENABLED", "message": "No authenticator is set up"})
    _check_code(db, user, body.code, allow_recovery=False)
    codes = mfa.replace_recovery_codes(db, user)
    audit.record(db, user.id, "auth.mfa_recovery_codes_replaced", "User", user.id)
    db.commit()
    return {"recovery_codes": codes, "note": "Shown once. The previous recovery codes no longer work."}


@router.post("/users/{username}/reset-mfa")
def admin_reset(username: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """For a lost phone with no recovery codes. Signs the person out everywhere."""
    require(db, user, "user.admin")
    u = db.scalar(select(User).where(User.username == username))
    if not u:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "User not found"})
    if u.id == user.id:
        raise HTTPException(409, {"code": "SELF_RESET", "message": "Ask another administrator, use a recovery code, "
                                  "or run: python -m app.cli reset-mfa <username>"})
    was = mfa.enabled(u)
    _drop(db, u)
    db.execute(update(UserSession).where(UserSession.user_id == u.id, UserSession.revoked_at.is_(None))
               .values(revoked_at=auth.utcnow()))
    audit.record(db, user.id, "user.mfa_reset", "User", u.id, before={"mfa_enabled": was}, after={"mfa_enabled": False})
    db.commit()
    return {"username": username, "mfa_enabled": False,
            "note": "Authenticator removed and sessions signed out. If the policy requires one, they set it up at next sign-in."}
