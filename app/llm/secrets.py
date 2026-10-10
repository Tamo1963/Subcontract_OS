"""API-key protection. Keys are encrypted at rest (Fernet) and never returned by the API.

Production should prefer ``api_key_env`` (the key lives in the host's secret store / environment)
or set ``SCOS_SECRET_KEY`` from a managed secrets service. Without it, a key file is generated in
the data directory with owner-only permissions.
"""
import os

from cryptography.fernet import Fernet

from app import config


def _fernet() -> Fernet:
    key = os.environ.get("SCOS_SECRET_KEY")
    if not key:
        path = config.DATA_DIR / "secret.key"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(Fernet.generate_key())
            os.chmod(path, 0o600)
        key = path.read_bytes().decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def hint(plain: str) -> str:
    return "…" + plain[-4:] if len(plain) >= 8 else "…set"
