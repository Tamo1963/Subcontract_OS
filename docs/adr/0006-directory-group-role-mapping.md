# ADR-0006 — Directory group → role mapping

Status: **Accepted — built** · 2026-11-03 · extends ADR-0005 (pluggable sign-in providers)

## Decision
An administrator can map groups from the company directory (Entra ID, Google, Okta, Keycloak …) to Subcontract OS
roles, either **globally** or **on one project**. At each company sign-in the user's groups are read from the ID token
and their directory roles are brought up to date. Manually granted roles stay exactly as an administrator or
project coordinator set them.

## How it works
| Item | Behaviour |
|---|---|
| Where | Users & access → Sign-in providers → **Group roles** (per provider) |
| Mapping | Group value as the provider sends it (name, path such as `/EPC-Contracts`, or an Entra object ID) + description + scope (global or a project) + one or more roles |
| Matching | Case-insensitive; leading `/` ignored; claim name configurable (`groups`, or nested such as `realm_access.roles`) |
| Sync | On every company sign-in when **Sync roles from groups** is on: roles for matched groups are granted, roles for groups the user left are withdrawn |
| Separate storage | Directory roles live in `identity_directory_grants`, merged with manual roles only at the permission check — so a sync can never remove a manual role, and an admin removing a manual role never fights the directory |
| Effective immediately when off | A directory role stops counting at once when the provider is **disabled** or the mapping is **removed** (removal also deletes the grants) |
| Expiry | A directory role expires if not refreshed by a company sign-in within the provider's TTL (default 30 days; 0 = never) |
| Dry run | Enter a list of groups → see which roles and scopes result |
| Visibility | "directory" badges in Users & access and Project team; `/users/{name}/directory-roles` shows group, provider, scope and whether active |

## Safety rules
- **`system_admin` can never come from a directory** — the break-glass administrator stays a local account.
- If the token has **no groups claim, or an overage marker** (Entra ID sends `_claim_names` instead of groups when a
  user is in more than ~200 groups), nothing changes and the event is audited. A misconfigured provider can never
  silently strip access.
- **Segregation of duties still applies per action**: a directory role does not let a drafter approve their own
  subcontract, accept their own evidence or approve their own amendment.
- All mapping changes, sync results (roles added/removed, groups matched) and unavailable-claim events are audited.

## Provider notes
- **Microsoft Entra ID**: in the app registration, Token configuration → *Add groups claim* (Security groups).
  Entra sends **object IDs**; paste the ID as the group value and the group name as the description. For users in very
  many groups, use *Groups assigned to the application* to avoid overage.
- **Okta**: add a `groups` claim to the ID token (filter, e.g. starts with `EPC-`).
- **Keycloak**: add the *Group Membership* mapper (claim `groups`, full path on → values like `/EPC-Contracts`), or use
  `realm_access.roles` for realm roles.
- **Google Workspace**: Google's standard ID token carries no groups; use Keycloak or another broker in front of Google
  if group mapping is required.

## Tests
`tests/test_directory_groups.py` (11): admin rules and `system_admin` refusal, project-scoped and global grants,
withdrawal on leaving a group with manual roles untouched, missing/overage claim leaves roles unchanged, provider
disable / TTL expiry / mapping removal, sync off, case-insensitive and nested claims, dry run, segregation of duties,
admin visibility.
