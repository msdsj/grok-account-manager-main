"""OpenAI-compatible relay proxy routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..services.relay import RELAY_MANAGER

router = APIRouter(tags=["proxy"])

_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
_BLOCKED_RESPONSE_HEADERS = {
    "content-length",
    "transfer-encoding",
    "connection",
    "content-encoding",
}


@router.api_route("/v1", methods=_PROXY_METHODS)
@router.api_route("/v1/{path:path}", methods=_PROXY_METHODS)
async def proxy_v1(request: Request, path: str = "") -> Response:
    return await _proxy(request, f"/v1/{path}".rstrip("/"))


@router.api_route("/admin/api", methods=_PROXY_METHODS)
@router.api_route("/admin/api/{path:path}", methods=_PROXY_METHODS)
async def proxy_admin_api(request: Request, path: str = "") -> Response:
    return await _proxy(request, f"/admin/api/{path}".rstrip("/"))


async def _proxy(request: Request, path: str) -> Response:
    body = await request.body()
    try:
        upstream = RELAY_MANAGER.proxy_request(
            request.method,
            path,
            query=request.url.query,
            headers={key: value for key, value in request.headers.items()},
            body=body,
        )
    except Exception as error:
        return JSONResponse({"error": str(error)}, status_code=502)

    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _BLOCKED_RESPONSE_HEADERS
    }
    return Response(
        content=upstream.content or b"",
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type"),
    )

