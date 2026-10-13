"""Sign-in provider catalogue (dropdown). All entries speak OpenID Connect; the issuer template shows
what the administrator fills in. SAML-only or LDAP systems sit behind an OIDC bridge (e.g. Keycloak)."""
CATALOGUE = [
    # key, label, issuer template, default button label, trust_unverified_email default, note
    ("entra", "Microsoft Entra ID (Azure AD / Microsoft 365)", "https://login.microsoftonline.com/<tenant-id>/v2.0",
     "Sign in with Microsoft", True,
     "Register an app in Entra ID; use the tenant ID (not 'common'). Entra does not send email_verified."),
    ("google", "Google Workspace", "https://accounts.google.com", "Sign in with Google", False,
     "Create an OAuth client (Web application) in Google Cloud Console."),
    ("okta", "Okta", "https://<your-org>.okta.com/oauth2/default", "Sign in with Okta", False, ""),
    ("auth0", "Auth0", "https://<tenant>.auth0.com/", "Sign in with Auth0", False, "Issuer keeps its trailing slash."),
    ("keycloak", "Keycloak (also bridges SAML / LDAP / Active Directory)", "https://<host>/realms/<realm>",
     "Sign in with company account", False, ""),
    ("zoho", "Zoho", "https://accounts.zoho.in", "Sign in with Zoho", False, "Use your data-centre domain (.in, .com, .eu)."),
    ("generic", "Other OpenID Connect provider", "https://", "Sign in with SSO", False,
     "Any provider publishing /.well-known/openid-configuration."),
]
BY_KEY = {c[0]: c for c in CATALOGUE}


def as_dicts():
    return [{"key": k, "label": lab, "issuer_template": iss, "button_label": btn, "trust_unverified_email": trust,
             "note": note} for k, lab, iss, btn, trust, note in CATALOGUE]
