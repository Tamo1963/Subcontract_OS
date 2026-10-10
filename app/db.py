"""Database engine and session handling (SQLite for the MVP, PostgreSQL via SCOS_DATABASE_URL)."""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app import config


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
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
    Base.metadata.create_all(engine)
    return engine


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
