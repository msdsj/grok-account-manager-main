"""网关凭证准备：按需刷新 access token 并回写数据库。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..account_tester import refresh_xai_access_token
from ..client import TOKEN_ENDPOINT, decode_jwt_claims
from ....core import database as db
from ....gateway.errors import GatewayError
from ....gateway.pool import PoolAccount, normalize_provider

_REFRESH_SKEW_SECONDS = 120


def _token(value: Any) -> str:
    return str(value or "").strip()


def _expires_soon(credential: dict[str, Any]) -> bool:
    raw = credential.get("expires_at")
    try:
        expires_at = int(raw)
    except (TypeError, ValueError):
        expires_at = 0
    if not expires_at:
        access_token = _token(credential.get("access_token"))
        if access_token:
            try:
                expires_at = int(decode_jwt_claims(access_token).get("exp") or 0)
            except Exception:
                expires_at = 0
    if expires_at > 10_000_000_000:  # 兼容毫秒时间戳
        expires_at //= 1000
    if expires_at <= 0:
        return False
    return expires_at - _REFRESH_SKEW_SECONDS <= int(time.time())


def _persist(account: PoolAccount, credential: dict[str, Any]) -> None:
    if not account.export_key:
        return
    try:
        from ....api.services.accounts import account_from_credential

        refreshed = account_from_credential(
            credential,
            Path("database"),
            export_key=account.export_key,
            account_id=str(account.account.get("id") or ""),
            provider=account.provider,
        )
        if account.account.get("tags") is not None:
            refreshed["tags"] = account.account["tags"]
        db.update_account_credential(account.export_key, refreshed, credential)
    except Exception as error:  # pragma: no cover - 回写失败不应该阻断请求
        print(f"[gateway] 回写刷新后的凭证失败: {error}")


def prepare_credentials(account: PoolAccount, *, force_refresh: bool = False, timeout: int = 30) -> dict[str, Any]:
    """返回可直接用于上游请求的凭证副本；Grok 账号会按需刷新 token。"""
    credential = dict(account.credential)
    if normalize_provider(account.provider) != "grok":
        return credential
    access_token = _token(credential.get("access_token"))
    refresh_token = _token(credential.get("refresh_token"))
    if not refresh_token:
        if not access_token:
            raise GatewayError("账号缺少可用凭证", status_code=401, code="authentication_error")
        return credential
    if not force_refresh and access_token and not _expires_soon(credential):
        return credential
    try:
        tokens = refresh_xai_access_token(
            refresh_token,
            token_endpoint=_token(credential.get("token_endpoint")) or TOKEN_ENDPOINT,
            timeout=max(5, min(30, int(timeout or 30))),
        )
    except Exception as error:
        raise GatewayError(
            f"刷新 Grok access token 失败: {error}",
            status_code=401,
            code="authentication_error",
        ) from error
    credential.update(tokens)
    _persist(account, credential)
    return credential
