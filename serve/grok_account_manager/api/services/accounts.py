"""Database-backed account storage, export, quota refresh, and health checks."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ...gateway.catalog import ModelRoute, resolve_model
from ...gateway.errors import GatewayError
from ...providers.gpt.gateway.adapter import ADAPTER as CHATGPT_ADAPTER
from ...providers.grok.gateway.adapter import ADAPTER as GROK_ADAPTER
from ...providers.kiro.gateway.adapter import ADAPTER as KIRO_ADAPTER
from ...gateway.pool import PoolAccount, normalize_provider
from ...providers.grok.account_tester import send_grok_chat, test_grok_account
from ...providers.grok.client import build_cockpit_grok_credential
from ...core.paths import ACCOUNT_TEST_RESULTS_PATH, CREDENTIALS_DIR
from ...core import database as account_db

_LEGACY_MIGRATION_META = "legacy_storage_migrated"


def invalidate_accounts_cache() -> None:
    """Kept for callers that used the old file-signature cache."""


def _format_created_at(value) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    seconds = timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))


def account_export_key(file_path: Path, item_index: int) -> str:
    return f"{file_path.name}:{item_index}"


def _stable_export_key(provider: str, credential: dict) -> str:
    identity = "|".join(
        str(credential.get(key) or "")
        for key in ("email", "access_token", "refresh_token", "sso_token", "clientId", "clientSecret")
    )
    digest = hashlib.sha1(f"{provider}:{identity}".encode("utf-8", errors="ignore")).hexdigest()[:20]
    return f"db:{provider}:{digest}"


def account_from_credential(
    credential: dict,
    file_path: Path,
    item_index: int = 0,
    *,
    export_key: str | None = None,
    account_id: str | None = None,
    provider: str | None = None,
) -> dict:
    email = str(credential.get("email") or "").strip()
    first_name = str(credential.get("first_name") or "").strip()
    last_name = str(credential.get("last_name") or "").strip()
    refresh_token = str(credential.get("refresh_token") or credential.get("refreshToken") or "").strip()
    access_token = str(credential.get("access_token") or credential.get("accessToken") or "").strip()
    oauth_status = str(credential.get("oauth_exchange_status") or "").strip()
    if refresh_token:
        oauth_status = "ready"
    elif not oauth_status:
        oauth_status = "unknown"
    created_at = credential.get("created_at") or credential.get("createdAt")
    resolved_provider = str(provider or credential.get("provider") or "").strip().lower()
    if not resolved_provider:
        name = file_path.name.lower()
        resolved_provider = "chatgpt" if "chatgpt" in name else "kiro" if "kiro" in name else "grok"
    resolved_account_id = str(account_id or credential.get("id") or "").strip()
    if not resolved_account_id:
        resolved_account_id = _stable_export_key(resolved_provider, credential).rsplit(":", 1)[-1]
    has_web_access = bool(str(credential.get("sso_token") or "").strip())
    has_build_access = bool(
        refresh_token
        or access_token
        or str(credential.get("auth_mode") or "").lower() in {"oauth", "oidc"}
    )
    providers = [name for name, enabled in (("build", has_build_access), ("web", has_web_access)) if enabled]

    quota = credential.get("quota") or {}
    return {
        "id": resolved_account_id,
        "exportKey": export_key or account_export_key(file_path, item_index),
        "provider": resolved_provider,
        "email": email or "unknown",
        "displayName": " ".join(part for part in [first_name, last_name] if part).strip(),
        "authMode": credential.get("auth_mode") or credential.get("authMode") or "oauth",
        "providers": providers,
        "buildAvailable": has_build_access,
        "webAvailable": has_web_access,
        "planType": credential.get("plan_type") or credential.get("planType") or credential.get("type") or "",
        "hasGrokCodeAccess": credential.get("has_grok_code_access"),
        "userId": credential.get("user_id") or credential.get("userId") or "",
        "createdAt": created_at or 0,
        "createdAtLabel": _format_created_at(created_at),
        "hasRefreshToken": bool(refresh_token),
        "hasAccessToken": bool(access_token),
        "oauthStatus": oauth_status,
        "oauthError": str(credential.get("oauth_exchange_error") or credential.get("oauthError") or "").strip(),
        "fileName": file_path.name,
        "filePath": str(file_path),
        "quota": {
            "frequentUsage": quota.get("frequentUsage"),
            "frequentLimit": quota.get("frequentLimit"),
            "occasionalUsage": quota.get("occasionalUsage"),
            "occasionalLimit": quota.get("occasionalLimit"),
            "weeklyUsed": quota.get("weeklyUsed"),
            "weeklyTotal": quota.get("weeklyTotal"),
            "weeklyLimitPercent": quota.get("weeklyLimitPercent"),
        },
        "usageUpdatedAt": credential.get("usage_updated_at") or 0,
    }


def _merge_account_test_result(account: dict, test_results: dict[str, dict]) -> dict:
    result = test_results.get(str(account.get("exportKey") or ""))
    if not result:
        return account
    account["availability"] = {
        "category": result.get("category") or "unavailable",
        "baseAvailable": bool(result.get("baseAvailable")),
        "chatAvailable": bool(result.get("chatAvailable") or result.get("baseAvailable")),
        "cli45Available": bool(result.get("cli45Available") or result.get("grok45Available")),
        "grok45Available": bool(result.get("grok45Available")),
        "imageAvailable": bool(result.get("imageAvailable")),
        "videoAvailable": bool(result.get("videoAvailable")),
        "baseModel": result.get("baseModel"),
        "chatModel": result.get("chatModel") or result.get("baseModel"),
        "cli45Model": result.get("cli45Model") or result.get("grok45Model"),
        "grok45Model": result.get("grok45Model"),
        "imageModel": result.get("imageModel"),
        "imageSource": result.get("imageSource"),
        "videoModel": result.get("videoModel"),
        "latencyMs": result.get("latencyMs"),
        "error": result.get("error"),
        "testedAt": result.get("testedAt"),
    }
    return account


def list_accounts() -> list[dict]:
    test_results = account_db.list_account_test_results()
    return [_merge_account_test_result(account, test_results) for account in account_db.list_accounts()]


def list_account_tags() -> list[dict]:
    return account_db.list_account_tags()


def update_account_tags(export_keys: list[str], tags: list[str], mode: str = "replace") -> dict:
    return account_db.update_account_tags(export_keys, tags, mode)


def export_credentials(export_keys: list[str]) -> list[dict]:
    selected = [str(key or "").strip() for key in export_keys if str(key or "").strip()]
    if not selected:
        raise ValueError("请选择要导出的账号")
    exported = account_db.export_credentials(selected)
    if not exported:
        raise ValueError("没有找到可导出的账号")
    return exported


def export_all_credentials() -> list[dict]:
    return account_db.export_credentials()


def _persist_updated_ref(ref: dict, updated: dict) -> dict:
    export_key = str(ref.get("export_key") or "")
    account = dict(ref.get("account") or {})
    new_account = account_from_credential(
        updated,
        Path("database"),
        export_key=export_key,
        account_id=str(account.get("id") or ""),
        provider=str(account.get("provider") or "") or None,
    )
    if account.get("tags") is not None:
        new_account["tags"] = account["tags"]
    if export_key:
        account_db.update_account_credential(export_key, new_account, updated)
    return _merge_account_test_result(new_account, account_db.list_account_test_results())


def _mark_account_capability(ref: dict, updates: dict) -> None:
    export_key = str(ref.get("export_key") or "")
    if not export_key:
        return
    credential = ref.get("credential") or {}
    previous = account_db.list_account_test_results().get(export_key, {})
    now_ms = int(time.time() * 1000)
    result = {
        "email": credential.get("email") or previous.get("email") or "unknown",
        "baseAvailable": bool(previous.get("baseAvailable")),
        "chatAvailable": bool(previous.get("chatAvailable") or previous.get("baseAvailable")),
        "cli45Available": bool(previous.get("cli45Available") or previous.get("grok45Available")),
        "grok45Available": bool(previous.get("grok45Available")),
        "imageAvailable": bool(previous.get("imageAvailable")),
        "videoAvailable": bool(previous.get("videoAvailable")),
        "category": previous.get("category") or "unavailable",
        "baseModel": previous.get("baseModel"),
        "chatModel": previous.get("chatModel") or previous.get("baseModel"),
        "cli45Model": previous.get("cli45Model") or previous.get("grok45Model"),
        "grok45Model": previous.get("grok45Model"),
        "imageModel": previous.get("imageModel"),
        "imageSource": previous.get("imageSource"),
        "videoModel": previous.get("videoModel"),
        "latencyMs": previous.get("latencyMs"),
        "error": None,
        "testedAt": now_ms,
        "exportKey": export_key,
        "id": str(credential.get("id") or "").strip() or str(ref.get("account", {}).get("id") or ""),
        "fileName": "database",
    }
    result.update(updates)
    if result.get("cli45Available"):
        result["category"] = "cli-4.5"
    elif result.get("chatAvailable") and result.get("imageAvailable"):
        result["category"] = "chat-image"
    elif result.get("chatAvailable") or result.get("baseAvailable"):
        result["category"] = "base-only"
    elif result.get("imageAvailable"):
        result["category"] = "image-only"
    elif result.get("videoAvailable"):
        result["category"] = "video-only"
    else:
        result["category"] = "unavailable"
    result["testedAt"] = now_ms
    account_db.upsert_account_test_result(result)


def _is_cli_chat_model(model: str) -> bool:
    return str(model or "").strip().lower() in {"grok-4.5", "grok4.5", "grok-45"}


def _image_error_message(error: Exception) -> str:
    text = str(error).strip() or "未知错误"
    if "internal server error" in text.lower():
        return "图片生成失败：上游返回内部错误，请检查账号状态或代理"
    return f"图片生成失败：{text}"


def _account_provider(ref: dict) -> str:
    account = ref.get("account") or {}
    credential = ref.get("credential") or {}
    provider = account.get("provider") or credential.get("provider") or "grok"
    return normalize_provider(str(provider))


def _pool_account(ref: dict, provider: str) -> PoolAccount:
    account = dict(ref.get("account") or {})
    credential = dict(ref.get("credential") or {})
    export_key = str(ref.get("export_key") or "")
    account_id = str(account.get("id") or "").strip() or export_key.rsplit(":", 1)[-1]
    return PoolAccount(
        export_key=export_key,
        account_id=account_id,
        email=str(account.get("email") or credential.get("email") or ""),
        provider=provider,
        credential=credential,
        account=account,
    )


_DEFAULT_TEST_MODELS = {
    ("grok", "chat"): "grok-4.5",
    ("grok", "image"): "grok-imagine-image",
    ("grok", "video"): "grok-imagine-video-1.5",
    ("kiro", "chat"): "kiro-auto",
    ("chatgpt", "image"): "gpt-image-1",
}


def _test_route(provider: str, model: str, capability: str) -> ModelRoute:
    name = str(model or "").strip() or _DEFAULT_TEST_MODELS.get((provider, capability), "")
    if not name:
        raise ValueError(f"{provider} 暂不支持 {capability} 测试")
    try:
        route = resolve_model(name)
    except GatewayError:
        route = None
    if route is None or route.provider != provider or route.capability != capability:
        return ModelRoute(
            public_model=name,
            provider=provider,
            upstream_model=name,
            capability=capability,
            config={},
        )
    return route


def test_selected_accounts(export_keys: list[str], timeout: int = 180) -> dict:
    refs = account_db.get_credential_refs(export_keys)
    if not refs:
        raise ValueError("请选择要测试的账号")

    results: list[dict] = []
    for ref in refs:
        export_key = ref["export_key"]
        credential = ref["credential"]
        if _account_provider(ref) != "grok":
            result = {
                "exportKey": export_key,
                "id": str(ref.get("account", {}).get("id") or ""),
                "email": str(ref.get("account", {}).get("email") or ""),
                "fileName": "database",
                "category": "unsupported",
                "error": "批量能力测试仅支持 Grok 账号，请在账号列表中使用单项测试",
                "testedAt": int(time.time() * 1000),
            }
            results.append(result)
            account_db.upsert_account_test_result(result)
            continue
        result, updated = test_grok_account(dict(credential), timeout=timeout)
        result["exportKey"] = export_key
        result["id"] = str(credential.get("id") or "").strip() or str(ref.get("account", {}).get("id") or "")
        result["fileName"] = "database"
        results.append(result)
        account_db.upsert_account_test_result(result)

        if updated != credential:
            _persist_updated_ref(ref, updated)

    invalidate_accounts_cache()
    return {"results": results, "accounts": list_accounts()}


def _extract_chat_content(result: dict) -> str:
    choices = result.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message_payload = choices[0].get("message")
        if isinstance(message_payload, dict):
            return str(message_payload.get("content") or "")
    return ""


def test_account_chat(export_key: str, model: str, messages: list[dict], timeout: int = 180) -> dict:
    refs = account_db.get_credential_refs([export_key])
    if not refs:
        raise ValueError("请选择要测试的账号")
    ref = refs[0]
    provider = _account_provider(ref)
    if provider == "chatgpt":
        raise ValueError("GPT 账号暂不支持对话测试，请切换到生图测试")
    if provider not in {"grok", "kiro"}:
        raise ValueError(f"{provider} 账号暂不支持对话测试")
    model = str(model or "").strip()
    started = time.monotonic()

    if provider == "grok" and _is_cli_chat_model(model):
        result, updated = send_grok_chat(ref["credential"], model="grok-4.5", messages=messages, timeout=timeout)
        account_view = ref.get("account")
        if updated != ref["credential"]:
            account_view = _persist_updated_ref(ref, updated)
            ref["credential"] = updated
            ref["account"] = account_view
        _mark_account_capability(
            ref,
            {
                "baseAvailable": True,
                "chatAvailable": True,
                "cli45Available": True,
                "grok45Available": True,
                "baseModel": result["model"],
                "chatModel": result["model"],
                "cli45Model": result["model"],
                "grok45Model": result["model"],
                "latencyMs": int((time.monotonic() - started) * 1000),
            },
        )
        return {
            "provider": provider,
            "model": result["model"],
            "message": {"role": "assistant", "content": result["content"]},
            "account": account_view,
            "accounts": list_accounts(),
        }

    route = _test_route(provider, model, "chat")
    adapter = KIRO_ADAPTER if provider == "kiro" else GROK_ADAPTER
    try:
        result, updated = adapter.chat(
            _pool_account(ref, provider),
            route,
            {"messages": messages},
            timeout=timeout,
        )
    except GatewayError as error:
        message = f"对话测试失败：{error.message}"
        _mark_account_capability(
            ref,
            {
                "baseAvailable": False,
                "chatAvailable": False,
                "error": message,
                "latencyMs": int((time.monotonic() - started) * 1000),
            },
        )
        raise ValueError(message) from error
    account_view = ref.get("account")
    if updated != ref["credential"]:
        account_view = _persist_updated_ref(ref, updated)
        ref["credential"] = updated
        ref["account"] = account_view
    content = _extract_chat_content(result)
    _mark_account_capability(
        ref,
        {
            "baseAvailable": True,
            "chatAvailable": True,
            "baseModel": result.get("model") or route.public_model,
            "chatModel": result.get("model") or route.public_model,
            "error": None,
            "latencyMs": int((time.monotonic() - started) * 1000),
        },
    )
    return {
        "provider": provider,
        "model": result.get("model") or route.public_model,
        "message": {"role": "assistant", "content": content},
        "account": account_view,
        "accounts": list_accounts(),
    }


def test_account_image(export_key: str, model: str, prompt: str, n: int, size: str, timeout: int = 180) -> dict:
    refs = account_db.get_credential_refs([export_key])
    if not refs:
        raise ValueError("请选择要测试的账号")
    ref = refs[0]
    provider = _account_provider(ref)
    if provider == "kiro":
        raise ValueError("Kiro 账号暂不支持生图测试，请切换到对话测试")
    if provider not in {"grok", "chatgpt"}:
        raise ValueError(f"{provider} 账号暂不支持生图测试")
    started = time.monotonic()
    route = _test_route(provider, str(model or "").strip(), "image")
    adapter = CHATGPT_ADAPTER if provider == "chatgpt" else GROK_ADAPTER
    try:
        result, updated = adapter.image(
            _pool_account(ref, provider),
            route,
            {"prompt": prompt, "n": n, "size": size},
            timeout=timeout,
        )
    except GatewayError as error:
        message = _image_error_message(error)
        _mark_account_capability(
            ref,
            {
                "imageAvailable": False,
                "imageModel": None,
                "imageSource": None,
                "error": message,
                "latencyMs": int((time.monotonic() - started) * 1000),
            },
        )
        raise ValueError(message) from error
    account_view = ref.get("account")
    if updated != ref["credential"]:
        account_view = _persist_updated_ref(ref, updated)
        ref["credential"] = updated
        ref["account"] = account_view
    _mark_account_capability(
        ref,
        {
            "imageAvailable": True,
            "imageModel": route.public_model,
            "imageSource": f"{provider}-gateway",
            "error": None,
            "latencyMs": int((time.monotonic() - started) * 1000),
        },
    )
    return {
        "provider": provider,
        "model": route.public_model,
        "data": result.get("data") or [],
        "account": account_view,
        "accounts": list_accounts(),
    }


def test_account_video(
    export_key: str,
    model: str,
    prompt: str,
    duration: int,
    resolution: str,
    aspect_ratio: str,
    timeout: int = 180,
) -> dict:
    refs = account_db.get_credential_refs([export_key])
    if not refs:
        raise ValueError("请选择要测试的账号")
    ref = refs[0]
    provider = _account_provider(ref)
    if provider != "grok":
        raise ValueError("仅 Grok 账号支持视频生成测试")
    started = time.monotonic()
    route = _test_route(provider, str(model or "").strip(), "video")
    payload = {
        "prompt": str(prompt or "").strip(),
        "duration": duration,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
    }
    try:
        result, updated = GROK_ADAPTER.video(
            _pool_account(ref, provider),
            route,
            payload,
            timeout=timeout,
        )
    except GatewayError as error:
        message = f"视频生成失败：{error.message}"
        _mark_account_capability(
            ref,
            {
                "videoAvailable": False,
                "videoModel": None,
                "error": message,
                "latencyMs": int((time.monotonic() - started) * 1000),
            },
        )
        raise ValueError(message) from error
    account_view = ref.get("account")
    if updated != ref["credential"]:
        account_view = _persist_updated_ref(ref, updated)
        ref["credential"] = updated
        ref["account"] = account_view
    _mark_account_capability(
        ref,
        {
            "videoAvailable": True,
            "videoModel": route.public_model,
            "error": None,
            "latencyMs": int((time.monotonic() - started) * 1000),
        },
    )
    return {
        "provider": provider,
        "model": route.public_model,
        "video": result,
        "account": account_view,
        "accounts": list_accounts(),
    }


def test_account_video_status(export_key: str, video_id: str, timeout: int = 180) -> dict:
    refs = account_db.get_credential_refs([export_key])
    if not refs:
        raise ValueError("请选择要测试的账号")
    ref = refs[0]
    provider = _account_provider(ref)
    if provider != "grok":
        raise ValueError("仅 Grok 账号支持视频任务查询")
    video_id = str(video_id or "").strip()
    if not video_id:
        raise ValueError("缺少 videoId")
    try:
        result, updated = GROK_ADAPTER.video_status(
            _pool_account(ref, provider),
            video_id,
            timeout=timeout,
        )
    except GatewayError as error:
        raise ValueError(f"视频状态查询失败：{error.message}") from error
    account_view = ref.get("account")
    if updated != ref["credential"]:
        account_view = _persist_updated_ref(ref, updated)
    return {
        "provider": provider,
        "video": result,
        "account": account_view,
        "accounts": list_accounts(),
    }


def delete_accounts(export_keys: list[str]) -> dict:
    deleted = account_db.hard_delete_accounts(export_keys)
    invalidate_accounts_cache()
    return {"deleted": deleted, "accounts": list_accounts()}


def delete_accounts_for_upstream_accounts(upstream_accounts: list[dict]) -> dict:
    """Remove local database rows that correspond to deleted gateway accounts."""
    emails = {
        str(item.get("email") or "").strip().lower()
        for item in upstream_accounts
        if isinstance(item, dict) and str(item.get("email") or "").strip()
    }
    user_ids = {
        str(item.get("userId") or item.get("user_id") or "").strip()
        for item in upstream_accounts
        if isinstance(item, dict) and str(item.get("userId") or item.get("user_id") or "").strip()
    }
    keys = []
    for account in account_db.list_accounts():
        email = str(account.get("email") or "").strip().lower()
        user_id = str(account.get("userId") or "").strip()
        if (email and email in emails) or (user_id and user_id in user_ids):
            keys.append(str(account.get("exportKey") or ""))
    deleted = account_db.hard_delete_accounts([key for key in keys if key])
    invalidate_accounts_cache()
    return {"removed": deleted, "matched": len(emails | user_ids)}


def refresh_account_quota(account_id: str) -> dict:
    """Refresh one stored account quota using its current access token."""
    from ...providers.grok.account_tester import refresh_xai_access_token
    from ...providers.grok.client import fetch_complete_credential

    ref = account_db.find_account_ref_by_id(account_id)
    if not ref:
        raise ValueError(f"未找到 accountId={account_id} 的账号")

    item = dict(ref["credential"])
    access_token = str(item.get("access_token") or "").strip()
    refresh_token = str(item.get("refresh_token") or "").strip()
    refreshed_tokens: dict | None = None
    if refresh_token:
        try:
            refreshed_tokens = refresh_xai_access_token(
                refresh_token,
                token_endpoint=str(item.get("token_endpoint") or ""),
                timeout=30,
            )
            item.update(refreshed_tokens)
            access_token = str(item.get("access_token") or "").strip()
        except Exception as error:
            if not access_token:
                raise ValueError(f"刷新 access_token 失败：{error}") from error
    if not access_token:
        raise ValueError("该账号没有 access_token 或可用 refresh_token，无法刷新额度")
    email = str(item.get("email") or "").strip()
    updated = fetch_complete_credential(
        email=email,
        sso_token=access_token,
        profile=None,
        oauth_tokens=refreshed_tokens,
    )
    for keep_key in (
        "id",
        "created_at",
        "refresh_token",
        "id_token",
        "expires_at",
        "expires_at_raw",
        "auth_raw",
    ):
        if item.get(keep_key) is not None and updated.get(keep_key) is None:
            updated[keep_key] = item[keep_key]
    account = account_from_credential(
        updated,
        Path("database"),
        export_key=str(ref.get("export_key") or ""),
        account_id=str(ref.get("account", {}).get("id") or ""),
        provider=str(ref.get("account", {}).get("provider") or "") or None,
    )
    if ref.get("account", {}).get("tags") is not None:
        account["tags"] = ref["account"]["tags"]
    account_db.update_account_credential(str(ref["export_key"]), account, updated)
    invalidate_accounts_cache()
    return {"account": _merge_account_test_result(account, account_db.list_account_test_results())}


def persist_registration_result(provider_name: str, result: dict) -> dict:
    """Convert one registration result into the database account record."""
    normalized = str(provider_name or "").strip().lower()
    oauth_status = str(result.get("oauth_status") or "not_requested")
    if oauth_status in {"pending", "failed"}:
        raise ValueError("refresh_token 尚未成功获取，拒绝写入账号数据库")

    if "full_credential" in result:
        credential = dict(result["full_credential"])
    elif normalized == "chatgpt":
        session_data = result.get("profile", {}).get("session_data", {})
        credential = {
            "email": result["email"],
            "accessToken": session_data.get("accessToken") or result["credential"],
            "provider": "chatgpt",
        }
    elif normalized == "kiro":
        credential = dict(result.get("full_credential") or {})
        credential.setdefault("email", result.get("email"))
        credential.setdefault("provider", "kiro")
    else:
        credential = build_cockpit_grok_credential(
            email=result["email"],
            access_token=result["credential"],
            profile=result.get("profile"),
        )
        credential["sso_token"] = result["credential"]

    if normalized == "grok" and oauth_status == "ready" and not str(credential.get("refresh_token") or "").strip():
        raise ValueError("OAuth 状态为 ready，但完整凭证缺少 refresh_token")

    credential["oauth_exchange_status"] = oauth_status
    credential["oauth_exchange_error"] = result.get("oauth_error") or credential.get("oauth_exchange_error") or None
    credential["credential_enrichment_error"] = (
        result.get("credential_enrichment_error") or credential.get("credential_enrichment_error") or None
    )
    credential.setdefault("provider", normalized)
    export_key = _stable_export_key(normalized, credential)
    account = account_from_credential(
        credential,
        Path("database"),
        export_key=export_key,
        account_id=export_key.rsplit(":", 1)[-1],
        provider=normalized,
    )
    account_db.upsert_account(account, credential, Path("database"), 0)
    invalidate_accounts_cache()
    return _merge_account_test_result(account, account_db.list_account_test_results())


def migrate_legacy_storage_once() -> None:
    """Import legacy JSON credentials and test results a single time."""
    account_db.cleanup_removed_providers()
    if account_db.get_meta(_LEGACY_MIGRATION_META) == "1":
        return
    try:
        if not account_db.list_accounts() and CREDENTIALS_DIR.exists():
            for file_path in sorted(CREDENTIALS_DIR.glob("*.json")):
                name = file_path.name.lower()
                if "trae" in name:
                    continue
                provider = "chatgpt" if "chatgpt" in name else "kiro" if "kiro" in name else "grok"
                try:
                    items = json.loads(file_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                for index, item in enumerate(items if isinstance(items, list) else []):
                    if not isinstance(item, dict):
                        continue
                    export_key = _stable_export_key(provider, item)
                    account = account_from_credential(
                        item,
                        file_path,
                        index,
                        export_key=export_key,
                        account_id=export_key.rsplit(":", 1)[-1],
                        provider=provider,
                    )
                    account_db.upsert_account(account, item, file_path, index)

        if ACCOUNT_TEST_RESULTS_PATH.exists():
            try:
                legacy_results = json.loads(ACCOUNT_TEST_RESULTS_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                legacy_results = []
            if isinstance(legacy_results, list) and not account_db.list_account_test_results():
                for item in legacy_results:
                    if isinstance(item, dict):
                        account_db.upsert_account_test_result(item)
    finally:
        account_db.set_meta(_LEGACY_MIGRATION_META, "1")
