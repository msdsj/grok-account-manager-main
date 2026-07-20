"""Sub2API sink：把注册产物批量灌入 Sub2API 的管理员账号 API。

接口规格（已读源码确认）：
- 路径：POST {base_url}/api/v1/admin/accounts/batch
- 请求体：{"accounts": [CreateAccountRequest...]}（见 sub2api/backend/internal/handler/admin/account_handler.go:1157）
- 鉴权：x-api-key: <admin api key>
- 响应：{"success": int, "failed": int, "results": [...]}（部分成功部分失败也是 200）

约束（与 plan 一致）：
- type 字段受 oneof=oauth setup-token apikey upstream bedrock 限制，没有 cookie。
  本 sink 走 hack 路线 type="apikey"，把 sso JWT 塞 credentials.api_key。
- extra.credential_kind="grok_sso_cookie" 是 Phase C grok-proxy 识别用的钩子。
- 批失败时全量落 fallback 文件，保证不丢账号（一次注册 = 一次真实成本）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests

from ..providers.base import RegistrationResult


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

        url = f"{self.base_url}/api/v1/admin/accounts/batch"
        try:
            resp = requests.post(
                url,
                headers={
                    "x-api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                json={"accounts": self._buf},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
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
                "source": "ai_signuper",
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
