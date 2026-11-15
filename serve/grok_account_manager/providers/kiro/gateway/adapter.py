"""Kiro 适配器：在账号池之上提供 OpenAI 兼容的对话能力。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import requests

from ....core import database as db
from ....gateway.catalog import ModelRoute
from ....gateway.errors import GatewayError
from ....gateway.pool import PoolAccount
from . import translator, upstream


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
            provider="kiro",
        )
        if account.account.get("tags") is not None:
            refreshed["tags"] = account.account["tags"]
        db.update_account_credential(account.export_key, refreshed, credential)
    except Exception as error:  # pragma: no cover - 回写失败不影响请求
        print(f"[gateway] 回写 Kiro 刷新后的凭证失败: {error}")


class KiroAdapter:
    provider = "kiro"
    capabilities = {"chat"}

    def chat(
        self,
        account: PoolAccount,
        route: ModelRoute,
        payload: dict[str, Any],
        *,
        timeout: int = 180,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        response, credential = self.open_chat_stream(account, route, payload, timeout=timeout)
        try:
            text, usage = upstream.collect_stream(response)
        finally:
            response.close()
        return upstream.completion_payload(text, usage, route.public_model), credential

    def open_chat_stream(
        self,
        account: PoolAccount,
        route: ModelRoute,
        payload: dict[str, Any],
        *,
        timeout: int = 180,
    ) -> tuple[requests.Response, dict[str, Any]]:
        credential = upstream.prepare_credential(account, timeout=max(5, min(30, int(timeout or 30))))
        provider_name = str(credential.get("provider") or account.account.get("provider") or "")
        profile_arn = translator.resolve_profile_arn(credential, provider_name)
        body = translator.build_payload(route.upstream_model, payload, profile_arn)
        try:
            response = upstream.request_chat_stream(
                credential,
                body,
                timeout=timeout,
                account_id=account.account_id,
            )
        except GatewayError as error:
            if error.upstream_status not in {401, 403}:
                raise
            credential = upstream.prepare_credential(
                account,
                force_refresh=True,
                timeout=max(5, min(30, int(timeout or 30))),
            )
            _persist(account, credential)
            response = upstream.request_chat_stream(
                credential,
                body,
                timeout=timeout,
                account_id=account.account_id,
            )
        return response, credential

    def stream_chunks(
        self,
        response: requests.Response,
        route: ModelRoute,
        usage_out: dict[str, int],
    ) -> Iterator[str]:
        return upstream.iter_openai_chunks(
            response,
            public_model=route.public_model,
            usage_out=usage_out,
        )


ADAPTER = KiroAdapter()
