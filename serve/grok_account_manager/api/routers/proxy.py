"""OpenAI-compatible relay proxy routes."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ...sinks.cpa_credential import build_cpa_download
from ...sinks.sub2api import (
    build_sub2api_oauth_accounts,
    create_sub2api_accounts,
    parse_sub2api_group_ids,
)
from ..services.relay import RELAY_MANAGER

router = APIRouter(tags=["proxy"])

_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
_BLOCKED_RESPONSE_HEADERS = {
    "content-length",
    "transfer-encoding",
    "connection",
    "content-encoding",
}

_SUB2API_DATA_TYPE = "sub2api-data"
_SUB2API_DATA_VERSION = 1


@router.api_route("/v1", methods=_PROXY_METHODS)
@router.api_route("/v1/{path:path}", methods=_PROXY_METHODS)
async def proxy_v1(request: Request, path: str = "") -> Response:
    return await _proxy(request, f"/v1/{path}".rstrip("/"))


@router.api_route("/api/admin/v1", methods=_PROXY_METHODS)
@router.api_route("/api/admin/v1/{path:path}", methods=_PROXY_METHODS)
async def proxy_admin_v1(request: Request, path: str = "") -> Response:
    if request.method == "DELETE" and (path == "accounts" or re.fullmatch(r"accounts/\d+", path)):
        return await _delete_accounts_with_local_cleanup(request, path)
    if request.method == "POST" and path == "accounts/import-sub2api":
        return await _import_selected_sub2api_accounts(request)
    if request.method == "POST" and path in {"accounts/export-cpa", "accounts/export-sub2api"}:
        return await _export_selected_accounts(request, path.rsplit("/", 1)[-1])
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

    return _upstream_response(upstream)


async def _export_selected_accounts(request: Request, format_name: str) -> Response:
    """Export the selected upstream accounts in a CPA or Sub2API document."""
    body = await request.body()
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _export_error("请求参数不是有效 JSON", 400, "invalidRequest")

    provider = str(payload.get("provider") or "").strip() if isinstance(payload, dict) else ""
    raw_ids = payload.get("ids") if isinstance(payload, dict) else None
    if provider not in {"grok_build", "grok_web", "grok_console"} or not isinstance(raw_ids, list):
        return _export_error("导出请求缺少有效的 provider 或 ids", 400, "invalidRequest")
    ids = [str(value).strip() for value in raw_ids]
    if not ids or len(ids) > 10_000 or len(set(ids)) != len(ids) or any(not value.isdigit() or int(value) <= 0 for value in ids):
        return _export_error("请选择有效的账号", 400, "invalidRequest")
    if provider != "grok_build":
        format_label = "CPA" if format_name == "export-cpa" else "Sub2API"
        return _export_error(
            f"{format_label} 格式仅支持 Grok Build OAuth 账号",
            400,
            "unsupportedExportFormat",
        )

    try:
        upstream = RELAY_MANAGER.proxy_request(
            "POST",
            "/api/admin/v1/accounts/export",
            query=request.url.query,
            headers={key: value for key, value in request.headers.items()},
            body=json.dumps({"provider": provider, "ids": ids}).encode("utf-8"),
            timeout=180,
        )
    except Exception as error:
        return _export_error(f"读取本地账号凭据失败：{error}", 502, "accountExportFailed")
    if not upstream.ok:
        return _upstream_response(upstream)

    try:
        document = upstream.json()
        accounts = document.get("accounts") if isinstance(document, dict) else None
        if not isinstance(accounts, list) or len(accounts) != len(ids):
            raise ValueError("新版 grok2api 返回的导出账号数量与所选数量不一致")
        if any(not isinstance(account, dict) for account in accounts):
            raise ValueError("新版 grok2api 返回了无效账号凭据")
        if format_name == "export-cpa":
            raw, filename, content_type = build_cpa_download(accounts)
        else:
            raw, filename, content_type = _build_sub2api_download(provider, accounts)
    except (ValueError, TypeError, KeyError) as error:
        return _export_error(str(error), 502, "exportFailed")

    return Response(
        content=raw,
        status_code=200,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Exported-Accounts": str(len(accounts)),
        },
    )


async def _import_selected_sub2api_accounts(request: Request) -> Response:
    """Import selected Grok Build OAuth accounts into the configured Sub2API."""
    body = await request.body()
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _export_error("请求参数不是有效 JSON", 400, "invalidRequest")

    provider = str(payload.get("provider") or "").strip() if isinstance(payload, dict) else ""
    raw_ids = payload.get("ids") if isinstance(payload, dict) else None
    if provider != "grok_build" or not isinstance(raw_ids, list):
        return _export_error("Sub2API 直连导入仅支持 Grok Build OAuth 账号", 400, "unsupportedImportFormat")
    ids = [str(value).strip() for value in raw_ids]
    if not ids or len(ids) > 10_000 or len(set(ids)) != len(ids) or any(not value.isdigit() or int(value) <= 0 for value in ids):
        return _export_error("请选择有效的账号", 400, "invalidRequest")

    try:
        upstream = RELAY_MANAGER.proxy_request(
            "POST",
            "/api/admin/v1/accounts/export",
            query=request.url.query,
            headers={key: value for key, value in request.headers.items()},
            body=json.dumps({"provider": provider, "ids": ids}).encode("utf-8"),
            timeout=180,
        )
    except Exception as error:
        return _export_error(f"读取本地账号凭据失败：{error}", 502, "accountExportFailed")
    if not upstream.ok:
        return _upstream_response(upstream)

    try:
        document = upstream.json()
        accounts = document.get("accounts") if isinstance(document, dict) else None
        if not isinstance(accounts, list) or len(accounts) != len(ids):
            raise ValueError("新版 grok2api 返回的导出账号数量与所选数量不一致")
        if any(not isinstance(account, dict) for account in accounts):
            raise ValueError("新版 grok2api 返回了无效账号凭据")
    except ValueError as error:
        return _export_error(str(error), 400, "invalidAccountCredentials")

    try:
        group_ids = parse_sub2api_group_ids(os.environ.get("SUB2API_DEFAULT_GROUP_IDS", ""))
    except ValueError as error:
        return _export_error(str(error), 400, "sub2apiNotConfigured")

    try:
        sub2api_accounts = _build_sub2api_accounts(provider, accounts, group_ids=group_ids)
    except ValueError as error:
        return _export_error(str(error), 400, "invalidAccountCredentials")

    try:
        result = create_sub2api_accounts(
            base_url=os.environ.get("SUB2API_BASE_URL", ""),
            api_key=os.environ.get("SUB2API_ADMIN_API_KEY", ""),
            accounts=sub2api_accounts,
            timeout=60,
        )
    except ValueError as error:
        return _export_error(str(error), 400, "sub2apiNotConfigured")
    except Exception as error:
        return _export_error(str(error), 502, "sub2apiImportFailed")

    errors = [
        _text(item.get("error"))
        for item in result.get("results", [])
        if isinstance(item, dict) and not item.get("success") and _text(item.get("error"))
    ]

    return JSONResponse(
        {
            "data": {
                "total": len(sub2api_accounts),
                "succeeded": int(result["success"]),
                "failed": int(result["failed"]),
                "groupIds": group_ids,
                "errors": errors[:5],
            }
        }
    )


def _build_sub2api_download(
    provider: str,
    accounts: list[dict],
    now: float | None = None,
) -> tuple[bytes, str, str]:
    """Build a native Sub2API account-data backup for Grok OAuth accounts."""
    if provider != "grok_build":
        raise ValueError("Sub2API 格式仅支持 Grok Build OAuth 账号")

    result = _build_sub2api_accounts(provider, accounts)

    timestamp = time.time() if now is None else now
    raw = (
        json.dumps(
            {
                "type": _SUB2API_DATA_TYPE,
                "version": _SUB2API_DATA_VERSION,
                "exported_at": _sub2api_rfc3339(timestamp),
                "proxies": [],
                "accounts": result,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(timestamp))
    filename = f"grok2api-{provider.replace('_', '-')}-sub2api-{stamp}.json"
    return raw, filename, "application/json; charset=utf-8"


def _build_sub2api_accounts(
    provider: str,
    accounts: list[dict],
    *,
    group_ids: list[int] | None = None,
) -> list[dict]:
    """Keep the existing router helper backed by the shared Sub2API mapper."""
    return build_sub2api_oauth_accounts(provider, accounts, group_ids=group_ids)


def _text(value: object) -> str:
    return str(value or "").strip()


def _sub2api_rfc3339(value: object) -> str:
    """Normalize Grok export timestamps to the RFC3339 strings Sub2API stores."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
    else:
        raw = _text(value)
        if not raw:
            return ""
        try:
            timestamp = float(raw)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return raw
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    if timestamp <= 0:
        return ""
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _export_error(message: str, status_code: int, code: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


def _upstream_response(upstream) -> Response:
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
