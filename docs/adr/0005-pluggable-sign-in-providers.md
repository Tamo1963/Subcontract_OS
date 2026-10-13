# ADR-0005 — Pluggable sign-in providers (OpenID Connect)

Status: **Accepted — framework built (phase 1)** · 2026-10-13 · design: Claude Doc "Pluggable Sign-in Providers"

## Decision
Sign-in stays flexible: username + password works as before, and any OpenID Connect provider can be added later
by an administrator **without code changes**. Accounts, roles and project access always live in Subcontract OS;
a provider only proves who the person is.

## How to add a provider (when IT is ready)
1. IT creates an app registration and gives you: **issuer URL, client ID, client secret**.
2. Register this redirect URI with them: `<SCOS_PUBLIC_BASE_URL>/api/v1/auth/oidc/callback`
   (set `SCOS_PUBLIC_BASE_URL` in `.env` to the address users open, e.g. `https://scos.yourcompany.com`).
3. Users & access → **Sign-in providers** → choose the provider from the dropdown, fill the fields, **Test**.
4. Invite people by setting their **work e-mail** on their account; at first company sign-in they are linked.

Dropdown: Microsoft Entra ID, Google Workspace, Okta, Auth0, Keycloak (also the bridge for SAML/LDAP/AD), Zoho,
any other OIDC provider.

## Security controls
| Control | Implementation |
|---|---|
| Flow | Authorization code + PKCE S256; no implicit flow |
| Login CSRF / replay | One-time `state` (10 min) bound to the starting browser by an HttpOnly cookie; `nonce` checked in the ID token |
| ID token | Signature verified against the provider's JWKS (asymmetric algorithms only; `none`/HS* refused; key rotation handled); `iss`, `aud`, `azp`, `exp`, `iat` with 60 s leeway |
| Transport | Issuer and endpoints must be HTTPS (HTTP only for localhost test providers); issuer must match discovery |
| Account matching | By the provider's permanent subject ID. E-mail is used once, to link an invited user, and only if verified and in an allowed domain |
| New people | Invite-only by default; optional auto-create for allowed domains, with **no roles** until granted |
| One identity, one account | Unique (provider, subject) and (provider, user); linking a login that belongs to someone else is refused |
| Secrets | Client secret encrypted at rest or read from an environment variable; never returned by the API |
| Break-glass | Password sign-in can be limited to administrators; it cannot be turned off without an enabled provider; a user cannot unlink their only sign-in method |
| Sessions | SSO ends in the same server-side session as password sign-in (expiry, revocation, CSRF header) |
| Audit | Provider changes, policy changes, links, invitation matches, auto-creation, refused sign-ins |

## Testing
`seed/mock_oidc.py` is a local OIDC provider (discovery, JWKS, PKCE, RS256). `tests/test_sso.py` (23 tests)
covers invitations, linking, auto-creation, domain rules, unverified e-mail, browser binding, replay,
and seven tampered-token cases. Demo: `uvicorn seed.mock_oidc:app --port 8098`, register issuer
`http://localhost:8098`, client `scos-demo`, secret `demo-secret`.

## Later (phase 3)
Directory-group → role mapping; authenticator-app codes (TOTP) for local accounts.
