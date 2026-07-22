"""Direct Grok account availability probes."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import time
from typing import Any

import requests

from .client import OIDC_CLIENT_ID, TOKEN_ENDPOINT, decode_jwt_claims


DEFAULT_BASE_MODELS = (
    "grok-4.20-0309-non-reasoning",
    "grok-4.20-fast",
    "grok-4.3",
    "grok-4",
)
DEFAULT_GROK45_MODELS = ("grok-4.5",)
DEFAULT_BASE_URL = "https://api.x.ai/v1"


def test_grok_account(credential: dict[str, Any], timeout: int = 120) -> tuple[dict[str, Any], dict[str, Any]]:
    """Probe one account with its own xAI OAuth token.

    Returns a public result and a possibly refreshed credential copy.
    """
    timeout = max(5, min(120, int(timeout or 120)))
    updated = dict(credential)
    email = _text(updated.get("email")) or "unknown"
    result: dict[str, Any] = {
        "email": email,
        "baseAvailable": False,
        "grok45Available": False,
        "category": "unavailable",
        "baseModel": None,
        "grok45Model": None,
        "latencyMs": None,
        "error": None,
        "testedAt": int(time.time() * 1000),
    }

    access_token = _text(updated.get("access_token"))
    refresh_token = _text(updated.get("refresh_token"))
    if not access_token and not refresh_token:
        result["error"] = "缺少 access_token 和 refresh_token"
        return result, updated

    refreshed = False
    if refresh_token and _token_should_refresh(access_token, updated.get("expires_at")):
        try:
            refreshed_tokens = refresh_xai_access_token(
                refresh_token,
                token_endpoint=_text(updated.get("token_endpoint")) or TOKEN_ENDPOINT,
                timeout=min(30, timeout),
            )
            updated.update(refreshed_tokens)
            access_token = _text(updated.get("access_token"))
            refreshed = True
        except Exception as error:
            if not access_token:
                result["error"] = f"refresh_token 刷新失败：{_short_error(error)}"
                return result, updated

    started = time.monotonic()
    base_probe = _probe_models(updated, _configured_models("GROK_ACCOUNT_TEST_BASE_MODELS", DEFAULT_BASE_MODELS), timeout)
    if base_probe["ok"]:
        result["baseAvailable"] = True
        result["baseModel"] = base_probe["model"]
    elif refresh_token and access_token and not refreshed and _looks_auth_error(base_probe.get("status"), base_probe.get("message")):
        try:
            refreshed_tokens = refresh_xai_access_token(
                refresh_token,
                token_endpoint=_text(updated.get("token_endpoint")) or TOKEN_ENDPOINT,
                timeout=min(30, timeout),
            )
            updated.update(refreshed_tokens)
            base_probe = _probe_models(updated, _configured_models("GROK_ACCOUNT_TEST_BASE_MODELS", DEFAULT_BASE_MODELS), timeout)
            if base_probe["ok"]:
                result["baseAvailable"] = True
                result["baseModel"] = base_probe["model"]
        except Exception as error:
            base_probe["message"] = f"刷新后重试失败：{_short_error(error)}"

    if result["baseAvailable"]:
        grok45_probe = _probe_models(
            updated,
            _configured_models("GROK_ACCOUNT_TEST_GROK45_MODELS", DEFAULT_GROK45_MODELS),
            timeout,
        )
        if grok45_probe["ok"]:
            result["grok45Available"] = True
            result["grok45Model"] = grok45_probe["model"]

    elapsed_ms = int((time.monotonic() - started) * 1000)
    result["latencyMs"] = elapsed_ms
    if result["grok45Available"]:
        result["category"] = "grok-4.5"
        result["error"] = None
    elif result["baseAvailable"]:
        result["category"] = "base-only"
        result["error"] = None
    else:
        result["error"] = base_probe.get("message") or "基础 Grok 模型不可用"
    return result, updated


def refresh_xai_access_token(refresh_token: str, token_endpoint: str = TOKEN_ENDPOINT, timeout: int = 30) -> dict[str, Any]:
    response = requests.post(
        token_endpoint or TOKEN_ENDPOINT,
        data={
            "grant_type": "refresh_token",
            "client_id": OIDC_CLIENT_ID,
            "refresh_token": refresh_token,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=timeout,
    )
    if not response.ok:
        raise RuntimeError(_response_error_text(response))
    data = response.json()
    access_token = _text(data.get("access_token"))
    if not access_token:
        raise RuntimeError("token endpoint 没有返回 access_token")
    now = int(time.time())
    expires_in = _safe_int(data.get("expires_in"), 0)
    result = {
        "access_token": access_token,
        "token_type": _text(data.get("token_type")) or "Bearer",
        "token_endpoint": token_endpoint or TOKEN_ENDPOINT,
        "usage_updated_at": int(time.time() * 1000),
    }
    if _text(data.get("refresh_token")):
        result["refresh_token"] = _text(data.get("refresh_token"))
    if _text(data.get("id_token")):
        result["id_token"] = _text(data.get("id_token"))
    if expires_in > 0:
        expires_at = now + expires_in
        result["expires_at"] = expires_at
        result["expires_at_raw"] = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
    return result


def _probe_models(credential: dict[str, Any], models: list[str], timeout: int) -> dict[str, Any]:
    last_error = ""
    for model in models:
        probe = _probe_model(credential, model, timeout)
        if probe["ok"]:
            return probe
        last_error = probe.get("message") or last_error
    return {"ok": False, "model": None, "message": last_error or "没有可测试的 Grok 模型"}


def _probe_model(credential: dict[str, Any], model: str, timeout: int) -> dict[str, Any]:
    access_token = _text(credential.get("access_token"))
    if not access_token:
        return {"ok": False, "model": model, "status": None, "message": "缺少 access_token"}
    base_url = (_text(credential.get("base_url")) or DEFAULT_BASE_URL).rstrip("/")
    response = requests.post(
        f"{base_url}/responses",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "Keep-Alive",
        },
        json={
            "model": model,
            "input": "Reply with OK.",
            "max_output_tokens": 8,
            "stream": False,
        },
        timeout=timeout,
    )
    if response.ok:
        return {"ok": True, "model": model, "status": response.status_code, "message": "调用成功"}
    return {
        "ok": False,
        "model": model,
        "status": response.status_code,
        "message": _response_error_text(response),
    }


def _configured_models(env_name: str, fallback: tuple[str, ...]) -> list[str]:
    raw = os.environ.get(env_name, "")
    values = [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]
    return values or list(fallback)


def _token_should_refresh(access_token: str | None, expires_at: Any) -> bool:
    expiry = _safe_int(expires_at, 0)
    if not expiry and access_token:
        expiry = _safe_int(decode_jwt_claims(access_token).get("exp"), 0)
    if not expiry:
        return False
    return expiry <= int(time.time()) + 300


def _looks_auth_error(status: Any, message: Any) -> bool:
    text = str(message or "").lower()
    return status in {401, 403} or "unauthorized" in text or "invalid token" in text


def _response_error_text(response: requests.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                return _text(error.get("message")) or _text(error.get("code")) or f"HTTP {response.status_code}"
            return _text(data.get("message")) or _text(error) or f"HTTP {response.status_code}"
    except Exception:
        pass
    return (response.text or f"HTTP {response.status_code}")[:300]


def _short_error(error: Exception) -> str:
    return str(error).replace("\n", " ")[:300]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
