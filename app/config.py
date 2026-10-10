"""Runtime configuration. Everything is overridable by environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SCOS_DATA_DIR", BASE_DIR / "var"))
OBJECT_STORE_DIR = DATA_DIR / "objects"          # stand-in for S3-compatible storage
DATABASE_URL = os.environ.get("SCOS_DATABASE_URL", f"sqlite:///{DATA_DIR / 'subcontract_os.db'}")
PIPELINE_VERSION = "1.0"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".txt", ".md"}
