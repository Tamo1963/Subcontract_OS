"""Authentication (replaces the X-User development stub).

- Passwords: PBKDF2-HMAC-SHA256 (stdlib), per-user salt, configurable work factor.
- Temporary passwords: issued by an administrator or the bootstrap command; the user must change
  them at first login before any other API call is allowed.
- Lockout after repeated failures; generic error messages (no username enumeration).
- Sessions: random 256-bit token in an HttpOnly, SameSite=Strict cookie; only its SHA-256 is stored
  server-side, so sessions are revocable and expire. API clients may request a bearer token.
- CSRF: cookie-authenticated state changes must carry the ``X-SCOS-CSRF`` header, which a
  cross-site form cannot send.
This module is the seam for SSO/OIDC + MFA: an identity-provider login would create the same session.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app import config

ITERATIONS = int(os.environ.get("SCOS_PBKDF2_ITERATIONS", "600000"))
COOKIE = "scos_session"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat stored values as UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iters, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iters))
    return hmac.compare_digest(dk, base64.b64decode(digest))


def temporary_password() -> str:
    """Readable, policy-compliant one-time password, e.g. Temp-k7Qm-x2Rw-9."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    part = lambda n: "".join(secrets.choice(alphabet) for _ in range(n))  # noqa: E731
    return f"Temp-{part(4)}-{part(4)}-{secrets.randbelow(10)}"


def check_policy(password: str, username: str) -> None:
    problems = []
    if len(password) < 12:
        problems.append("at least 12 characters")
    if not re.search(r"[a-z]", password) or not re.search(r"[A-Z]", password):
        problems.append("upper- and lower-case letters")
    if not re.search(r"\d", password):
        problems.append("a digit")
    if username.lower() in password.lower():
        problems.append("must not contain the username")
    if password.lower().startswith("temp-"):
        problems.append("must not reuse the temporary-password pattern")
    if problems:
        raise HTTPException(422, {"code": "WEAK_PASSWORD", "message": "Password needs: " + ", ".join(problems)})


def new_session_token() -> tuple[str, str, datetime]:
    token = secrets.token_urlsafe(32)
    return token, token_hash(token), utcnow() + timedelta(hours=config.SESSION_HOURS)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
