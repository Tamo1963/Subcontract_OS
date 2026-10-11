"""Runtime configuration. Values come from the environment, optionally via a local ``.env`` file
(never committed). See ``.env.example``."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.environ.get("SCOS_DATA_DIR", BASE_DIR / "var"))
OBJECT_STORE_DIR = DATA_DIR / "objects"          # stand-in for S3-compatible storage
DATABASE_URL = os.environ.get("SCOS_DATABASE_URL", f"sqlite:///{DATA_DIR / 'subcontract_os.db'}")
PIPELINE_VERSION = "1.0"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".txt", ".md"}

# Authentication
SESSION_HOURS = int(os.environ.get("SCOS_SESSION_HOURS", "8"))
MAX_FAILED_LOGINS = int(os.environ.get("SCOS_MAX_FAILED_LOGINS", "5"))
LOCKOUT_MINUTES = int(os.environ.get("SCOS_LOCKOUT_MINUTES", "15"))
COOKIE_SECURE = os.environ.get("SCOS_COOKIE_SECURE", "0") == "1"     # set 1 behind HTTPS
# Development-only header login (X-User). Off unless explicitly enabled; used by the test suite.
ALLOW_DEV_AUTH = os.environ.get("SCOS_ALLOW_DEV_AUTH", "0") == "1"


def is_sqlite(url: str | None = None) -> bool:
    return (url or DATABASE_URL).startswith("sqlite")
