"""网关请求审计：把每次 /v1 调用写入数据库。"""

from __future__ import annotations

from typing import Any

from ..core import database as db
from .auth import AuthContext
from .catalog import ModelRoute


def summarize_request(payload: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages")
    summary: dict[str, Any] = {
        "model": payload.get("model"),
        "stream": bool(payload.get("stream")),
    }
    if isinstance(messages, list):
        summary["messageCount"] = len(messages)
        last_text = ""
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                last_text = content
            elif isinstance(content, list):
                last_text = " ".join(
                    str(part.get("text") or "") for part in content if isinstance(part, dict)
                )
            if last_text:
                break
        if last_text:
            summary["preview"] = last_text[:200]
    prompt = payload.get("prompt")
    if isinstance(prompt, str):
        summary["prompt"] = prompt[:200]
        summary["promptLength"] = len(prompt)
    if payload.get("n") is not None:
        summary["n"] = payload.get("n")
    if payload.get("size") is not None:
        summary["size"] = payload.get("size")
    return summary


def record(
    *,
    request_id: str,
    auth: AuthContext,
    route: ModelRoute | None,
    account_id: str,
    status: str,
    http_status: int,
    latency_ms: int,
    stream: bool,
    usage: dict[str, int] | None,
    error: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    try:
        db.insert_gateway_request(
            request_id,
            client_key_id=auth.key_id,
            provider=route.provider if route else "",
            model=route.public_model if route else str((payload or {}).get("model") or ""),
            capability=route.capability if route else "",
            account_id=account_id,
            status=status,
            http_status=http_status,
            latency_ms=latency_ms,
            stream=stream,
            prompt_tokens=int((usage or {}).get("prompt_tokens") or 0),
            completion_tokens=int((usage or {}).get("completion_tokens") or 0),
            error=error,
            request=summarize_request(payload or {}),
        )
    except Exception as exc:  # pragma: no cover - 审计失败不能阻断请求
        print(f"[gateway] 写入网关审计失败: {exc}")
