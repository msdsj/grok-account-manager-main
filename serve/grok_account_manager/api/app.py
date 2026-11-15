"""FastAPI application factory."""

from __future__ import annotations

import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from ..gateway import catalog as model_catalog
from ..gateway.auth import ensure_default_client_key
from ..core.paths import CREDENTIALS_DIR, WEB_DIST_DIR
from .routers import accounts, admin_auth, admin_stats, gateway, logs, mailboxes, register, state
from ..core import audit
from ..core import database as app_db
from .services.accounts import migrate_legacy_storage_once
from ..core.database import init_db
from .services.jobs import JOB_MANAGER


def create_app() -> FastAPI:
    load_dotenv()
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    migrate_legacy_storage_once()
    model_catalog.ensure_seeded()
    ensure_default_client_key()
    admin_auth.ensure_admin()

    app = FastAPI(
        title="Grok 2api API",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )

    app.include_router(state.router)
    app.include_router(admin_auth.router)
    app.include_router(admin_stats.router)
    app.include_router(register.router)
    app.include_router(mailboxes.router)
    app.include_router(accounts.router)
    app.include_router(logs.router)
    app.include_router(gateway.router)
    app.include_router(state.router)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.on_event("shutdown")
    def shutdown_services() -> None:
        audit.log_system("info", "app", "服务停止")
        JOB_MANAGER.stop(wait=True, timeout=12)

    @app.on_event("startup")
    def log_startup() -> None:
        audit.log_system("info", "app", "服务启动")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok", "service": "grok-account-manager", "time": int(time.time() * 1000)}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        try:
            accounts_count = len(app_db.list_accounts())
            models_count = len(model_catalog.list_models(enabled_only=False))
        except Exception as error:
            return JSONResponse({"status": "error", "error": str(error)}, status_code=503)
        return JSONResponse(
            {
                "status": "ready",
                "accounts": accounts_count,
                "models": models_count,
                "web": WEB_DIST_DIR.exists(),
            }
        )

    @app.get("/{request_path:path}", include_in_schema=False, response_model=None)
    def serve_spa(request_path: str):
        if request_path.startswith(("api/", "v1/", "admin/api")) or request_path in {"healthz", "readyz"}:
            raise HTTPException(status_code=404, detail="not found")
        if not WEB_DIST_DIR.exists():
            return JSONResponse(
                {
                    "error": "web/dist 不存在。开发模式请运行 cd web && npm run dev；生产模式先运行 cd web && npm run build。"
                },
                status_code=404,
            )

        relative = request_path.lstrip("/") or "index.html"
        root = WEB_DIST_DIR.resolve()
        file_path = (WEB_DIST_DIR / relative).resolve()
        if file_path != root and root not in file_path.parents:
            raise HTTPException(status_code=400, detail="invalid path")
        if not file_path.exists() or file_path.is_dir():
            file_path = WEB_DIST_DIR / "index.html"
        return FileResponse(Path(file_path))

    return app


app = create_app()
