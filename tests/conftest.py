import pytest
from fastapi.testclient import TestClient

from app import config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OBJECT_STORE_DIR", tmp_path / "objects")
    from app.db import SessionLocal
    from app.main import create_app
    from seed.demo_seed import create_users
    app = create_app(f"sqlite:///{tmp_path / 'test.db'}")
    with SessionLocal() as db:
        create_users(db)
    return TestClient(app)


@pytest.fixture
def world(client):
    """The full demo pilot loaded through the API (itself an end-to-end test of the vertical slice)."""
    from seed.demo_seed import run
    return run(client)


def H(user, **extra):
    return {"X-User": user, **extra}
