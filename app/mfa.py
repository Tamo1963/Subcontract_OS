"""Authenticator-app codes (TOTP, RFC 6238) for username + password sign-in (ADR-0007).

- Standard 6-digit, 30-second, HMAC-SHA1 codes: works with Microsoft Authenticator, Google
  Authenticator, Authy, 1Password, Bitwarden and any RFC 6238 app.
- The shared secret is encrypted at rest with the same key as AI-provider keys (app/llm/secrets.py).
- A code is accepted within +/- one 30-second step (clock drift) and never twice (replay protection:
  the last accepted step is stored and later codes must be newer).
- Ten single-use recovery codes are issued at enrolment; only their SHA-256 is stored.
- Company (OpenID Connect) sign-in is not affected: the company's provider applies its own MFA.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm import secrets as keystore

DIGITS, PERIOD, WINDOW = 6, 30, 1
ISSUER = "Subcontract OS"
RECOVERY_CODES = 10
CHALLENGE_MINUTES = 5
CHALLENGE_ATTEMPTS = 5
POLICIES = ("off", "admins", "all")      # who must use an authenticator app with password sign-in


# ---------------------------------------------------------------- RFC 6238
def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.upper().replace(" ", "")
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // PERIOD)


def verify(secret: str, code: str, last_step: int | None, now: float | None = None) -> int | None:
    """Return the matched time step, or None. Steps at or before ``last_step`` are refused (replay)."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    step = current_step(now)
    for s in range(step - WINDOW, step + WINDOW + 1):
        if last_step is not None and s <= last_step:
            continue
        if hmac.compare_digest(code_at(secret, s), code):
            return s
    return None


def otpauth_uri(secret: str, username: str) -> str:
    label = quote(f"{ISSUER}:{username}")
    return f"otpauth://totp/{label}?" + urlencode({"secret": secret, "issuer": ISSUER, "algorithm": "SHA1",
                                                   "digits": DIGITS, "period": PERIOD})


def qr_svg(uri: str) -> str | None:
    try:
        import segno
    except ImportError:          # the secret can still be typed in by hand
        return None
    return segno.make(uri, error="m").svg_inline(scale=5, dark="#111827", light="#ffffff")


def encrypt(secret: str) -> str:
    return keystore.encrypt(secret)


def decrypt(token: str) -> str:
    return keystore.decrypt(token)


# ---------------------------------------------------------------- recovery codes
def _normalise_recovery(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def recovery_hash(code: str) -> str:
    return hashlib.sha256(("scos-recovery:" + _normalise_recovery(code)).encode()).hexdigest()


def new_recovery_codes() -> list[str]:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O, 1/I
    part = lambda: "".join(secrets.choice(alphabet) for _ in range(5))  # noqa: E731
    return [f"{part()}-{part()}" for _ in range(RECOVERY_CODES)]       # 50 random bits each


def looks_like_recovery(code: str) -> bool:
    return len(_normalise_recovery(code)) == 10 and not code.strip().isdigit()


def replace_recovery_codes(db: Session, user) -> list[str]:
    from app.models import MfaRecoveryCode
    for old in db.scalars(select(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id)):
        db.delete(old)
    codes = new_recovery_codes()
    for c in codes:
        db.add(MfaRecoveryCode(user_id=user.id, code_hash=recovery_hash(c)))
    return codes


def use_recovery_code(db: Session, user, code: str) -> bool:
    from app.auth import utcnow
    from app.models import MfaRecoveryCode
    row = db.scalar(select(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id,
                                                  MfaRecoveryCode.code_hash == recovery_hash(code),
                                                  MfaRecoveryCode.used_at.is_(None)))
    if not row:
        return False
    row.used_at = utcnow()
    return True


def recovery_left(db: Session, user) -> int:
    from app.models import MfaRecoveryCode
    return len(list(db.scalars(select(MfaRecoveryCode.id).where(MfaRecoveryCode.user_id == user.id,
                                                                MfaRecoveryCode.used_at.is_(None)))))


# ---------------------------------------------------------------- policy
def policy(db: Session) -> str:
    from app.models import AppSetting
    s = db.get(AppSetting, "auth_policy")
    return (s.value if s else {}).get("mfa_required", "off")


def required_for(db: Session, user) -> bool:
    p = policy(db)
    return p == "all" or (p == "admins" and "system_admin" in (user.roles or []))


def enabled(user) -> bool:
    return bool(user.totp_enabled_at and user.totp_secret_encrypted)


def clear(user) -> None:
    user.totp_secret_encrypted = user.totp_pending_encrypted = None
    user.totp_enabled_at = user.totp_last_step = None
