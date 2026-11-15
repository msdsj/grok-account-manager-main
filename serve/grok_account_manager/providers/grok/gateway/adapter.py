"""Grok 适配器：在账号池之上提供对话与图片调用。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import requests

from ....gateway.catalog import ModelRoute
from .credentials import prepare_credentials
from ....gateway.errors import GatewayError
from ....gateway.pool import PoolAccount
from . import upstream, video


class GrokAdapter:
    provider = "grok"
    capabilities = {"chat", "image", "video"}

    def _with_refresh(self, call):
        """执行上游调用；遇到 401/403 时强制刷新 token 重试一次。"""
        try:
            return call()
        except GatewayError as error:
            if error.upstream_status not in {401, 403}:
                raise
            return call(force_refresh=True)

    def chat(
        self,
        account: PoolAccount,
        route: ModelRoute,
        payload: dict[str, Any],
        *,
        timeout: int = 180,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        body = upstream.build_chat_body(route.upstream_model, payload, stream=False)

        def call(force_refresh: bool = False):
            credential = prepare_credentials(account, force_refresh=force_refresh)
            return upstream.request_chat(credential, body, timeout=timeout, stream=False), credential

        response, credential = self._with_refresh(call)
        try:
            data = response.json()
        except Exception as error:
            raise GatewayError(f"上游响应解析失败: {error}", status_code=502, code="upstream_error") from error
        if not isinstance(data, dict):
            raise GatewayError("上游返回格式异常", status_code=502, code="upstream_error")
        return upstream.completion_payload(data, route.public_model), credential

    def open_chat_stream(
        self,
        account: PoolAccount,
        route: ModelRoute,
        payload: dict[str, Any],
        *,
        timeout: int = 180,
    ) -> tuple[requests.Response, dict[str, Any]]:
        body = upstream.build_chat_body(route.upstream_model, payload, stream=True)

        def call(force_refresh: bool = False):
            credential = prepare_credentials(account, force_refresh=force_refresh)
            return upstream.request_chat(credential, body, timeout=timeout, stream=True), credential

        return self._with_refresh(call)

    def stream_chunks(
        self,
        response: requests.Response,
        route: ModelRoute,
        usage_out: dict[str, int],
    ) -> Iterator[str]:
        return upstream.iter_openai_chunks(response, public_model=route.public_model, usage_out=usage_out)

    def image(
        self,
        account: PoolAccount,
        route: ModelRoute,
        payload: dict[str, Any],
        *,
        timeout: int = 180,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        body = upstream.build_image_body(route.upstream_model, payload)

        def call(force_refresh: bool = False):
            credential = prepare_credentials(account, force_refresh=force_refresh)
            return upstream.request_image(credential, body, timeout=timeout), credential

        response, credential = self._with_refresh(call)
        try:
            data = response.json()
        except Exception as error:
            raise GatewayError(f"上游响应解析失败: {error}", status_code=502, code="upstream_error") from error
        if not isinstance(data, dict):
            raise GatewayError("上游返回格式异常", status_code=502, code="upstream_error")
        return upstream.image_payload(data), credential

    def video(
        self,
        account: PoolAccount,
        route: ModelRoute,
        payload: dict[str, Any],
        *,
        timeout: int = 180,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        body = video.build_video_body(route.upstream_model, payload)

        def call(force_refresh: bool = False):
            credential = prepare_credentials(account, force_refresh=force_refresh)
            return video.request_video(credential, body, timeout=timeout), credential

        return self._with_refresh(call)

    def video_status(
        self,
        account: PoolAccount,
        video_id: str,
        *,
        timeout: int = 180,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        def call(force_refresh: bool = False):
            credential = prepare_credentials(account, force_refresh=force_refresh)
            return video.query_video(credential, video_id, timeout=timeout), credential

        return self._with_refresh(call)


ADAPTER = GrokAdapter()
