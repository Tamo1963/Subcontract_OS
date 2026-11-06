"""Pluggable sign-in providers (OpenID Connect): login buttons, callback, linking, administration."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, config
from app.db import get_db
from app.idp import catalogue, groups, oidc
from app.llm import secrets as keystore
from app.models import (AppSetting, DirectoryRoleGrant, ExternalIdentity, GroupRoleMapping, IdentityProvider,
                        Project, User)
from app.routers.auth import issue_session, password_login_policy
from app.security import current_user, require

router = APIRouter(prefix="/api/v1", tags=["sign-in providers (OIDC)"])
BIND_COOKIE = "scos_oidc"


def prov_out(p: IdentityProvider, db: Session) -> dict:
    links = db.scalars(select(ExternalIdentity).where(ExternalIdentity.provider_id == p.id)).all()
    return {"id": p.id, "name": p.name, "provider_type": p.provider_type,
            "provider_label": catalogue.BY_KEY[p.provider_type][1], "issuer": p.issuer, "client_id": p.client_id,
            "secret_source": "env:" + p.client_secret_env if p.client_secret_env else
            ("stored (encrypted)" if p.client_secret_encrypted else "none (public client)"),
            "scopes": p.scopes, "button_label": p.button_label, "allowed_domains": p.allowed_domains,
            "auto_provision": p.auto_provision, "trust_unverified_email": p.trust_unverified_email,
            "enabled": p.enabled, "linked_accounts": len(links),
            "sync_roles": p.sync_roles, "groups_claim": p.groups_claim, "grant_ttl_days": p.grant_ttl_days,
            "group_mappings": db.scalar(select(func.count()).select_from(GroupRoleMapping)
                                        .where(GroupRoleMapping.provider_id == p.id)) or 0}


def _err_redirect(code: str) -> RedirectResponse:
    r = RedirectResponse(f"/#signin-error={code}", status_code=303)
    r.delete_cookie(BIND_COOKIE, path="/api/v1/auth/oidc")
    return r


# ---------------------------------------------------------------- public: login page and flow
@router.get("/auth/providers")
def login_options(db: Session = Depends(get_db)):
    ps = db.scalars(select(IdentityProvider).where(IdentityProvider.enabled.is_(True)).order_by(IdentityProvider.name))
    return {"providers": [{"id": p.id, "label": p.button_label, "type": p.provider_type} for p in ps],
            "password_login": password_login_policy(db)}



@router.get("/auth/oidc/{pid}/start")
def start_signin(pid: str, db: Session = Depends(get_db)):
    p = db.get(IdentityProvider, pid)
    if p is None:
        return _err_redirect("UNKNOWN_PROVIDER")
    try:
        url, binding = oidc.start(db, p)
    except oidc.OIDCError as exc:
        return _err_redirect(exc.code)
    r = RedirectResponse(url, status_code=303)
    # SameSite=Lax: it must come back on the provider's top-level redirect to our callback
    r.set_cookie(BIND_COOKIE, binding, httponly=True, samesite="lax", secure=config.COOKIE_SECURE,
                 max_age=oidc.STATE_TTL_S, path="/api/v1/auth/oidc")
    return r


@router.post("/auth/oidc/{pid}/link")
def start_link(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Signed-in user links a company login. POST + CSRF header, returns the URL to navigate to."""
    from fastapi.responses import JSONResponse
    p = db.get(IdentityProvider, pid)
    if p is None:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Provider not found"})
    try:
        url, binding = oidc.start(db, p, link_user_id=user.id)
    except oidc.OIDCError as exc:
        raise HTTPException(422, {"code": exc.code, "message": exc.message})
    r = JSONResponse({"authorization_url": url})
    r.set_cookie(BIND_COOKIE, binding, httponly=True, samesite="lax", secure=config.COOKIE_SECURE,
                 max_age=oidc.STATE_TTL_S, path="/api/v1/auth/oidc")
    return r


