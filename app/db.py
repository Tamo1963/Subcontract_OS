"""Database engine and session handling: PostgreSQL (SCOS_DATABASE_URL) or SQLite for development."""
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app import config


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else \
        {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 10}
    engine = create_engine(url, future=True, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # enforce FKs (incl. the composite project/main-contract FK)
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return engine


engine = None
SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False, future=True)


def init_db(url: str | None = None):
    global engine
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.OBJECT_STORE_DIR.mkdir(parents=True, exist_ok=True)
    engine = make_engine(url or config.DATABASE_URL)
    SessionLocal.configure(bind=engine)
    from app import models  # noqa: F401  (register tables)
    if config.is_sqlite(str(engine.url)):
        Base.metadata.create_all(engine)          # development / tests
    elif os.environ.get("SCOS_AUTO_MIGRATE", "1") == "1":
        migrate(engine)                           # PostgreSQL: versioned Alembic migrations
    return engine


def migrate(engine_=None):
    """Apply Alembic migrations up to head (idempotent)."""
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(config.BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.BASE_DIR / "migrations"))
    with (engine_ or engine).begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def reset_database():
    """Drop every application table and re-create the schema (demo reset only)."""
    from sqlalchemy import text
    from app import models  # noqa: F401
    Base.metadata.drop_all(engine)
    if not config.is_sqlite(str(engine.url)):
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        migrate()
    else:
        Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
