"""Kiro 上游协议封装：凭证刷新、请求构造与事件流转换。"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time
import uuid
from collections.abc import Iterator
from typing import Any

import requests

from ....gateway.errors import GatewayError
from ....gateway.pool import PoolAccount
from . import eventstream

DEFAULT_REGION = "us-east-1"
EU_REGION = "eu-central-1"
KIRO_VERSION = "0.12.155"
AWS_SDK_VERSION = "1.0.34"
AGENT_MODE = "vibe"
AMZ_TARGET = "AmazonCodeWhispererStreamingService.GenerateAssistantResponse"
SOCIAL_REFRESH_URL = "https://prod.us-east-1.auth.desktop.kiro.dev/refreshToken"
OIDC_TOKEN_URL = "https://oidc.{region}.amazonaws.com/token"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _first(credential: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(credential.get(key))
        if value:
            return value
    return ""


def endpoint_region(region: str) -> str:
    """参考实现：非 EU 账号固定用 us-east-1，EU 账号切到 eu-central-1。"""
    return EU_REGION if _text(region).lower().startswith("eu-") else DEFAULT_REGION


def endpoint_urls(region: str) -> list[str]:
    override = os.environ.get("KIRO_ENDPOINT_BASE")
    if override:
        return [f"{override.rstrip('/')}/generateAssistantResponse"]
    target = endpoint_region(region)
    return [
        f"https://codewhisperer.{target}.amazonaws.com/generateAssistantResponse",
        f"https://q.{target}.amazonaws.com/generateAssistantResponse",
    ]


def _platform_tag() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "win32"
    return "linux"


def machine_id(credential: dict[str, Any], account_id: str = "") -> str:
    explicit = _first(credential, "machineId", "machine_id")
    if explicit:
        return explicit
    seed = account_id or _first(credential, "email") or "kiro-account"
    return hashlib.sha256(f"kiro-device-{seed}".encode("utf-8")).hexdigest()


def build_headers(credential: dict[str, Any], account_id: str = "") -> dict[str, str]:
    device_id = machine_id(credential, account_id)
    platform_name = _platform_tag()
    platform_release = platform.release() or "unknown"
    user_agent = (
        f"aws-sdk-js/{AWS_SDK_VERSION} ua/2.1 os/{platform_name}#{platform_release} "
        f"lang/js md/nodejs#22.22.0 api/codewhispererstreaming#{AWS_SDK_VERSION} "
        f"m/E KiroIDE-{KIRO_VERSION}-{device_id}"
    )
    amz_user_agent = f"aws-sdk-js/{AWS_SDK_VERSION} KiroIDE {KIRO_VERSION} {device_id}"
    return {
        "Authorization": f"Bearer {_first(credential, 'accessToken', 'access_token')}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.amazon.eventstream",
        "X-Amz-Target": AMZ_TARGET,
        "x-amzn-kiro-agent-mode": AGENT_MODE,
        "x-amz-user-agent": amz_user_agent,
        "User-Agent": user_agent,
        "amz-sdk-invocation-id": str(uuid.uuid4()),
        "amz-sdk-request": "attempt=1; max=3",
    }


def _parse_json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def refresh_credential(credential: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    """按 authMethod 刷新 access token，返回需要合并回 credential 的字段。"""
    refresh_token = _first(credential, "refreshToken", "refresh_token")
    if not refresh_token:
        raise GatewayError("Kiro 账号缺少 refreshToken", status_code=401, code="authentication_error")
    auth_method = _text(credential.get("authMethod") or credential.get("auth_method")).lower()
    provider = _text(credential.get("provider"))
    region = _first(credential, "region") or DEFAULT_REGION
    headers = {"Content-Type": "application/json"}
    if auth_method == "social" or provider in {"Github", "Google"}:
        url = SOCIAL_REFRESH_URL
        body: dict[str, Any] = {"refreshToken": refresh_token}
        headers["User-Agent"] = (
            f"aws-sdk-js/{AWS_SDK_VERSION} ua/2.1 os/{_platform_tag()} "
            f"lang/js md/nodejs#22.22.0 api/codewhispererstreaming#{AWS_SDK_VERSION} "
            f"m/E KiroIDE-{KIRO_VERSION}"
        )
    else:
        client_id = _first(credential, "clientId", "client_id")
        client_secret = _first(credential, "clientSecret", "client_secret")
        if not client_id or not client_secret:
            raise GatewayError(
                "Kiro IdC 账号缺少 clientId/clientSecret，无法刷新 access token",
                status_code=401,
                code="authentication_error",
            )
        url = OIDC_TOKEN_URL.format(region=region)
        body = {
            "clientId": client_id,
            "clientSecret": client_secret,
            "refreshToken": refresh_token,
            "grantType": "refresh_token",
        }

    try:
        response = requests.post(url, headers=headers, json=body, timeout=max(5, min(30, int(timeout or 30))))
    except requests.RequestException as error:
        raise GatewayError(f"刷新 Kiro access token 失败: {error}", status_code=401, code="authentication_error") from error
    if not response.ok:
        raise GatewayError(
            f"刷新 Kiro access token 失败: HTTP {response.status_code}",
            status_code=401,
            code="authentication_error",
        )
    data = _parse_json(response)
    access_token = _text(data.get("accessToken"))
    if not access_token:
        raise GatewayError("Kiro 刷新接口没有返回 accessToken", status_code=401, code="authentication_error")
    new_refresh = _text(data.get("refreshToken")) or refresh_token
    return {
        "accessToken": access_token,
        "access_token": access_token,
        "refreshToken": new_refresh,
        "refresh_token": new_refresh,
    }


def _expires_soon(credential: dict[str, Any]) -> bool:
    raw = credential.get("expiresAt") or credential.get("expires_at")
    try:
        expires_at = int(raw)
    except (TypeError, ValueError):
        return False
    if expires_at > 10_000_000_000:
        expires_at //= 1000
    if expires_at <= 0:
        return False
    return expires_at - 120 <= int(time.time())


def prepare_credential(account: PoolAccount, *, force_refresh: bool = False, timeout: int = 30) -> dict[str, Any]:
    """返回可用的凭证副本；缺少 accessToken 或即将过期时用 refreshToken 刷新。"""
    credential = dict(account.credential)
    access_token = _first(credential, "accessToken", "access_token")
    refresh_token = _first(credential, "refreshToken", "refresh_token")
    if not refresh_token:
        if not access_token:
            raise GatewayError("Kiro 账号缺少可用凭证", status_code=401, code="authentication_error")
        return credential
    if access_token and not force_refresh and not _expires_soon(credential):
        return credential
    credential.update(refresh_credential(credential, timeout=timeout))
    now = int(time.time() * 1000)
    credential["expiresAt"] = now + 3600 * 1000
    credential["expires_at"] = now + 3600 * 1000
    return credential


def error_message(response: requests.Response) -> str:
    data = _parse_json(response)
    error = data.get("error")
    if isinstance(error, dict):
        message = _text(error.get("message")) or _text(error.get("code"))
        if message:
            return message
    if isinstance(error, str) and error.strip():
        return error.strip()
    message = _text(data.get("message") or data.get("Message") or data.get("errorMessage"))
    if message:
        return message
    try:
        return _text(response.text)[:500] or f"Kiro 上游返回 HTTP {response.status_code}"
    except Exception:
        return f"Kiro 上游返回 HTTP {response.status_code}"


def raise_for_upstream(response: requests.Response) -> None:
    if response.ok:
        return
    status = int(response.status_code or 502)
    message = error_message(response)
    if status in {401, 403}:
        raise GatewayError(message, status_code=401, upstream_status=401, code="authentication_error")
    if status == 429:
        raise GatewayError(message, status_code=429, upstream_status=429, code="rate_limit_error")
    if status >= 500:
        raise GatewayError(message, status_code=502, upstream_status=status, code="upstream_error")
    raise GatewayError(message, status_code=status, upstream_status=status, code="invalid_request_error")


def request_chat_stream(
    credential: dict[str, Any],
    body: dict[str, Any],
    *,
    timeout: int = 180,
    account_id: str = "",
) -> requests.Response:
    """按 CodeWhisperer → AmazonQ 的顺序请求上游，429 时尝试下一个端点。"""
    headers = build_headers(credential, account_id)
    quota_message = ""
    last_error: GatewayError | None = None
    for url in endpoint_urls(_first(credential, "region") or DEFAULT_REGION):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=body,
                timeout=max(5, int(timeout or 180)),
                stream=True,
            )
        except requests.RequestException as error:
            last_error = GatewayError(
                f"连接 Kiro 上游失败: {error}", status_code=502, code="upstream_error"
            )
            continue
        if response.status_code == 429:
            quota_message = error_message(response)
            response.close()
            continue
        raise_for_upstream(response)
        return response
    if last_error is not None:
        raise last_error
    raise GatewayError(quota_message or "Kiro 上游额度已用尽", status_code=429, upstream_status=429, code="rate_limit_error")


def _unwrap(event: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = event.get(key)
        if isinstance(value, dict):
            return value
    return event


def _event_error(event: dict[str, Any]) -> str:
    direct = _text(event.get("errorMessage"))
    if direct:
        return direct
    if event.get("_type") or event.get("error"):
        error = event.get("error")
        if isinstance(error, dict):
            return _text(error.get("message") or error.get("code")) or "Kiro 上游流式响应返回错误"
        if isinstance(error, str) and error.strip():
            return error.strip()
        return _text(event.get("message")) or "Kiro 上游流式响应返回错误"
    return ""


def _event_text(event: dict[str, Any]) -> str:
    body = _unwrap(event, "assistantResponseEvent", "codeEvent")
    content = body.get("content")
    return content if isinstance(content, str) else ""


def _usage_payload(prompt: int, completion: int, total: int = 0) -> dict[str, int]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total or prompt + completion,
    }


def _usage_from_event(event: dict[str, Any]) -> dict[str, int] | None:
    body = _unwrap(event, "messageMetadataEvent", "metadataEvent", "usageEvent", "usage")
    token_usage = body.get("tokenUsage")
    if isinstance(token_usage, dict):
        prompt = (
            _int(token_usage.get("uncachedInputTokens"))
            + _int(token_usage.get("cacheReadInputTokens"))
            + _int(token_usage.get("cacheWriteInputTokens"))
        )
        completion = _int(token_usage.get("outputTokens"))
        total = _int(token_usage.get("totalTokens"))
        if prompt or completion or total:
            return _usage_payload(prompt, completion, total)
    prompt = _int(body.get("inputTokens"))
    completion = _int(body.get("outputTokens"))
    if prompt or completion:
        return _usage_payload(prompt, completion)
    return None


def _iter_raw_chunks(response: requests.Response) -> Iterator[bytes]:
    if hasattr(response, "iter_content"):
        yield from response.iter_content(chunk_size=8192)
        return
    for line in response.iter_lines(decode_unicode=False):
        if line:
            yield line if isinstance(line, bytes) else str(line).encode("utf-8")


def iter_events(response: requests.Response) -> Iterator[tuple[str, Any]]:
    """解析上游事件流，产出 ("text", 文本) 或 ("usage", usage 字典)。"""
    try:
        for message in eventstream.iter_messages(_iter_raw_chunks(response)):
            try:
                event = message.json()
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            message_error = _event_error(event)
            if message_error:
                raise GatewayError(message_error, status_code=502, code="upstream_error")
            text = _event_text(event)
            if text:
                yield "text", text
            usage = _usage_from_event(event)
            if usage is not None:
                yield "usage", usage
    except eventstream.EventStreamError as error:
        raise GatewayError(f"解析 Kiro 流式响应失败: {error}", status_code=502, code="upstream_error") from error


def collect_stream(response: requests.Response) -> tuple[str, dict[str, int]]:
    """把事件流聚合为完整文本与 usage（供非流式 chat 使用）。"""
    parts: list[str] = []
    usage = _usage_payload(0, 0)
    for kind, value in iter_events(response):
        if kind == "text":
            parts.append(str(value))
        elif kind == "usage":
            usage.update(value)
    return "".join(parts), usage


def completion_payload(text: str, usage: dict[str, int], public_model: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": public_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": _usage_payload(
            _int(usage.get("prompt_tokens")),
            _int(usage.get("completion_tokens")),
            _int(usage.get("total_tokens")),
        ),
    }


def _sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chunk(
    completion_id: str,
    created: int,
    public_model: str,
    delta: dict[str, Any],
    finish_reason: str | None,
) -> str:
    return _sse(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": public_model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
    )


def iter_openai_chunks(
    response: requests.Response,
    *,
    public_model: str,
    usage_out: dict[str, int],
) -> Iterator[str]:
    """把 Kiro 事件流转换为 OpenAI chat.completion.chunk SSE。"""
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    try:
        for kind, value in iter_events(response):
            if kind == "text" and value:
                yield _chunk(completion_id, created, public_model, {"content": str(value)}, None)
            elif kind == "usage":
                usage_out.update(value)
    finally:
        response.close()
    yield _chunk(completion_id, created, public_model, {}, "stop")
    yield "data: [DONE]\n\n"
