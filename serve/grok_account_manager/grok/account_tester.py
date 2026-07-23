"""Direct Grok account availability probes."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import time
from typing import Any
from urllib.parse import urlparse

import requests

from .client import OIDC_CLIENT_ID, TOKEN_ENDPOINT, decode_jwt_claims


DEFAULT_BASE_MODELS = (
    "grok-4.20-0309-non-reasoning",
    "grok-4.20-fast",
    "grok-4.20-auto",
    "grok-4.20-expert",
    "grok-4.3-beta",
)
DEFAULT_CLI45_MODELS = ("grok-4.5",)
DEFAULT_GROK45_MODELS = DEFAULT_CLI45_MODELS
DEFAULT_IMAGE_MODELS = (
    "grok-imagine-image",
    "grok-imagine-image-pro",
    "grok-imagine-image-lite",
)
DEFAULT_API_BASE_URL = "https://api.x.ai/v1"
DEFAULT_CLI_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
DEFAULT_BASE_URL = DEFAULT_API_BASE_URL
GROK_CLI_VERSION = "0.2.93"
GROK_UPSTREAM_USER_AGENT = "sub2api-grok/1.0"


def test_grok_account(credential: dict[str, Any], timeout: int = 180) -> tuple[dict[str, Any], dict[str, Any]]:
    """Probe one account with its own xAI OAuth token.

    Returns a public result and a possibly refreshed credential copy.
    """
    timeout = max(5, min(180, int(timeout or 180)))
    updated = dict(credential)
    email = _text(updated.get("email")) or "unknown"
    result: dict[str, Any] = {
        "email": email,
        "baseAvailable": False,
        "chatAvailable": False,
        "cli45Available": False,
        "grok45Available": False,
        "imageAvailable": False,
        "category": "unavailable",
        "baseModel": None,
        "chatModel": None,
        "cli45Model": None,
        "grok45Model": None,
        "imageModel": None,
        "imageSource": None,
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
    if refresh_token and (not access_token or _token_should_refresh(access_token, updated.get("expires_at"))):
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
        result["chatAvailable"] = True
        result["baseModel"] = base_probe["model"]
        result["chatModel"] = base_probe["model"]
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
                result["chatAvailable"] = True
                result["baseModel"] = base_probe["model"]
                result["chatModel"] = base_probe["model"]
        except Exception as error:
            base_probe["message"] = f"刷新后重试失败：{_short_error(error)}"

    cli45_probe = _probe_models(
        updated,
        _configured_models(
            "GROK_ACCOUNT_TEST_CLI45_MODELS",
            DEFAULT_CLI45_MODELS,
            "GROK_ACCOUNT_TEST_GROK45_MODELS",
        ),
        timeout,
    )
    if cli45_probe["ok"]:
        result["cli45Available"] = True
        result["grok45Available"] = True
        result["cli45Model"] = cli45_probe["model"]
        result["grok45Model"] = cli45_probe["model"]

    image_probe = _probe_image_models(
        updated,
        _configured_models("GROK_ACCOUNT_TEST_IMAGE_MODELS", DEFAULT_IMAGE_MODELS),
        timeout,
    )
    if image_probe["ok"]:
        result["imageAvailable"] = True
        result["imageModel"] = image_probe["model"]
        result["imageSource"] = image_probe["source"]

    elapsed_ms = int((time.monotonic() - started) * 1000)
    result["latencyMs"] = elapsed_ms
    if result["cli45Available"]:
        result["category"] = "cli-4.5"
        result["error"] = None
    elif result["baseAvailable"] and result["imageAvailable"]:
        result["category"] = "chat-image"
        result["error"] = None
    elif result["baseAvailable"]:
        result["category"] = "base-only"
        result["error"] = None
    elif result["imageAvailable"]:
        result["category"] = "image-only"
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


def send_grok_chat(
    credential: dict[str, Any],
    *,
    model: str,
    messages: list[dict[str, Any]],
    timeout: int = 180,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Send one non-stream Grok chat probe using the selected local account."""
    timeout = max(5, min(180, int(timeout or 180)))
    updated = prepare_grok_credential_for_test(credential, timeout=timeout, force_refresh=False)
    model = _text(model) or "grok-4.20-auto"
    base_url = _responses_base_url(updated)
    response = _post_responses_chat(updated, base_url, model, messages, timeout)
    if (
        not response.ok
        and _text(updated.get("refresh_token"))
        and _looks_auth_error(response.status_code, _response_error_text(response))
    ):
        updated = prepare_grok_credential_for_test(updated, timeout=timeout, force_refresh=True)
        base_url = _responses_base_url(updated)
        response = _post_responses_chat(updated, base_url, model, messages, timeout)
    if not response.ok:
        raise RuntimeError(_response_error_text(response))
    payload = response.json()
    content = extract_response_text(payload)
    return {
        "model": model,
        "content": content or "模型已响应，但没有返回文本内容",
        "rawId": _text(payload.get("id")) if isinstance(payload, dict) else "",
    }, updated


