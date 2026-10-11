"""Alembic environment: versioned schema changes for PostgreSQL (and SQLite in development)."""
from alembic import context
from sqlalchemy import engine_from_config, pool

from app import config as app_config
from app import models  # noqa: F401  (register tables)
from app.db import Base

cfg = context.config
url = cfg.attributes.get("url") or app_config.DATABASE_URL
target_metadata = Base.metadata


def run_offline():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online():
    connectable = cfg.attributes.get("connection")
    if connectable is None:
        connectable = engine_from_config({"sqlalchemy.url": url}, prefix="sqlalchemy.", poolclass=pool.NullPool)
        with connectable.connect() as conn:
            _run(conn)
    else:
        _run(connectable)


def _run(conn):
    context.configure(connection=conn, target_metadata=target_metadata, compare_type=True,
                      render_as_batch=conn.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
