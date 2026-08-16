"""Sub2API  sink：把注册产物批量灌入 Sub2API 的管理员账号 API。

接口规格（已读源码确认）：
- 路径：POST {base_url}/api/v1/admin/accounts/batch
- 请求体：{"accounts": [CreateAccountRequest...]}（见 sub2api/backend/internal/handler/admin/account_handler.go:1157）
- 鉴权：x-api-key: <admin api key>
- 响应：兼容顶层结果和当前 {"code": 0, "data": {...}} 包装

约束（与 plan 一致）：
- type 字段受 oneof=oauth setup-token apikey upstream bedrock 限制，没有 cookie。
  本 sink 走 hack 路线 type="apikey"，把 sso JWT 塞 credentials.api_key。
- extra.credential_kind="grok_sso_cookie" 是 Phase C grok-proxy 识别用的钩子。
- 批失败时全量落 fallback 文件，保证不丢账号（一次注册 = 一次真实成本）。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from ..providers.base import RegistrationResult

GROK_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
GROK_CLI_BASE_URL = "https://cli-chat-proxy.grok.com/v1"


def parse_sub2api_group_ids(raw: str) -> list[int]:
    """Parse the comma-separated default group IDs used by Sub2API imports."""
    group_ids: list[int] = []
    for item in str(raw or "").split(","):
        value = item.strip()
        if not value:
            continue
        try:
            group_id = int(value)
        except ValueError as error:
            raise ValueError("SUB2API_DEFAULT_GROUP_IDS 必须是逗号分隔的数字 ID") from error
        if group_id <= 0:
            raise ValueError("SUB2API_DEFAULT_GROUP_IDS 必须使用大于 0 的数字 ID")
        if group_id not in group_ids:
            group_ids.append(group_id)
    return group_ids


def load_sub2api_env_config() -> tuple[str, str, list[int]]:
    """Load and validate the external Sub2API connection settings."""
    base_url = str(os.environ.get("SUB2API_BASE_URL") or "").strip().rstrip("/")
    api_key = str(os.environ.get("SUB2API_ADMIN_API_KEY") or "").strip()
    if not base_url:
        raise ValueError("未配置 SUB2API_BASE_URL")
    if not api_key:
        raise ValueError("未配置 SUB2API_ADMIN_API_KEY")
    group_ids = parse_sub2api_group_ids(
        os.environ.get("SUB2API_DEFAULT_GROUP_IDS", "")
    )
    return base_url, api_key, group_ids


def build_sub2api_oauth_accounts(
    provider: str,
    accounts: list[dict[str, Any]],
    *,
    group_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Map Grok Build OAuth credentials to Sub2API account requests."""
    if provider != "grok_build":
        raise ValueError("Sub2API 格式仅支持 Grok Build OAuth 账号")

    result: list[dict[str, Any]] = []
    for index, account in enumerate(accounts, start=1):
        access_token = _text(account.get("access_token"))
        refresh_token = _text(account.get("refresh_token"))
        if not access_token or not refresh_token:
            raise ValueError(
                f"第 {index} 个账号缺少 access_token 或 refresh_token，无法导入可用的 Sub2API Grok OAuth 账号"
            )

        email = _text(account.get("email"))
        user_id = _text(account.get("user_id") or account.get("userId"))
        credentials: dict[str, str] = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": _text(account.get("token_type")) or "Bearer",
            "client_id": _text(account.get("client_id") or account.get("oidc_client_id"))
            or GROK_OAUTH_CLIENT_ID,
            "base_url": _text(account.get("base_url")) or GROK_CLI_BASE_URL,
        }
        expires_at = normalize_sub2api_rfc3339(
            account.get("expires_at") or account.get("expires_at_raw")
        )
        if expires_at:
            credentials["expires_at"] = expires_at
        for key in (
            "id_token",
            "scope",
            "email",
            "sub",
            "user_id",
            "principal_id",
            "team_id",
            "subscription_tier",
            "entitlement_status",
        ):
            value = _text(account.get(key))
            if value:
                credentials[key] = value
        if email and "email" not in credentials:
            credentials["email"] = email
        if user_id and "user_id" not in credentials:
            credentials["user_id"] = user_id

        item: dict[str, Any] = {
            "name": _text(account.get("name")) or email or f"Grok Build {index}",
            "platform": "grok",
            "type": "oauth",
            "credentials": credentials,
            "concurrency": 1,
            "priority": 0,
            "auto_pause_on_expired": True,
        }
        if group_ids is not None:
            item["group_ids"] = list(group_ids)
            item["confirm_mixed_channel_risk"] = True
        result.append(item)
    return result


