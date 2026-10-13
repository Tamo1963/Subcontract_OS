"""OpenID Connect sign-in, provider-agnostic (authorization-code flow with PKCE).

Checks applied on every sign-in:
- one-time ``state`` (10-minute life) bound to the browser that started the flow (login-CSRF protection);
- PKCE S256 code verifier; ``nonce`` echoed in the ID token (replay protection);
- ID-token signature against the provider's published JWKS, asymmetric algorithms only (never ``none``/HS*);
- ``iss`` equals the discovered issuer, ``aud`` contains our client ID (``azp`` when several audiences),
  ``exp``/``iat`` with 60 s leeway;
- issuer and endpoints over HTTPS (plain HTTP only for localhost test providers).
Account resolution: linked (provider, subject) → that user; otherwise an invited user whose e-mail matches a
VERIFIED e-mail in an allowed domain is linked once; otherwise, only if the provider allows it, a new account
with no roles. Roles and project access always stay in the OS.
"""
import base64
import hashlib
import secrets
import time
from datetime import timedelta
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, auth, config
from app.idp.catalogue import BY_KEY
from app.llm import secrets as keystore
from app.models import ExternalIdentity, IdentityProvider, OIDCLoginState, User

TRANSPORT: httpx.BaseTransport | None = None      # tests inject httpx.MockTransport
ALLOWED_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
STATE_TTL_S = 600
CACHE_TTL_S = 3600
_discovery: dict[str, tuple[float, dict]] = {}
_jwks: dict[str, tuple[float, dict]] = {}


class OIDCError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def redirect_uri() -> str:
    return config.PUBLIC_BASE_URL.rstrip("/") + "/api/v1/auth/oidc/callback"


def check_url(url: str, what: str = "Issuer") -> None:
    u = urlparse(url)
    local = u.hostname in ("localhost", "127.0.0.1", "::1")
    if not u.netloc or u.scheme not in ("https", "http") or (u.scheme == "http" and not local):
        raise OIDCError("INSECURE_ENDPOINT", f"{what} must use https (http is allowed only for localhost test providers)")


def _http() -> httpx.Client:
    return httpx.Client(timeout=15, transport=TRANSPORT, follow_redirects=False)


def discovery(issuer: str, force: bool = False) -> dict:
    hit = _discovery.get(issuer)
    if hit and not force and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    check_url(issuer)
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        with _http() as c:
            r = c.get(url)
        r.raise_for_status()
        doc = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OIDCError("DISCOVERY_FAILED", f"Could not read {url}: {str(exc)[:150]}") from exc
    if doc.get("issuer", "").rstrip("/") != issuer.rstrip("/"):
        raise OIDCError("ISSUER_MISMATCH", f"Provider reports issuer {doc.get('issuer')!r}, configured {issuer!r}")
    for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not doc.get(k):
            raise OIDCError("DISCOVERY_INCOMPLETE", f"Discovery document lacks {k}")
        check_url(doc[k], k)
    _discovery[issuer] = (time.time(), doc)
    return doc


def _signing_key(jwks_uri: str, kid: str | None):
    for attempt in (0, 1):   # refresh once on an unknown key id (provider key rotation)
        hit = _jwks.get(jwks_uri)
        if attempt or not hit or time.time() - hit[0] > CACHE_TTL_S:
            try:
                with _http() as c:
                    r = c.get(jwks_uri)
                r.raise_for_status()
                _jwks[jwks_uri] = hit = (time.time(), r.json())
            except (httpx.HTTPError, ValueError) as exc:
                raise OIDCError("JWKS_FAILED", f"Could not read signing keys: {str(exc)[:150]}") from exc
        keys = [k for k in hit[1].get("keys", []) if kid is None or k.get("kid") == kid]
        if keys:
            return jwt.PyJWK(keys[0])
    raise OIDCError("UNKNOWN_SIGNING_KEY", "ID token is signed with a key the provider does not publish")


