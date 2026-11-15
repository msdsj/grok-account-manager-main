"""网关账号池：按 provider/能力选号，并维护失败冷却。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from ..core import database as db
from .errors import GatewayError

_COOLDOWN_SECONDS = {
    401: 300,
    403: 300,
    429: 60,
}
_DEFAULT_COOLDOWN = 30

_PROVIDER_ALIASES = {
    "grok": "grok",
    "xai": "grok",
    "gpt": "chatgpt",
    "chatgpt": "chatgpt",
    "openai": "chatgpt",
    "kiro": "kiro",
    "aws": "kiro",
}

_lock = threading.RLock()
_cooldowns: dict[str, float] = {}
_cursors: dict[str, int] = {}


@dataclass
class PoolAccount:
    export_key: str
    account_id: str
    email: str
    provider: str
    credential: dict[str, Any]
    account: dict[str, Any]


def normalize_provider(provider: str) -> str:
    return _PROVIDER_ALIASES.get(str(provider or "").strip().lower(), str(provider or "").strip().lower())


def _has_token(credential: dict[str, Any], capability: str) -> bool:
    access = str(credential.get("access_token") or credential.get("accessToken") or "").strip()
    refresh = str(credential.get("refresh_token") or credential.get("refreshToken") or "").strip()
    if access or refresh:
        return True
    if capability == "chat":
        return bool(str(credential.get("sso_token") or credential.get("session_token") or "").strip())
    return False


def _to_pool_account(ref: dict[str, Any], provider: str) -> PoolAccount:
    account = ref.get("account") or {}
    credential = ref.get("credential") or {}
    account_id = str(account.get("id") or "").strip()
    if not account_id:
        account_id = str(ref.get("export_key") or "").rsplit(":", 1)[-1]
    return PoolAccount(
        export_key=str(ref.get("export_key") or ""),
        account_id=account_id,
        email=str(account.get("email") or credential.get("email") or ""),
        provider=provider,
        credential=dict(credential),
        account=dict(account),
    )


def list_accounts(provider: str, capability: str = "chat") -> list[PoolAccount]:
    target = normalize_provider(provider)
    accounts: list[PoolAccount] = []
    for ref in db.list_credential_refs():
        account = ref.get("account") or {}
        actual = normalize_provider(str(account.get("provider") or ""))
        if actual != target:
            continue
        if not _has_token(ref.get("credential") or {}, capability):
            continue
        accounts.append(_to_pool_account(ref, target))
    return accounts


def pick(provider: str, capability: str, *, exclude: set[str] | None = None) -> PoolAccount | None:
    """轮询选出一个未冷却账号；全部不可用时返回 None。"""
    excluded = exclude or set()
    now = time.time()
    with _lock:
        accounts = [
            account
            for account in list_accounts(provider, capability)
            if account.export_key not in excluded and _cooldowns.get(account.export_key, 0) <= now
        ]
        if not accounts:
            return None
        cursor_key = f"{normalize_provider(provider)}:{capability}"
        cursor = _cursors.get(cursor_key, 0) % len(accounts)
        _cursors[cursor_key] = cursor + 1
        return accounts[cursor]


def require_account(provider: str, capability: str, *, exclude: set[str] | None = None) -> PoolAccount:
    account = pick(provider, capability, exclude=exclude)
    if account is None:
        raise GatewayError(
            f"{provider} 没有可用账号，请先在账号管理中导入或注册",
            status_code=503,
            code="no_available_account",
        )
    return account


def cool_down(export_key: str, seconds: int | None = None, *, upstream_status: int = 0) -> None:
    duration = seconds if seconds is not None else _COOLDOWN_SECONDS.get(upstream_status, _DEFAULT_COOLDOWN)
    if not export_key:
        return
    with _lock:
        _cooldowns[str(export_key)] = time.time() + max(1, int(duration))


def cooldown_remaining(export_key: str) -> int:
    with _lock:
        until = _cooldowns.get(str(export_key), 0)
    return max(0, int(until - time.time()))


def clear_cooldown(export_key: str) -> None:
    with _lock:
        _cooldowns.pop(str(export_key), None)


def reset_state() -> None:
    """测试辅助：清空轮询游标与冷却状态。"""
    with _lock:
        _cooldowns.clear()
        _cursors.clear()
