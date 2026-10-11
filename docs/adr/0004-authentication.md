# ADR-0004 — Authentication: sessions, temporary passwords, lockout

Status: **Accepted** · 2026-10-11 · replaces the `X-User` development stub

| Control | Implementation |
|---|---|
| Passwords | PBKDF2-HMAC-SHA256, 600 000 iterations, per-user salt (stdlib, no extra dependency) |
| Temporary passwords | Issued by an administrator (`Users & access`), the demo loader or `python -m app.cli create-admin`. Shown once. The account can call nothing except sign-in, `/me` and change-password until the user sets their own. |
| Password policy | ≥ 12 characters, upper + lower case, a digit, must not contain the username or reuse the temporary pattern |
| Lockout | 5 failed attempts → locked 15 minutes (configurable); administrator can unlock. Generic error text, so usernames cannot be enumerated. |
| Sessions | 256-bit random token in an `HttpOnly`, `SameSite=Strict` cookie (`Secure` when `SCOS_COOKIE_SECURE=1` behind HTTPS). Only the SHA-256 is stored; sessions expire (8 h default) and are revoked on sign-out, password change, reset and deactivation. |
| CSRF | Cookie-authenticated writes must send `X-SCOS-CSRF: 1`, which a cross-site form cannot set. |
| API clients | `issue_bearer_token: true` at sign-in returns a bearer token (same session rules). |
| Audit | Sign-in, failure, lockout, sign-out, password change, user creation/reset/update are in the audit ledger. |
| Dev header | `X-User` works only when `SCOS_ALLOW_DEV_AUTH=1` (tests); disabled by default. |

## Next
Enterprise SSO (OIDC/SAML) and MFA plug into the same session layer: the identity provider authenticates,
the OS creates the session. Until then, use this only on a trusted network.