def client_secret(p: IdentityProvider) -> str | None:
    import os
    if p.client_secret_env:
        return os.environ.get(p.client_secret_env)
    return keystore.decrypt(p.client_secret_encrypted) if p.client_secret_encrypted else None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def start(db: Session, p: IdentityProvider, link_user_id: str | None = None) -> tuple[str, str]:
    """Return (authorization URL, browser-binding token for a short-lived cookie)."""
    if not p.enabled:
        raise OIDCError("PROVIDER_DISABLED", "This sign-in provider is disabled")
    disc = discovery(p.issuer)
    state, nonce, verifier, binding = (secrets.token_urlsafe(32), secrets.token_urlsafe(32),
                                       secrets.token_urlsafe(64), secrets.token_urlsafe(32))
    cutoff = auth.utcnow() - timedelta(seconds=STATE_TTL_S)
    for old in db.scalars(select(OIDCLoginState).where(OIDCLoginState.created_at < cutoff)):
        db.delete(old)
    db.add(OIDCLoginState(state=state, provider_id=p.id, nonce=nonce, code_verifier=verifier,
                          browser_binding=hashlib.sha256(binding.encode()).hexdigest(), link_user_id=link_user_id))
    db.commit()
    params = {"response_type": "code", "client_id": p.client_id, "redirect_uri": redirect_uri(),
              "scope": p.scopes or "openid email profile", "state": state, "nonce": nonce,
              "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    sep = "&" if "?" in disc["authorization_endpoint"] else "?"
    return disc["authorization_endpoint"] + sep + urlencode(params), binding


def validate_id_token(p: IdentityProvider, disc: dict, id_token: str, nonce: str) -> dict:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OIDCError("INVALID_TOKEN", "ID token is malformed") from exc
    if header.get("alg") not in ALLOWED_ALGS:
        raise OIDCError("INVALID_TOKEN", f"ID token algorithm {header.get('alg')!r} is not accepted")
    key = _signing_key(disc["jwks_uri"], header.get("kid"))
    try:
        claims = jwt.decode(id_token, key=key.key, algorithms=[header["alg"]], audience=p.client_id,
                            issuer=disc["issuer"], leeway=60, options={"require": ["exp", "iat", "sub", "aud", "iss"]})
    except jwt.PyJWTError as exc:
        raise OIDCError("INVALID_TOKEN", f"ID token rejected: {exc}") from exc
    aud = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if len(aud) > 1 and claims.get("azp") != p.client_id:
        raise OIDCError("INVALID_TOKEN", "ID token has several audiences and azp is not this application")
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OIDCError("INVALID_TOKEN", "ID token nonce does not match this sign-in")
    return claims


def _verified_email(p: IdentityProvider, claims: dict) -> str | None:
    email = (claims.get("email") or (claims.get("preferred_username") if p.provider_type == "entra" else None) or "")
    email = email.strip().lower()
    if "@" not in email:
        return None
    if claims.get("email_verified") is True or p.trust_unverified_email:
        return email
    return None


def _domain_allowed(p: IdentityProvider, email: str | None) -> bool:
    if not email:
        return False
    domains = [d.lower().lstrip("@") for d in (p.allowed_domains or [])]
    return not domains or email.rsplit("@", 1)[1] in domains


def complete(db: Session, state: str, code: str, binding: str | None) -> tuple[User, str]:
    """Finish a sign-in or a link. Returns (user, outcome)."""
    st = db.get(OIDCLoginState, state or "")
    if st is None:
        raise OIDCError("INVALID_STATE", "Sign-in session not recognised or already used; start again")
    db.delete(st)       # one-time, whatever happens next
    db.commit()
    if auth.aware(st.created_at) < auth.utcnow() - timedelta(seconds=STATE_TTL_S):
        raise OIDCError("STATE_EXPIRED", "Sign-in took too long; start again")
    if not binding or not secrets.compare_digest(hashlib.sha256(binding.encode()).hexdigest(), st.browser_binding):
        raise OIDCError("BROWSER_MISMATCH", "Sign-in was started in a different browser; start again")
    p = db.get(IdentityProvider, st.provider_id)
    if p is None or not p.enabled:
        raise OIDCError("PROVIDER_DISABLED", "This sign-in provider is disabled")
    disc = discovery(p.issuer)
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
            "code_verifier": st.code_verifier, "client_id": p.client_id}
    secret = client_secret(p)
    methods = disc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    basic = None
    if secret and "client_secret_basic" in methods:
        basic = (p.client_id, secret)
    elif secret:
        form["client_secret"] = secret
    try:
        with _http() as c:
            r = c.post(disc["token_endpoint"], data=form, auth=basic, headers={"Accept": "application/json"})
        tok = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OIDCError("TOKEN_EXCHANGE_FAILED", f"Token endpoint unreachable: {str(exc)[:150]}") from exc
    if r.status_code >= 400 or "id_token" not in tok:
        raise OIDCError("TOKEN_EXCHANGE_FAILED", f"Provider refused the code: {tok.get('error', r.status_code)}")
    claims = validate_id_token(p, disc, tok["id_token"], st.nonce)
    subject = str(claims["sub"])
    email = _verified_email(p, claims)
    link = db.scalar(select(ExternalIdentity).where(ExternalIdentity.provider_id == p.id,
                                                    ExternalIdentity.subject == subject))

    if st.link_user_id:                                  # signed-in user linking a company login
        user = db.get(User, st.link_user_id)
        if link and link.user_id != user.id:
            raise OIDCError("ALREADY_LINKED", "That company login is linked to another account")
        if not link:
            if db.scalar(select(ExternalIdentity).where(ExternalIdentity.provider_id == p.id,
                                                        ExternalIdentity.user_id == user.id)):
                raise OIDCError("ALREADY_LINKED", "Your account already has a login from this provider; unlink it first")
            db.add(ExternalIdentity(provider_id=p.id, subject=subject, user_id=user.id, email_at_link=email))
            audit.record(db, user.id, "auth.identity_linked", "User", user.id, after={"provider": p.name})
        return _finish(db, user, p, subject, "LINKED")

    if link:
        return _finish(db, db.get(User, link.user_id), p, subject, "SIGNED_IN")
    if email and _domain_allowed(p, email):
        invited = db.scalar(select(User).where(User.email == email))
        if invited:
            if db.scalar(select(ExternalIdentity).where(ExternalIdentity.provider_id == p.id,
                                                        ExternalIdentity.user_id == invited.id)):
                raise OIDCError("ALREADY_LINKED", "This account is linked to a different login from this provider")
            db.add(ExternalIdentity(provider_id=p.id, subject=subject, user_id=invited.id, email_at_link=email))
            audit.record(db, invited.id, "auth.identity_linked_by_invitation", "User", invited.id,
                         after={"provider": p.name, "email": email})
            return _finish(db, invited, p, subject, "LINKED_BY_INVITATION")
        if p.auto_provision:
            base = email.split("@")[0][:60]
            username = base
            while db.scalar(select(User).where(User.username == username)):
                username = f"{base}.{secrets.token_hex(2)}"
            user = User(username=username, full_name=str(claims.get("name") or email), email=email, roles=[],
                        clearance="INTERNAL", password_hash=None, must_change_password=False)
            db.add(user)
            db.flush()
            db.add(ExternalIdentity(provider_id=p.id, subject=subject, user_id=user.id, email_at_link=email))
            audit.record(db, user.id, "user.auto_provisioned", "User", user.id, after={"provider": p.name, "email": email})
            return _finish(db, user, p, subject, "PROVISIONED")
    audit.record(db, "anonymous", "auth.sso_no_account", "IdentityProvider", p.id,
                 after={"email": email, "verified_email": bool(email)})
    db.commit()
    raise OIDCError("NO_ACCOUNT", "No Subcontract OS account is set up for this login. Ask an administrator to invite you.")


