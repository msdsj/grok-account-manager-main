"""Local mailbox-pool management endpoints."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..schemas import OutlookMailboxPoolRequest
from ..services.mailbox_pool import load_outlook_mailbox_pool, save_outlook_mailbox_pool

router = APIRouter(tags=["mailboxes"])


def _private_response(payload: dict) -> JSONResponse:
    return JSONResponse(
        payload,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _require_local_origin(request: Request) -> None:
    """Do not let the application's permissive public CORS policy expose local secrets."""
    origin = request.headers.get("origin", "").strip()
    if not origin:
        return
    origin_host = (urlparse(origin).hostname or "").lower()
    request_host = (request.url.hostname or "").lower()
    local_hosts = {"127.0.0.1", "::1", "localhost"}
    if origin_host not in local_hosts and origin_host != request_host:
        raise HTTPException(status_code=403, detail="Outlook 账号池只能从本机页面访问")


@router.get("/api/mailboxes/outlook")
def get_outlook_mailbox_pool(request: Request) -> JSONResponse:
    try:
        _require_local_origin(request)
        return _private_response(load_outlook_mailbox_pool())
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.put("/api/mailboxes/outlook")
def put_outlook_mailbox_pool(body: OutlookMailboxPoolRequest, request: Request) -> JSONResponse:
    try:
        _require_local_origin(request)
        return _private_response(save_outlook_mailbox_pool(body.data))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
