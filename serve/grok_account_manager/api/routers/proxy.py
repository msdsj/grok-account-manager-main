"""OpenAI-compatible relay proxy routes."""

from __future__ import annotations

import json
import re

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


@router.api_route("/api/admin/v1", methods=_PROXY_METHODS)
@router.api_route("/api/admin/v1/{path:path}", methods=_PROXY_METHODS)
async def proxy_admin_v1(request: Request, path: str = "") -> Response:
    if request.method == "DELETE" and (path == "accounts" or re.fullmatch(r"accounts/\d+", path)):
        return await _delete_accounts_with_local_cleanup(request, path)
    return await _proxy(request, f"/api/admin/v1/{path}".rstrip("/"))


@router.api_route("/healthz", methods=["GET"])
async def proxy_healthz(request: Request) -> Response:
    return await _proxy(request, "/healthz")


@router.api_route("/admin/api", methods=_PROXY_METHODS)
@router.api_route("/admin/api/{path:path}", methods=_PROXY_METHODS)
async def proxy_admin_api(request: Request, path: str = "") -> Response:
    return await _proxy(request, f"/admin/api/{path}".rstrip("/"))


async def _proxy(request: Request, path: str) -> Response:
    body = await request.body()
    return await _proxy_body(request, path, body)


async def _proxy_body(request: Request, path: str, body: bytes) -> Response:
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


def _account_data(response) -> dict:
    if not response.ok:
        raise RuntimeError(f"读取 grok2api 账号失败：HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError("读取 grok2api 账号返回了无效 JSON") from error
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("读取 grok2api 账号返回了无效数据")
    return data


def _account_ids_from_delete(path: str, body: bytes) -> list[str]:
    if path != "accounts":
        return [path.rsplit("/", 1)[-1]]
    if not body:
        return []
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("删除账号请求不是有效 JSON") from error
    ids = payload.get("ids") if isinstance(payload, dict) else None
    if not isinstance(ids, list):
        raise ValueError("批量删除账号缺少 ids")
    return [str(value).strip() for value in ids if str(value).strip()]


def _load_upstream_accounts(request: Request, ids: list[str]) -> list[dict]:
    accounts: list[dict] = []
    headers = {key: value for key, value in request.headers.items()}
    seen_ids: set[str] = set()
    for account_id in ids:
        if not account_id.isdigit() or account_id in seen_ids:
            continue
        seen_ids.add(account_id)
        response = RELAY_MANAGER.proxy_request(
            "GET",
            f"/api/admin/v1/accounts/{account_id}",
            headers=headers,
            timeout=20,
        )
        account = _account_data(response)
        accounts.append(account)
        linked = account.get("linkedAccounts")
        if isinstance(linked, list):
            accounts.extend(item for item in linked if isinstance(item, dict))
        linked_id = str(account.get("linkedAccountId") or "").strip()
        if linked_id.isdigit() and linked_id not in seen_ids:
            seen_ids.add(linked_id)
            linked_response = RELAY_MANAGER.proxy_request(
                "GET",
                f"/api/admin/v1/accounts/{linked_id}",
                headers=headers,
                timeout=20,
            )
            accounts.append(_account_data(linked_response))
    return accounts


def _delete_completed(response, path: str) -> bool:
    if path != "accounts":
        return True
    try:
        payload = response.json()
    except ValueError:
        return True
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return True
    try:
        return int(data.get("skipped") or 0) == 0
    except (TypeError, ValueError):
        return True


async def _delete_accounts_with_local_cleanup(request: Request, path: str) -> Response:
    body = await request.body()
    try:
        ids = _account_ids_from_delete(path, body)
        upstream_accounts = _load_upstream_accounts(request, ids) if ids else []
    except Exception as error:
        return JSONResponse({"error": str(error)}, status_code=502)

    try:
        upstream = RELAY_MANAGER.proxy_request(
            request.method,
            f"/api/admin/v1/{path}".rstrip("/"),
            query=request.url.query,
            headers={key: value for key, value in request.headers.items()},
            body=body,
        )
    except Exception as error:
        return JSONResponse({"error": str(error)}, status_code=502)

    if 200 <= upstream.status_code < 300 and upstream_accounts and _delete_completed(upstream, path):
        try:
            from ..services.accounts import delete_local_credentials_for_upstream_accounts

            delete_local_credentials_for_upstream_accounts(upstream_accounts)
        except Exception as error:
            # The upstream account is already gone. Surface the local cleanup
            # failure instead of pretending the operation was fully complete.
            return JSONResponse(
                {"error": f"grok2api 已删除，但本地凭证清理失败：{error}"},
                status_code=500,
            )

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