@router.get("/auth/oidc/callback")
def callback(request: Request, db: Session = Depends(get_db), code: str | None = Query(None),
             state: str | None = Query(None), error: str | None = Query(None)):
    if error or not code or not state:
        if state:
            from app.models import OIDCLoginState
            st = db.get(OIDCLoginState, state)
            if st:
                db.delete(st)
                db.commit()
        return _err_redirect("PROVIDER_" + (error or "NO_CODE").upper()[:40])
    try:
        user, outcome = oidc.complete(db, state, code, request.cookies.get(BIND_COOKIE))
    except oidc.OIDCError as exc:
        return _err_redirect(exc.code)
    r = RedirectResponse("/#linked" if outcome == "LINKED" else "/", status_code=303)
    r.delete_cookie(BIND_COOKIE, path="/api/v1/auth/oidc")
    if outcome != "LINKED":      # linking keeps the existing session
        issue_session(db, user, request, r, "sso")
    return r


# ---------------------------------------------------------------- self-service: my linked logins
@router.get("/me/identities")
def my_identities(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(select(ExternalIdentity, IdentityProvider).join(IdentityProvider)
                      .where(ExternalIdentity.user_id == user.id)).all()
    return [{"id": l.id, "provider": p.name, "provider_id": p.id, "email": l.email_at_link,
             "linked_at": l.linked_at.isoformat(), "last_used_at": l.last_used_at.isoformat() if l.last_used_at else None}
            for l, p in rows]


@router.delete("/me/identities/{lid}")
def unlink_mine(lid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    link = db.get(ExternalIdentity, lid)
    if not link or link.user_id != user.id:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Linked login not found"})
    others = db.scalars(select(ExternalIdentity).where(ExternalIdentity.user_id == user.id,
                                                       ExternalIdentity.id != lid)).all()
    if not user.password_hash and not others:
        raise HTTPException(409, {"code": "LAST_SIGN_IN_METHOD", "message": "This is your only way to sign in"})
    db.delete(link)
    audit.record(db, user.id, "auth.identity_unlinked", "User", user.id)
    db.commit()
    return {"unlinked": lid}


# ---------------------------------------------------------------- administration
class ProviderIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    provider_type: str
    issuer: str
    client_id: str = Field(min_length=1)
    client_secret: str | None = None          # write-only
    client_secret_env: str | None = None
    scopes: str = "openid email profile"
    button_label: str | None = None
    allowed_domains: list[str] = []
    auto_provision: bool = False
    trust_unverified_email: bool | None = None
    sync_roles: bool = False
    groups_claim: str = "groups"
    grant_ttl_days: int = Field(default=30, ge=0, le=365)


class ProviderPatch(BaseModel):
    name: str | None = None
    issuer: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    client_secret_env: str | None = None
    scopes: str | None = None
    button_label: str | None = None
    allowed_domains: list[str] | None = None
    auto_provision: bool | None = None
    trust_unverified_email: bool | None = None
    enabled: bool | None = None
    sync_roles: bool | None = None
    groups_claim: str | None = None
    grant_ttl_days: int | None = Field(default=None, ge=0, le=365)


def _apply_secret(p, secret, env):
    if secret:
        p.client_secret_encrypted, p.client_secret_env = keystore.encrypt(secret.strip()), None
    if env:
        p.client_secret_env, p.client_secret_encrypted = env.strip(), None


def _clean_domains(ds):
    return sorted({d.strip().lower().lstrip("@") for d in ds if d.strip()})


@router.get("/auth/identity-providers")
def admin_list(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    return {"providers": [prov_out(p, db) for p in db.scalars(select(IdentityProvider).order_by(IdentityProvider.name))],
            "catalogue": catalogue.as_dicts(), "redirect_uri": oidc.redirect_uri(),
            "password_login": password_login_policy(db), "mfa_required": _mfa_policy(db)}


def _mfa_policy(db):
    from app import mfa
    return mfa.policy(db)


@router.post("/auth/identity-providers", status_code=201)
def admin_create(body: ProviderIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    entry = catalogue.BY_KEY.get(body.provider_type)
    if not entry:
        raise HTTPException(422, {"code": "UNKNOWN_PROVIDER_TYPE", "message": "Choose a provider from the list"})
    try:
        oidc.check_url(body.issuer.strip())
    except oidc.OIDCError as exc:
        raise HTTPException(422, {"code": exc.code, "message": exc.message})
    if "<" in body.issuer:
        raise HTTPException(422, {"code": "TEMPLATE_NOT_FILLED", "message": "Replace the <placeholders> in the issuer URL"})
    if db.scalar(select(IdentityProvider).where(IdentityProvider.name == body.name)):
        raise HTTPException(409, {"code": "DUPLICATE_NAME", "message": "A provider with this name exists"})
    p = IdentityProvider(name=body.name, provider_type=body.provider_type, issuer=body.issuer.strip(),
                         client_id=body.client_id.strip(), scopes=body.scopes, button_label=body.button_label or entry[3],
                         allowed_domains=_clean_domains(body.allowed_domains), auto_provision=body.auto_provision,
                         trust_unverified_email=entry[4] if body.trust_unverified_email is None else body.trust_unverified_email,
                         sync_roles=body.sync_roles, groups_claim=body.groups_claim.strip() or "groups",
                         grant_ttl_days=body.grant_ttl_days, created_by=user.id)
    _apply_secret(p, body.client_secret, body.client_secret_env)
    db.add(p)
    db.flush()
    audit.record(db, user.id, "auth.provider_registered", "IdentityProvider", p.id, after=prov_out(p, db))
    db.commit()
    return prov_out(p, db)


@router.patch("/auth/identity-providers/{pid}")
def admin_update(pid: str, body: ProviderPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    p = db.get(IdentityProvider, pid)
    if not p:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Provider not found"})
    before = prov_out(p, db)
    data = body.model_dump(exclude_none=True)
    if "issuer" in data:
        try:
            oidc.check_url(data["issuer"])
        except oidc.OIDCError as exc:
            raise HTTPException(422, {"code": exc.code, "message": exc.message})
    for k in ("name", "issuer", "client_id", "scopes", "button_label", "auto_provision", "trust_unverified_email", "enabled",
              "sync_roles", "groups_claim", "grant_ttl_days"):
        if k in data:
            setattr(p, k, data[k])
    if "allowed_domains" in data:
        p.allowed_domains = _clean_domains(data["allowed_domains"])
    _apply_secret(p, body.client_secret, body.client_secret_env)
    audit.record(db, user.id, "auth.provider_updated", "IdentityProvider", p.id, before=before, after=prov_out(p, db))
    db.commit()
    oidc._discovery.pop(p.issuer, None)
    return prov_out(p, db)


@router.delete("/auth/identity-providers/{pid}")
def admin_delete(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    p = db.get(IdentityProvider, pid)
    if not p:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Provider not found"})
    for link in db.scalars(select(ExternalIdentity).where(ExternalIdentity.provider_id == pid)):
        db.delete(link)
    audit.record(db, user.id, "auth.provider_removed", "IdentityProvider", p.id, before=prov_out(p, db))
    db.delete(p)
    db.commit()
    return {"deleted": pid}


@router.post("/auth/identity-providers/{pid}/test")
def admin_test(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    p = db.get(IdentityProvider, pid)
    if not p:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Provider not found"})
    try:
        return {"ok": True, **oidc.test_provider(p)}
    except (oidc.OIDCError, Exception) as exc:     # noqa: BLE001 - report any connectivity problem
        return {"ok": False, "error": getattr(exc, "message", str(exc))[:300]}


class PolicyIn(BaseModel):
    password_login: str | None = None     # "all" | "admins_only"
    mfa_required: str | None = None       # "off" | "admins" | "all" - authenticator app for password sign-in


@router.put("/auth/policy")
def set_policy(body: PolicyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    from app import mfa
    if body.password_login is not None and body.password_login not in ("all", "admins_only"):
        raise HTTPException(422, {"code": "INVALID_POLICY", "message": "all or admins_only"})
    if body.mfa_required is not None and body.mfa_required not in mfa.POLICIES:
        raise HTTPException(422, {"code": "INVALID_POLICY", "message": "mfa_required: off, admins or all"})
    if body.password_login == "admins_only" and not db.scalars(
            select(IdentityProvider).where(IdentityProvider.enabled.is_(True))).first():
        raise HTTPException(409, {"code": "NO_PROVIDER", "message": "Enable a sign-in provider before turning off passwords"})
    s = db.get(AppSetting, "auth_policy")
    before = dict(s.value) if s else {}
    value = {"password_login": "all", "mfa_required": "off", **before,
             **{k: v for k, v in body.model_dump().items() if v is not None}}
    if s:
        s.value = value
    else:
        db.add(AppSetting(key="auth_policy", value=value))
    audit.record(db, user.id, "auth.policy_changed", "AppSetting", "auth_policy", before=before, after=value)
    db.commit()
    return value


@router.get("/users/{username}/identities")
def user_identities(username: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    u = db.scalar(select(User).where(User.username == username))
    if not u:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "User not found"})
    rows = db.execute(select(ExternalIdentity, IdentityProvider).join(IdentityProvider)
                      .where(ExternalIdentity.user_id == u.id)).all()
    return [{"id": l.id, "provider": p.name, "email": l.email_at_link, "linked_at": l.linked_at.isoformat()}
            for l, p in rows]


@router.delete("/users/{username}/identities/{lid}")
def admin_unlink(username: str, lid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    link = db.get(ExternalIdentity, lid)
    u = db.scalar(select(User).where(User.username == username))
    if not link or not u or link.user_id != u.id:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Linked login not found"})
    db.delete(link)
    audit.record(db, user.id, "auth.identity_unlinked_by_admin", "User", u.id)
    db.commit()
    return {"unlinked": lid}


# ---------------------------------------------------------------- directory group -> role mapping
class MappingIn(BaseModel):
    group_value: str = Field(min_length=1, max_length=300)
    label: str = ""
    project_id: str | None = None        # None = global roles
    roles: list[str] = Field(min_length=1)


def mapping_out(db: Session, m: GroupRoleMapping) -> dict:
    proj = db.get(Project, m.project_id) if m.project_id else None
    holders = db.scalar(select(func.count(func.distinct(DirectoryRoleGrant.user_id)))
                        .where(DirectoryRoleGrant.mapping_id == m.id)) or 0
    return {"id": m.id, "group_value": m.group_value, "label": m.label, "project_id": m.project_id,
            "project_code": proj.project_code if proj else None, "scope": proj.project_code if proj else "global",
            "roles": m.roles, "users_holding": holders}


def _provider(db, pid) -> IdentityProvider:
    p = db.get(IdentityProvider, pid)
    if not p:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Provider not found"})
    return p


@router.get("/auth/identity-providers/{pid}/group-mappings")
def list_mappings(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    p = _provider(db, pid)
    ms = db.scalars(select(GroupRoleMapping).where(GroupRoleMapping.provider_id == pid)
                    .order_by(GroupRoleMapping.label, GroupRoleMapping.group_value))
    from app.routers.auth import ALL_ROLES
    return {"provider": p.name, "sync_roles": p.sync_roles, "groups_claim": p.groups_claim,
            "grant_ttl_days": p.grant_ttl_days, "mappings": [mapping_out(db, m) for m in ms],
            "assignable_roles": [r for r in ALL_ROLES if r not in groups.FORBIDDEN_FROM_DIRECTORY],
            "projects": [{"id": x.id, "code": x.project_code, "name": x.name} for x in db.scalars(select(Project))]}


@router.post("/auth/identity-providers/{pid}/group-mappings", status_code=201)
def create_mapping(pid: str, body: MappingIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    p = _provider(db, pid)
    from app.routers.auth import ALL_ROLES
    bad = [r for r in body.roles if r not in ALL_ROLES]
    if bad:
        raise HTTPException(422, {"code": "UNKNOWN_ROLE", "message": f"Unknown roles: {bad}"})
    if set(body.roles) & groups.FORBIDDEN_FROM_DIRECTORY:
        raise HTTPException(422, {"code": "ROLE_NOT_DELEGABLE",
                                  "message": "system_admin cannot come from a directory group; keep administrators local"})
    if body.project_id and not db.get(Project, body.project_id):
        raise HTTPException(422, {"code": "UNKNOWN_PROJECT", "message": "Project not found"})
    dup = [m for m in db.scalars(select(GroupRoleMapping).where(GroupRoleMapping.provider_id == pid,
                                                                GroupRoleMapping.project_id.is_(body.project_id) if body.project_id is None
                                                                else GroupRoleMapping.project_id == body.project_id))
           if groups.norm(m.group_value) == groups.norm(body.group_value)]
    if dup:
        raise HTTPException(409, {"code": "DUPLICATE_MAPPING",
                                  "message": "This group already has a mapping for that scope; edit or remove it"})
    m = GroupRoleMapping(provider_id=pid, group_value=body.group_value.strip(), label=body.label.strip(),
                         project_id=body.project_id, roles=sorted(set(body.roles)), created_by=user.id)
    db.add(m)
    db.flush()
    audit.record(db, user.id, "auth.group_mapping_created", "IdentityProvider", p.id, after=mapping_out(db, m))
    db.commit()
    return mapping_out(db, m)


@router.delete("/auth/group-mappings/{mid}")
def delete_mapping(mid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Removing a mapping withdraws the roles it granted immediately."""
    require(db, user, "user.admin")
    m = db.get(GroupRoleMapping, mid)
    if not m:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "Mapping not found"})
    before = mapping_out(db, m)
    revoked = db.query(DirectoryRoleGrant).filter(DirectoryRoleGrant.mapping_id == mid).delete()
    audit.record(db, user.id, "auth.group_mapping_removed", "IdentityProvider", m.provider_id,
                 before=before, after={"grants_withdrawn": revoked})
    db.delete(m)
    db.commit()
    return {"deleted": mid, "grants_withdrawn": revoked}


class PreviewIn(BaseModel):
    groups: list[str]


@router.post("/auth/identity-providers/{pid}/group-mappings/preview")
def preview_mapping(pid: str, body: PreviewIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Dry run: which roles would a person in these groups receive?"""
    require(db, user, "user.admin")
    return {"result": groups.preview(db, _provider(db, pid), body.groups)}


@router.get("/users/{username}/directory-roles")
def user_directory_roles(username: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "user.admin")
    u = db.scalar(select(User).where(User.username == username))
    if not u:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "User not found"})
    active = {g.id for g in groups.active_grants(db, u)}
    rows = db.execute(select(DirectoryRoleGrant, GroupRoleMapping, IdentityProvider)
                      .join(GroupRoleMapping, DirectoryRoleGrant.mapping_id == GroupRoleMapping.id)
                      .join(IdentityProvider, DirectoryRoleGrant.provider_id == IdentityProvider.id)
                      .where(DirectoryRoleGrant.user_id == u.id)).all()
    codes = {p.id: p.project_code for p in db.scalars(select(Project))}
    return [{"role": g.role, "scope": codes.get(g.project_id, "global") if g.project_id else "global",
             "group": m.label or m.group_value, "provider": p.name, "active": g.id in active,
             "refreshed_at": g.refreshed_at.isoformat()} for g, m, p in rows]
