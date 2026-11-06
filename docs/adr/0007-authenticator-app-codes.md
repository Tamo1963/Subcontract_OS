# ADR-0007 — Authenticator-app codes (TOTP) for password sign-in

**Status:** Accepted · 6 Nov 2026 · supersedes the "Later" TOTP item in ADR-0005

## Context
Company sign-in (ADR-0005) delegates multi-factor authentication to the company's identity provider.
Username + password accounts — administrators' break-glass accounts above all — need their own second
step, without a phone/SMS gateway or a paid service.

## Decision
1. **Standard TOTP (RFC 6238):** 6 digits, 30 s, HMAC-SHA1, implemented with the Python standard
   library (`app/mfa.py`) and checked against the RFC reference vector. Works with Microsoft
   Authenticator, Google Authenticator, Authy, 1Password, Bitwarden. `segno` draws the QR code; the key
   is also shown as text.
2. **Two-step sign-in.** A correct password for an enrolled account returns a 5-minute, single-use
   challenge (`identity_mfa_challenges`, only a hash stored, at most 5 attempts) instead of a session.
   `POST /auth/login/mfa` with the code issues the session. Wrong codes count towards the account
   lockout. Sessions record how they were created (`password`, `password+totp`, `password+recovery`, `sso`).
3. **No replay, small drift.** ±1 step is accepted; the last accepted step is stored and a code is never
   accepted twice.
4. **Secrets encrypted at rest** with the platform key (`SCOS_SECRET_KEY` / `var/secret.key`). Set-up is
   two-phase: the secret stays *pending* until the user proves the app works with a current code.
5. **Recovery codes:** 10 single-use codes (50 random bits each) shown once at enrolment; only SHA-256
   stored; can be replaced with a current app code. Fewer than 4 left → the user is warned at sign-in.
6. **Policy** (Users & access → Sign-in providers): *Optional* (default), *Required for administrators*,
   *Required for everyone*. When required, a password session of someone not yet enrolled can reach only
   the sign-in and "my account" endpoints (`MFA_ENROLMENT_REQUIRED`) until they set up the app; they cannot
   turn it off while it is required.
7. **Company sign-in is not affected** — the provider applies its own MFA; accounts without a password
   have nothing to enrol.
8. **Lost phone:** recovery code → administrator *Reset 2-step* (not on their own account; signs the person
   out everywhere; audited) → last resort `python -m app.cli reset-mfa <username>` on the server.
9. Enrolling or resetting signs out the person's other sessions. Every step is in the audit trail
   (`auth.mfa_*`, `user.mfa_reset`).

## Consequences
- Back up `SCOS_SECRET_KEY` / `var/secret.key` with the database: without it, enrolled users must be reset.
- Server clocks must be NTP-synchronised (codes are time-based).
- Not included: WebAuthn/passkeys, "remember this device", SMS/e-mail codes (weaker; deliberately omitted).

## Tests
`tests/test_mfa.py` (11): RFC vector, enrolment, two-step sign-in, replay, single-use challenge, lockout,
expiry, recovery codes, other-session sign-out, policy gate (admins/all), company sign-in unaffected,
disable rules, administrator reset, bearer tokens, passwordless accounts.
