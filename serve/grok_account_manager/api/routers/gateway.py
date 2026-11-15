"""OpenAI 兼容的 /v1 网关路由。"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...gateway import audit as gateway_audit
from ...gateway import catalog
from ...gateway.auth import AuthContext, authenticate, create_client_key, require_scope
from ...gateway.errors import GatewayError
from ...gateway.pool import PoolAccount, cool_down, pick
from ...gateway.registry import get_adapter
from ...core import audit as operation_audit
from ...core import database as db

router = APIRouter(tags=["gateway"])

_MAX_ACCOUNT_ATTEMPTS = 3


def _timeout_seconds() -> int:
    try:
        return max(5, int(os.environ.get("GATEWAY_UPSTREAM_TIMEOUT") or 180))
    except ValueError:
        return 180


def _authorize(request: Request) -> AuthContext:
    return authenticate(request.headers.get("authorization"), request.headers.get("x-api-key"))


def _error_response(error: GatewayError) -> JSONResponse:
    return JSONResponse(error.to_payload(), status_code=error.status_code)


def _openai_error(error: GatewayError) -> str:
    body = json.dumps(error.to_payload(), ensure_ascii=False)
    return f"data: {body}\n\n"


def _account_payload(account: PoolAccount) -> str:
    return account.account_id or account.email or account.export_key


@router.get("/v1/models")
def list_models(request: Request):
    try:
        auth = _authorize(request)
    except GatewayError as error:
        return _error_response(error)
    items = []
    for entry in catalog.list_models():
        capability = str(entry.get("capability") or "chat")
        public_model = str(entry.get("publicModel") or "")
        if not auth.local:
            if capability not in auth.scopes:
                continue
            if auth.models and public_model not in auth.models:
                continue
        items.append(
            {
                "id": public_model,
                "object": "model",
                "created": 0,
                "owned_by": entry.get("provider") or "grok",
                "capability": capability,
            }
        )
    return {"object": "list", "data": items}


def _resolve_route(payload: dict[str, Any], capability: str, auth: AuthContext):
    route = catalog.resolve_model(str(payload.get("model") or ""))
    if route.capability != capability:
        raise GatewayError(
            f"模型 {route.public_model} 属于 {route.capability} 能力，不能用于 {capability} 接口",
            status_code=400,
            code="invalid_request_error",
        )
    require_scope(auth, capability, route.public_model)
    return route


def _chat_stream_response(
    *,
    adapter,
    upstream_response,
    route,
    auth: AuthContext,
    account: PoolAccount,
    payload: dict[str, Any],
    started_at: int,
    request_id: str,
) -> StreamingResponse:
    usage: dict[str, int] = {}

    def generate():
        status = "success"
        error_text = ""
        try:
            for chunk in adapter.stream_chunks(upstream_response, route, usage):
                yield chunk
        except GatewayError as error:
            status = "error"
            error_text = error.message
            cool_down(account.export_key, upstream_status=error.upstream_status)
            yield _openai_error(error)
            yield "data: [DONE]\n\n"
        except Exception as error:  # pragma: no cover - 网络中断等异常
            status = "error"
            error_text = str(error)
            yield _openai_error(GatewayError(str(error), status_code=502, code="upstream_error"))
            yield "data: [DONE]\n\n"
        finally:
            gateway_audit.record(
                request_id=request_id,
                auth=auth,
                route=route,
                account_id=_account_payload(account),
                status=status,
                http_status=200 if status == "success" else 502,
                latency_ms=int(time.time() * 1000) - started_at,
                stream=True,
                usage=usage,
                error=error_text,
                payload=payload,
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _synthetic_stream_response(
    *,
    result: dict[str, Any],
    route,
    auth: AuthContext,
    account: PoolAccount,
    payload: dict[str, Any],
    started_at: int,
    request_id: str,
) -> StreamingResponse:
    """为没有原生流式能力的适配器生成单段 SSE，保持客户端协议一致。"""
    content = ""
    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            content = str(message.get("content") or "")
    usage = result.get("usage") or {}
    completion_id = str(result.get("id") or f"chatcmpl-{uuid.uuid4().hex}")
    created = int(result.get("created") or time.time())

    def generate():
        yield "data: " + json.dumps(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": route.public_model,
                "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
            },
            ensure_ascii=False,
        ) + "\n\n"
        yield "data: " + json.dumps(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": route.public_model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
            ensure_ascii=False,
        ) + "\n\n"
        yield "data: [DONE]\n\n"
        gateway_audit.record(
            request_id=request_id,
            auth=auth,
            route=route,
            account_id=_account_payload(account),
            status="success",
            http_status=200,
            latency_ms=int(time.time() * 1000) - started_at,
            stream=True,
            usage=usage,
            payload=payload,
        )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/v1/chat/completions")
def chat_completions(request: Request, payload: dict[str, Any] = Body(...)):
    request_id = uuid.uuid4().hex
    started_at = int(time.time() * 1000)
    auth = None
    route = None
    try:
        auth = _authorize(request)
        route = _resolve_route(payload, "chat", auth)
        adapter = get_adapter(route.provider)
        stream = bool(payload.get("stream"))
        timeout = _timeout_seconds()
        exclude: set[str] = set()
        last_error: GatewayError | None = None
        for _ in range(_MAX_ACCOUNT_ATTEMPTS):
            account = pick(route.provider, "chat", exclude=exclude)
            if account is None:
                break
            try:
                stream_supported = stream and callable(getattr(adapter, "open_chat_stream", None))
                if stream_supported:
                    upstream_response, _credential = adapter.open_chat_stream(
                        account, route, payload, timeout=timeout
                    )
                    return _chat_stream_response(
                        adapter=adapter,
                        upstream_response=upstream_response,
                        route=route,
                        auth=auth,
                        account=account,
                        payload=payload,
                        started_at=started_at,
                        request_id=request_id,
                    )
                result, _credential = adapter.chat(account, route, payload, timeout=timeout)
                if stream:
                    return _synthetic_stream_response(
                        result=result,
                        route=route,
                        auth=auth,
                        account=account,
                        payload=payload,
                        started_at=started_at,
                        request_id=request_id,
                    )
                gateway_audit.record(
                    request_id=request_id,
                    auth=auth,
                    route=route,
                    account_id=_account_payload(account),
                    status="success",
                    http_status=200,
                    latency_ms=int(time.time() * 1000) - started_at,
                    stream=False,
                    usage=result.get("usage") or {},
                    payload=payload,
                )
                return result
            except GatewayError as error:
                last_error = error
                if error.upstream_status in {401, 403, 429} or error.upstream_status >= 500:
                    cool_down(account.export_key, upstream_status=error.upstream_status)
                    exclude.add(account.export_key)
                    continue
                raise
            except Exception as error:
                last_error = GatewayError(str(error), status_code=502, code="upstream_error")
                cool_down(account.export_key, 30)
                exclude.add(account.export_key)
        if last_error is not None:
            raise last_error
        raise GatewayError(
            f"{route.provider} 没有可用账号，请先在账号管理中导入或注册",
            status_code=503,
            code="no_available_account",
        )
    except GatewayError as error:
        status_code = 502 if error.status_code in {401, 403} else error.status_code
        if auth is not None:
            gateway_audit.record(
                request_id=request_id,
                auth=auth,
                route=route,
                account_id="",
                status="error",
                http_status=status_code,
                latency_ms=int(time.time() * 1000) - started_at,
                stream=bool(payload.get("stream")),
                usage={},
                error=error.message,
                payload=payload,
            )
        if status_code != error.status_code:
            error = GatewayError(error.message, status_code=status_code, code=error.code)
        return _error_response(error)
    except Exception as error:  # pragma: no cover - 兜底
        return JSONResponse(
            {"error": {"message": str(error), "type": "internal_error", "code": "internal_error"}},
            status_code=500,
        )


@router.post("/v1/images/generations")
@router.post("/v1/images/edits")
def generate_images(request: Request, payload: dict[str, Any] = Body(...)):
    request_id = uuid.uuid4().hex
    started_at = int(time.time() * 1000)
    auth = None
    route = None
    try:
        auth = _authorize(request)
        if not payload.get("model"):
            payload = dict(payload)
            payload["model"] = "grok-imagine-image"
        route = _resolve_route(payload, "image", auth)
        adapter = get_adapter(route.provider)
        timeout = _timeout_seconds()
        exclude: set[str] = set()
        last_error: GatewayError | None = None
        for _ in range(_MAX_ACCOUNT_ATTEMPTS):
            account = pick(route.provider, "image", exclude=exclude)
            if account is None:
                break
            try:
                result, _credential = adapter.image(account, route, payload, timeout=timeout)
                gateway_audit.record(
                    request_id=request_id,
                    auth=auth,
                    route=route,
                    account_id=_account_payload(account),
                    status="success",
                    http_status=200,
                    latency_ms=int(time.time() * 1000) - started_at,
                    stream=False,
                    usage={},
                    payload=payload,
                )
                return result
            except GatewayError as error:
                last_error = error
                if error.upstream_status in {401, 403, 429} or error.upstream_status >= 500:
                    cool_down(account.export_key, upstream_status=error.upstream_status)
                    exclude.add(account.export_key)
                    continue
                raise
            except Exception as error:
                last_error = GatewayError(str(error), status_code=502, code="upstream_error")
                cool_down(account.export_key, 30)
                exclude.add(account.export_key)
        if last_error is not None:
            raise last_error
        raise GatewayError(
            f"{route.provider} 没有可用账号，请先在账号管理中导入或注册",
            status_code=503,
            code="no_available_account",
        )
    except GatewayError as error:
        status_code = 502 if error.status_code in {401, 403} else error.status_code
        if auth is not None:
            gateway_audit.record(
                request_id=request_id,
                auth=auth,
                route=route,
                account_id="",
                status="error",
                http_status=status_code,
                latency_ms=int(time.time() * 1000) - started_at,
                stream=False,
                usage={},
                error=error.message,
                payload=payload,
            )
        if status_code != error.status_code:
            error = GatewayError(error.message, status_code=status_code, code=error.code)
        return _error_response(error)
    except Exception as error:  # pragma: no cover - 兜底
        return JSONResponse(
            {"error": {"message": str(error), "type": "internal_error", "code": "internal_error"}},
            status_code=500,
        )


@router.post("/v1/videos/generations")
def generate_video(request: Request, payload: dict[str, Any] = Body(...)):
    request_id = uuid.uuid4().hex
    started_at = int(time.time() * 1000)
    auth = None
    route = None
    try:
        auth = _authorize(request)
        if not payload.get("model"):
            payload = dict(payload)
            payload["model"] = "grok-imagine-video-1.5"
        route = _resolve_route(payload, "video", auth)
        adapter = get_adapter(route.provider)
        timeout = _timeout_seconds()
        exclude: set[str] = set()
        last_error: GatewayError | None = None
        for _ in range(_MAX_ACCOUNT_ATTEMPTS):
            account = pick(route.provider, "video", exclude=exclude)
            if account is None:
                break
            try:
                result, _credential = adapter.video(account, route, payload, timeout=timeout)
                gateway_audit.record(
                    request_id=request_id,
                    auth=auth,
                    route=route,
                    account_id=_account_payload(account),
                    status="success",
                    http_status=200,
                    latency_ms=int(time.time() * 1000) - started_at,
                    stream=False,
                    usage={},
                    payload=payload,
                )
                return result
            except GatewayError as error:
                last_error = error
                if error.upstream_status in {401, 403, 429} or error.upstream_status >= 500:
                    cool_down(account.export_key, upstream_status=error.upstream_status)
                    exclude.add(account.export_key)
                    continue
                raise
            except Exception as error:
                last_error = GatewayError(str(error), status_code=502, code="upstream_error")
                cool_down(account.export_key, 30)
                exclude.add(account.export_key)
        if last_error is not None:
            raise last_error
        raise GatewayError(
            f"{route.provider} 没有可用账号，请先在账号管理中导入或注册",
            status_code=503,
            code="no_available_account",
        )
    except GatewayError as error:
        status_code = 502 if error.status_code in {401, 403} else error.status_code
        if status_code != error.status_code:
            error = GatewayError(error.message, status_code=status_code, code=error.code)
        return _error_response(error)
    except Exception as error:  # pragma: no cover - 兜底
        return JSONResponse(
            {"error": {"message": str(error), "type": "internal_error", "code": "internal_error"}},
            status_code=500,
        )


@router.get("/v1/videos/{video_id}")
def get_video(video_id: str, request: Request):
    try:
        _authorize(request)
        adapter = get_adapter("grok")
        exclude: set[str] = set()
        last_error: GatewayError | None = None
        for _ in range(_MAX_ACCOUNT_ATTEMPTS):
            account = pick("grok", "video", exclude=exclude)
            if account is None:
                break
            try:
                result, _credential = adapter.video_status(account, video_id, timeout=_timeout_seconds())
                return result
            except GatewayError as error:
                last_error = error
                if error.upstream_status in {401, 403, 429} or error.upstream_status >= 500:
                    cool_down(account.export_key, upstream_status=error.upstream_status)
                    exclude.add(account.export_key)
                    continue
                raise
            except Exception as error:
                last_error = GatewayError(str(error), status_code=502, code="upstream_error")
                cool_down(account.export_key, 30)
                exclude.add(account.export_key)
        if last_error is not None:
            raise last_error
        raise GatewayError(
            "grok 没有可用账号，请先在账号管理中导入或注册",
            status_code=503,
            code="no_available_account",
        )
    except GatewayError as error:
        status_code = 502 if error.status_code in {401, 403} else error.status_code
        if status_code != error.status_code:
            error = GatewayError(error.message, status_code=status_code, code=error.code)
        return _error_response(error)
    except Exception as error:  # pragma: no cover - 兜底
        return JSONResponse(
            {"error": {"message": str(error), "type": "internal_error", "code": "internal_error"}},
            status_code=500,
        )


@router.get("/api/gateway/models")
def list_catalog_models() -> dict:
    return {"items": catalog.list_models(enabled_only=False)}


@router.get("/api/gateway/status")
def gateway_status() -> dict:
    items = []
    for provider in ("grok", "chatgpt", "kiro"):
        accounts = [account for account in _pool_accounts(provider)]
        items.append(
            {
                "provider": provider,
                "accounts": len(accounts),
                "models": sum(
                    1 for entry in catalog.list_models() if entry.get("provider") == provider
                ),
            }
        )
    return {"providers": items}


def _pool_accounts(provider: str) -> list[PoolAccount]:
    from ...gateway.pool import list_accounts

    return list_accounts(provider, "chat")


@router.get("/api/gateway/keys")
def list_client_keys() -> dict:
    return {"items": db.list_client_keys()}


@router.post("/api/gateway/keys")
def add_client_key(request: Request, payload: dict[str, Any] = Body(...)) -> dict:
    try:
        record = create_client_key(
            str(payload.get("name") or ""),
            scopes=[str(item) for item in payload.get("scopes") or []],
            models=[str(item) for item in payload.get("models") or []],
        )
    except GatewayError as error:
        return _error_response(error)
    operation_audit.log_operation(
        operation_audit.actor_from_request(request),
        "gateway.key.create",
        target_type="client_key",
        target_id=record["id"],
        detail={"name": record["name"], "scopes": record["scopes"], "models": record["models"]},
    )
    return {"key": record}


@router.delete("/api/gateway/keys/{key_id}")
def remove_client_key(key_id: str, request: Request) -> dict:
    deleted = db.delete_client_key(key_id)
    operation_audit.log_operation(
        operation_audit.actor_from_request(request),
        "gateway.key.delete",
        target_type="client_key",
        target_id=key_id,
        result="success" if deleted else "not_found",
    )
    return {"deleted": deleted}


@router.get("/api/gateway/requests")
def list_gateway_requests(
    provider: str = "",
    status: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    return db.list_gateway_requests(
        provider=provider,
        status=status,
        limit=max(1, min(500, limit)),
        offset=max(0, offset),
    )