def _finish(db: Session, user: User | None, p: IdentityProvider, subject: str, outcome: str) -> tuple[User, str]:
    if user is None or not user.is_active:
        raise OIDCError("ACCOUNT_DISABLED", "This account is disabled")
    link = db.scalar(select(ExternalIdentity).where(ExternalIdentity.provider_id == p.id,
                                                    ExternalIdentity.subject == subject))
    if link:
        link.last_used_at = auth.utcnow()
    db.commit()
    return user, outcome


def test_provider(p: IdentityProvider) -> dict:
    """Admin connection test: discovery, issuer match, JWKS reachable, PKCE support."""
    disc = discovery(p.issuer, force=True)
    _jwks.pop(disc["jwks_uri"], None)
    with _http() as c:
        r = c.get(disc["jwks_uri"])
    n_keys = len(r.json().get("keys", [])) if r.status_code < 400 else 0
    if not n_keys:
        raise OIDCError("JWKS_FAILED", "Provider publishes no signing keys")
    return {"issuer": disc["issuer"], "authorization_endpoint": disc["authorization_endpoint"],
            "signing_keys": n_keys, "pkce_s256": "S256" in (disc.get("code_challenge_methods_supported") or ["S256"]),
            "redirect_uri_to_register": redirect_uri()}


def _provider_type_ok(key: str) -> bool:
    return key in BY_KEY
