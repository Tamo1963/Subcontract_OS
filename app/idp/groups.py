"""Directory group → role mapping.

At each company sign-in (when the provider has ``sync_roles`` on) the user's groups are read from the ID
token and compared with the administrator's mappings. Roles from matching mappings are granted; roles from
groups the user has left are withdrawn. Manually granted roles are never touched: directory roles live in
their own table and are merged with manual roles only when permissions are checked.

Safety rules
- ``system_admin`` can never come from a directory (the break-glass administrator stays local).
- If the token carries no groups claim, or signals an overage (too many groups to embed - Entra ID), nothing
  is changed and the event is audited, so a provider misconfiguration never silently strips access.
- A directory role stops counting when its provider is disabled, its mapping is removed, or it has not been
  refreshed by a sign-in within the provider's ``grant_ttl_days``.
- Segregation-of-duty checks still apply per action, whatever roles the directory grants.
"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, auth
from app.models import DirectoryRoleGrant, GroupRoleMapping, IdentityProvider, Project, User

FORBIDDEN_FROM_DIRECTORY = {"system_admin"}


def norm(group: str) -> str:
    return str(group).strip().lstrip("/").casefold()


def extract_groups(claims: dict, claim_path: str) -> list[str] | None:
    """Groups from the ID token, or None when the claim is missing or truncated (overage)."""
    names = claims.get("_claim_names") or {}
    if "groups" in names or claims.get("hasgroups"):        # Entra ID overage signal
        return None
    value = claims.get(claim_path)
    if value is None and "." in claim_path:                  # nested claim, e.g. Keycloak realm_access.roles
        value = claims
        for part in claim_path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
    if value is None:
        return None
    if isinstance(value, str):
        value = [v for v in value.replace(",", " ").split() if v]
    if not isinstance(value, list):
        return None
    return [str(v) for v in value]


def desired(db: Session, provider: IdentityProvider, groups: list[str]) -> list[tuple[GroupRoleMapping, str]]:
    mine = {norm(g) for g in groups}
    out = []
    for m in db.scalars(select(GroupRoleMapping).where(GroupRoleMapping.provider_id == provider.id)):
        if norm(m.group_value) in mine:
            out.extend((m, r) for r in m.roles if r not in FORBIDDEN_FROM_DIRECTORY)
    return out


def sync(db: Session, user: User, provider: IdentityProvider, claims: dict) -> dict:
    """Bring the user's directory roles for this provider in line with their current groups."""
    if not provider.sync_roles:
        return {"synced": False}
    groups = extract_groups(claims, provider.groups_claim or "groups")
    if groups is None:
        audit.record(db, user.id, "auth.directory_groups_unavailable", "User", user.id,
                     after={"provider": provider.name, "claim": provider.groups_claim,
                            "note": "No groups claim (or overage) in the token; directory roles left unchanged"})
        return {"synced": False, "reason": "GROUPS_CLAIM_MISSING"}
    want = {(m.id, r): m for m, r in desired(db, provider, groups)}
    have = {(g.mapping_id, g.role): g for g in db.scalars(select(DirectoryRoleGrant).where(
        DirectoryRoleGrant.user_id == user.id, DirectoryRoleGrant.provider_id == provider.id))}
    now = auth.utcnow()
    added, removed = [], []
    for key, m in want.items():
        if key in have:
            have[key].refreshed_at = now
        else:
            db.add(DirectoryRoleGrant(user_id=user.id, provider_id=provider.id, mapping_id=m.id,
                                      project_id=m.project_id, role=key[1], granted_at=now, refreshed_at=now))
            added.append({"role": key[1], "project_id": m.project_id, "group": m.label or m.group_value})
    for key, g in have.items():
        if key not in want:
            removed.append({"role": g.role, "project_id": g.project_id})
            db.delete(g)
    if added or removed:
        audit.record(db, user.id, "auth.directory_roles_synced", "User", user.id,
                     after={"provider": provider.name, "added": added, "removed": removed,
                            "groups_matched": sorted({m.label or m.group_value for m in want.values()})})
    return {"synced": True, "added": added, "removed": removed}


def active_grants(db: Session, user: User) -> list[DirectoryRoleGrant]:
    rows = db.execute(select(DirectoryRoleGrant, IdentityProvider)
                      .join(IdentityProvider, DirectoryRoleGrant.provider_id == IdentityProvider.id)
                      .where(DirectoryRoleGrant.user_id == user.id, IdentityProvider.enabled.is_(True))).all()
    now = auth.utcnow()
    return [g for g, p in rows
            if not p.grant_ttl_days or auth.aware(g.refreshed_at) >= now - timedelta(days=p.grant_ttl_days)]


def preview(db: Session, provider: IdentityProvider, groups: list[str]) -> list[dict]:
    names = {p.id: p.project_code for p in db.scalars(select(Project))}
    return [{"group": m.label or m.group_value, "role": r, "scope": names.get(m.project_id, "all projects")
             if m.project_id else "global"} for m, r in desired(db, provider, groups)]
