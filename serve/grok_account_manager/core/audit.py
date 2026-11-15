"""Persistent operation, job and system logs."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from . import database as db

_SENSITIVE_KEYS = {
    "password",
    "token",
    "access_token",
    "refresh_token",
    "sso",
    "sso_token",
    "api_key",
    "apikey",
    "secret",
    "authorization",
    "cookie",
}


def actor_from_request(request: Request | None) -> str:
    if request is None:
        return "admin"
    for header in ("x-actor", "x-operator", "x-user"):
        value = str(request.headers.get(header) or "").strip()
        if value:
            return value[:128]
    return "admin"


def _redact_value(key: str, value: Any) -> Any:
    lowered = str(key or "").lower()
    if any(marker in lowered for marker in _SENSITIVE_KEYS):
        return "***"
    if isinstance(value, dict):
        return {child_key: _redact_value(child_key, child_value) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, item) for item in value]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "..."
    return value


def redact_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(detail, dict):
        return {}
    return {str(key): _redact_value(str(key), value) for key, value in detail.items()}


def log_operation(
    actor: str,
    action: str,
    *,
    target_type: str = "",
    target_id: str = "",
    detail: dict[str, Any] | None = None,
    result: str = "success",
) -> None:
    try:
        db.insert_operation_log(
            actor=actor or "admin",
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=redact_detail(detail),
            result=result,
        )
    except Exception as error:  # pragma: no cover - logging must never break the API
        print(f"[audit] 写入操作日志失败: {error}")


def log_system(level: str, source: str, message: str, *, detail: dict[str, Any] | None = None) -> None:
    try:
        db.insert_system_log(level, source, message, detail=redact_detail(detail))
    except Exception as error:  # pragma: no cover
        print(f"[audit] 写入系统日志失败: {error}")


def log_job_event(
    job_id: str,
    level: str,
    message: str,
    *,
    stage: str = "",
    worker: int = 0,
    payload: dict[str, Any] | None = None,
) -> None:
    try:
        db.insert_job_log(
            job_id,
            level,
            message,
            stage=stage,
            worker=worker,
            payload=redact_detail(payload),
        )
    except Exception as error:  # pragma: no cover
        print(f"[audit] 写入任务日志失败: {error}")