def generate_grok_image(
    credential: dict[str, Any],
    *,
    model: str,
    prompt: str,
    n: int = 1,
    size: str = "",
    timeout: int = 180,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one Grok Imagine generation using the selected local account."""
    timeout = max(5, min(180, int(timeout or 180)))
    updated = prepare_grok_credential_for_test(credential, timeout=timeout, force_refresh=False)
    model = _normalize_image_model(_text(model) or "grok-imagine-image")
    prompt = _text(prompt)
    if not prompt:
        raise RuntimeError("prompt 不能为空")
    count = max(1, min(4, int(n or 1)))
    base_url = _media_base_url(updated)
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
    }
    if count > 1:
        payload["n"] = count
    # sub2api 转发 Grok Imagine 时会移除 size；官方 Grok media API 对该字段并不稳定。
    _ = size
    response = _post_image_generation(updated, base_url, payload, timeout)
    if (
        not response.ok
        and _text(updated.get("refresh_token"))
        and _looks_auth_error(response.status_code, _response_error_text(response))
    ):
        updated = prepare_grok_credential_for_test(updated, timeout=timeout, force_refresh=True)
        base_url = _media_base_url(updated)
        response = _post_image_generation(updated, base_url, payload, timeout)
    if not response.ok:
        raise RuntimeError(_response_error_text(response))
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("图片接口返回格式异常")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise RuntimeError("图片接口没有返回图片数据")
    return {"model": model, "data": data}, updated


def _post_responses_chat(
    credential: dict[str, Any],
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    timeout: int,
) -> requests.Response:
    return requests.post(
        f"{base_url}/responses",
        headers=_json_headers(credential, base_url, accept="application/json", use_cli_headers=True),
        json={
            "model": model,
            "input": _messages_to_responses_input(messages),
            "max_output_tokens": 768,
            "stream": False,
        },
        timeout=timeout,
    )


def _post_image_generation(
    credential: dict[str, Any],
    base_url: str,
    payload: dict[str, Any],
    timeout: int,
) -> requests.Response:
    return requests.post(
        f"{base_url}/images/generations",
        headers=_json_headers(
            credential,
            base_url,
            accept="application/json",
            use_cli_headers=_is_cli_base_url(base_url),
        ),
        json=payload,
        timeout=timeout,
    )


def prepare_grok_credential_for_test(
    credential: dict[str, Any],
    *,
    timeout: int = 30,
    force_refresh: bool = False,
) -> dict[str, Any]:
    updated = dict(credential)
    access_token = _text(updated.get("access_token"))
    refresh_token = _text(updated.get("refresh_token"))
    if refresh_token and (force_refresh or not access_token or _token_should_refresh(access_token, updated.get("expires_at"))):
        refreshed_tokens = refresh_xai_access_token(
            refresh_token,
            token_endpoint=_text(updated.get("token_endpoint")) or TOKEN_ENDPOINT,
            timeout=min(30, max(5, int(timeout or 30))),
        )
        updated.update(refreshed_tokens)
        access_token = _text(updated.get("access_token"))
    if not access_token:
        raise RuntimeError("缺少 access_token 或 refresh_token 无法刷新")
    return updated


def _probe_models(credential: dict[str, Any], models: list[str], timeout: int) -> dict[str, Any]:
    last_error = ""
    last_status = None
    for model in models:
        probe = _probe_model(credential, model, timeout)
        if probe["ok"]:
            return probe
        last_error = probe.get("message") or last_error
        last_status = probe.get("status")
    return {
        "ok": False,
        "model": None,
        "status": last_status,
        "message": last_error or "没有可测试的 Grok 模型",
    }


def _probe_model(credential: dict[str, Any], model: str, timeout: int) -> dict[str, Any]:
    access_token = _text(credential.get("access_token"))
    if not access_token:
        return {"ok": False, "model": model, "status": None, "message": "缺少 access_token"}
    base_url = _responses_base_url(credential)
    response = requests.post(
        f"{base_url}/responses",
        headers=_json_headers(
            credential,
            base_url,
            accept="application/json",
            use_cli_headers=_is_oauth_credential(credential),
        ),
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


def _probe_image_models(credential: dict[str, Any], preferred_models: list[str], timeout: int) -> dict[str, Any]:
    listed = _list_available_models(credential, timeout)
    if listed["ok"]:
        listed_models = listed.get("models") or []
        for model in preferred_models:
            if model in listed_models:
                return {"ok": True, "model": model, "source": "model-list", "message": "模型列表包含生图模型"}
        for model in listed_models:
            if _is_image_model(model):
                return {"ok": True, "model": model, "source": "model-list", "message": "模型列表包含生图模型"}

    inferred = _infer_image_model(credential, preferred_models)
    if inferred:
        return {"ok": True, "model": inferred["model"], "source": inferred["source"], "message": "根据账号信息推断支持生图"}
    return {
        "ok": False,
        "model": None,
        "source": None,
        "status": listed.get("status"),
        "message": listed.get("message") or "未发现生图能力",
    }


def _list_available_models(credential: dict[str, Any], timeout: int) -> dict[str, Any]:
    access_token = _text(credential.get("access_token"))
    if not access_token:
        return {"ok": False, "models": [], "status": None, "message": "缺少 access_token"}
    base_url = _responses_base_url(credential)
    try:
        response = requests.get(
            f"{base_url}/models",
            headers=_json_headers(
                credential,
                base_url,
                accept="application/json",
                content_type="",
                use_cli_headers=_is_oauth_credential(credential),
            ),
            timeout=min(timeout, 30),
        )
    except Exception as error:
        return {"ok": False, "models": [], "status": None, "message": _short_error(error)}
    if not response.ok:
        return {
            "ok": False,
            "models": [],
            "status": response.status_code,
            "message": _response_error_text(response),
        }
    try:
        payload = response.json()
    except Exception as error:
        return {"ok": False, "models": [], "status": response.status_code, "message": _short_error(error)}
    models = []
    raw_models = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(raw_models, list):
        for item in raw_models:
            if isinstance(item, dict):
                model_id = _text(item.get("id") or item.get("model") or item.get("name"))
            else:
                model_id = _text(item)
            if model_id:
                models.append(model_id)
    return {"ok": True, "models": models, "status": response.status_code, "message": "模型列表获取成功"}


def _infer_image_model(credential: dict[str, Any], preferred_models: list[str]) -> dict[str, str] | None:
    declared_models = _declared_models_from_credential(credential)
    for model in preferred_models:
        if model in declared_models:
            return {"model": model, "source": "credential-models"}
    for model in declared_models:
        if _is_image_model(model):
            return {"model": model, "source": "credential-models"}

    quota = credential.get("quota") if isinstance(credential.get("quota"), dict) else {}
    products = quota.get("products") if isinstance(quota, dict) else []
    if isinstance(products, list):
        for item in products:
            if not isinstance(item, dict):
                continue
            product = _text(item.get("product") or item.get("name") or item.get("id")).lower()
            if "imagine" in product or "image" in product:
                return {"model": preferred_models[0], "source": "quota-products"}

    plan_text = " ".join(
        _text(value)
        for value in (
            credential.get("plan_type"),
            credential.get("subscription_tier"),
            quota.get("subscriptionTier") if isinstance(quota, dict) else "",
        )
        if _text(value)
    ).lower()
    if any(marker in plan_text for marker in ("supergrok", "super", "heavy", "premium", "pro")):
        return {"model": preferred_models[0], "source": "plan-inferred"}
    return None


def _declared_models_from_credential(credential: dict[str, Any]) -> set[str]:
    models: set[str] = set()
    containers = [credential]
    for key in ("quota", "billing_raw", "subscription_raw", "user_raw", "task_usage_raw"):
        value = credential.get(key)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        for key in ("models", "available_models", "availableModels", "modelIds", "model_ids"):
            value = container.get(key)
            if isinstance(value, list):
                for item in value:
                    model_id = _text(
                        (item.get("id") or item.get("model") or item.get("name") or item.get("model_name"))
                        if isinstance(item, dict)
                        else item
                    )
                    if model_id:
                        models.add(model_id)
    return models


def _is_image_model(model: str) -> bool:
    lower = model.lower()
    return "image" in lower or "imagine" in lower


def _responses_base_url(credential: dict[str, Any]) -> str:
    raw = _text(credential.get("base_url"))
    if raw and _parseable_base_url(raw):
        return raw.rstrip("/")
    if _is_oauth_credential(credential):
        return DEFAULT_CLI_BASE_URL
    return DEFAULT_API_BASE_URL


def _media_base_url(credential: dict[str, Any]) -> str:
    base_url = _responses_base_url(credential)
    if _is_cli_base_url(base_url):
        return DEFAULT_API_BASE_URL
    return base_url


def _json_headers(
    credential: dict[str, Any],
    base_url: str,
    *,
    accept: str,
    content_type: str = "application/json",
    use_cli_headers: bool | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_text(credential.get('access_token'))}",
        "Accept": accept,
        "Connection": "Keep-Alive",
    }
    if content_type:
        headers["Content-Type"] = content_type
    if use_cli_headers is None:
        use_cli_headers = _is_oauth_credential(credential) or _is_cli_base_url(base_url)
    if use_cli_headers:
        headers.update(
            {
                "User-Agent": GROK_UPSTREAM_USER_AGENT,
                "X-Grok-Client-Version": GROK_CLI_VERSION,
                "X-Grok-Client-Mode": "interactive",
            }
        )
    return headers


def _is_oauth_credential(credential: dict[str, Any]) -> bool:
    return _text(credential.get("auth_mode")).lower() in {"", "oauth"} or bool(_text(credential.get("refresh_token")))


def _is_cli_base_url(raw: str) -> bool:
    try:
        return urlparse(raw).hostname == "cli-chat-proxy.grok.com"
    except Exception:
        return False


def _parseable_base_url(raw: str) -> bool:
    try:
        parsed = urlparse(raw)
        return bool(parsed.scheme and parsed.netloc)
    except Exception:
        return False


def _normalize_image_model(model: str) -> str:
    if model == "grok-imagine":
        return "grok-imagine-image-quality"
    return model


def _messages_to_responses_input(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = _text(message.get("role")) or "user"
        content = message.get("content")
        if isinstance(content, list):
            text = " ".join(_text(part.get("text") if isinstance(part, dict) else part) for part in content)
        else:
            text = _text(content)
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines) or "Reply with OK."


def extract_response_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    direct = _text(payload.get("output_text"))
    if direct:
        return direct
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
    output = payload.get("output")
    parts: list[str] = []
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text = _text(part.get("text") or part.get("output_text"))
                        if text:
                            parts.append(text)
            text = _text(item.get("text"))
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _configured_models(env_name: str, fallback: tuple[str, ...], *alias_env_names: str) -> list[str]:
    raw = ""
    for name in (env_name, *alias_env_names):
        raw = os.environ.get(name, "")
        if raw.strip():
            break
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
