"""FastAPI application factory."""

from __future__ import annotations

import threading
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .config import CREDENTIALS_DIR, WEB_DIST_DIR
from .routers import accounts, mailboxes, proxy, register, relay, state
from .services.database import init_db
from .services.jobs import JOB_MANAGER
from .services.relay import RELAY_MANAGER


def create_app() -> FastAPI:
    load_dotenv()
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

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
    app.include_router(register.router)
    app.include_router(mailboxes.router)
    app.include_router(accounts.router)
    app.include_router(relay.router)
    app.include_router(proxy.router)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.on_event("shutdown")
    def shutdown_services() -> None:
        JOB_MANAGER.stop(wait=True, timeout=12)
        RELAY_MANAGER.stop()

    @app.on_event("startup")
    def start_relay_engine() -> None:
        # grok2api takes tens of seconds to boot (Docker container / go binary), so start
        # it in the background instead of blocking the FastAPI startup handshake on it.
        def _boot() -> None:
            try:
                RELAY_MANAGER.start()
            except Exception as error:
                print(f"[app] grok2api 引擎自动启动失败，将在首次使用时重试: {error}")

        threading.Thread(target=_boot, name="relay-autostart", daemon=True).start()

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
