"""Subcontract OS - MVP application (modular monolith)."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.db import init_db
from app.routers import contracts, documents, insight, obligations, registry

WEB = Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


def create_app(db_url: str | None = None) -> FastAPI:
    app = FastAPI(title="Subcontract OS", version="0.1.0-mvp",
                  description="Contract-to-evidence chain for EPC subcontracts (MVP vertical slice).",
                  lifespan=None if db_url else lifespan)
    if db_url:
        init_db(db_url)

    @app.exception_handler(HTTPException)
    async def typed_errors(_req: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "ERROR", "message": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content={"error": detail})

    for r in (registry, contracts, documents, obligations, insight):
        app.include_router(r.router)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    return app


app = create_app()