def normalize_sub2api_rfc3339(value: object) -> str:
    """Normalize timestamps to the RFC3339 strings stored by Sub2API."""
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
            return (
                parsed.astimezone(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )

    if timestamp <= 0:
        return ""
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def create_sub2api_accounts(
    *,
    base_url: str,
    api_key: str,
    accounts: list[dict[str, Any]],
    timeout: int = 30,
    idempotency_key: str = "",
) -> dict[str, Any]:
    """Create accounts through the Sub2API admin batch endpoint."""
    normalized_base_url = str(base_url or "").strip().rstrip("/")
    normalized_api_key = str(api_key or "").strip()
    if not normalized_base_url:
        raise ValueError("未配置 SUB2API_BASE_URL")
    if not normalized_api_key:
        raise ValueError("未配置 SUB2API_ADMIN_API_KEY")
    if not accounts:
        raise ValueError("没有可导入的 Sub2API 账号")

    headers = {
        "x-api-key": normalized_api_key,
        "Content-Type": "application/json",
    }
    normalized_idempotency_key = str(idempotency_key or "").strip()
    if normalized_idempotency_key:
        headers["Idempotency-Key"] = normalized_idempotency_key

    response = requests.post(
        f"{normalized_base_url}/api/v1/admin/accounts/batch",
        headers=headers,
        json={"accounts": accounts},
        timeout=timeout,
    )
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(f"Sub2API 返回了无效 JSON（HTTP {response.status_code}）") from error

    if not response.ok:
        message = _sub2api_response_message(payload) or f"HTTP {response.status_code}"
        raise RuntimeError(f"Sub2API 请求失败：{message}")
    if not isinstance(payload, dict):
        raise RuntimeError("Sub2API 返回了无效批量导入结果")

    code = payload.get("code")
    if code not in (None, 0, "0"):
        message = _sub2api_response_message(payload) or f"业务状态码 {code}"
        raise RuntimeError(f"Sub2API 请求失败：{message}")

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    success = _non_negative_int(data.get("success"), "success")
    failed = _non_negative_int(data.get("failed"), "failed")
    results = data.get("results")
    if not isinstance(results, list):
        results = []
    if success + failed != len(accounts):
        raise RuntimeError("Sub2API 返回的成功和失败数量与提交账号数不一致")
    return {"success": success, "failed": failed, "results": results}


def _non_negative_int(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Sub2API 返回的 {field} 数量无效") from error
    if result < 0:
        raise RuntimeError(f"Sub2API 返回的 {field} 数量无效")
    return result


def _sub2api_response_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message") or payload.get("error")
    if isinstance(message, dict):
        message = message.get("message") or message.get("error")
    return str(message or "").strip()


def _text(value: object) -> str:
    return str(value or "").strip()


class Sub2ApiSink:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_group_ids: list[int] | None = None,
        batch_size: int = 1,
        fallback_path: str | os.PathLike[str] = "output/sso-failed.txt",
        timeout: int = 30,
    ):
        if not base_url:
            raise ValueError("Sub2ApiSink 需要 base_url（环境变量 SUB2API_BASE_URL）")
        if not api_key:
            raise ValueError("Sub2ApiSink 需要 api_key（环境变量 SUB2API_ADMIN_API_KEY）")

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_group_ids = list(default_group_ids or [])
        self.batch_size = max(1, batch_size)
        self.fallback_path = Path(fallback_path)
        self.timeout = timeout
        self._buf: list[dict[str, Any]] = []

    def push(self, provider_name: str, result: RegistrationResult) -> None:
        self._buf.append(self._build_account(provider_name, result))
        if len(self._buf) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            return

        try:
            payload = create_sub2api_accounts(
                base_url=self.base_url,
                api_key=self.api_key,
                accounts=self._buf,
                timeout=self.timeout,
            )
            success = int(payload.get("success", 0))
            failed = int(payload.get("failed", 0))
            results = payload.get("results", []) or []
            print(f"[*] Sub2API 批量入库：成功 {success}，失败 {failed}（共 {len(self._buf)} 条）")
            if failed:
                self._dump_failed_results(results)
            self._buf.clear()
        except Exception as e:
            print(f"[Error] Sub2API 批量入库失败: {e}，全量落兜底文件")
            self._dump_to_fallback(self._buf)
            self._buf.clear()

    def _build_account(self, provider: str, result: RegistrationResult) -> dict[str, Any]:
        email = result["email"]
        return {
            "name": f"{provider}-{email}",
            "platform": provider,
            "type": "apikey",
            "credentials": {"api_key": result["credential"]},
            "extra": {
                "email": email,
                "profile": result.get("profile") or {},
                "source": "grok-account-manager",
                "credential_kind": f"{provider}_sso_cookie",
            },
            "group_ids": self.default_group_ids,
            "auto_pause_on_expired": True,
            # 跳过 mixed-channel 警告：本工具会持续灌单一 platform，不存在混合风险。
            "confirm_mixed_channel_risk": True,
        }

    def _dump_to_fallback(self, accounts: list[dict[str, Any]]) -> None:
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        with self.fallback_path.open("a", encoding="utf-8") as f:
            for acc in accounts:
                cred = (acc.get("credentials") or {}).get("api_key", "")
                if cred:
                    f.write(cred + "\n")

    def _dump_failed_results(self, results: list[dict[str, Any]]) -> None:
        """部分成功批：把失败条目按 name 反查 buffer 里的对应账号写到 fallback。"""
        failed_names = {r.get("name") for r in results if not r.get("success")}
        if not failed_names:
            return
        failed_accounts = [a for a in self._buf if a.get("name") in failed_names]
        if failed_accounts:
            self._dump_to_fallback(failed_accounts)
            print(f"[Info] {len(failed_accounts)} 条失败账号已写入 {self.fallback_path}")
