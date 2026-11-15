"""ChatGPT 适配器：在账号池之上提供图片生成与图片编辑。"""

from __future__ import annotations

from typing import Any

from ....gateway.catalog import ModelRoute
from ....gateway.errors import GatewayError
from ....gateway.pool import PoolAccount
from . import upstream


class ChatGPTAdapter:
    provider = "chatgpt"
    capabilities = {"image"}

    def _with_refresh(self, call):
        """执行上游调用；遇到 401/403 时强制刷新 token 重试一次。"""
        try:
            return call()
        except GatewayError as error:
            if error.upstream_status not in {401, 403}:
                raise
            return call(force_refresh=True)

    def image(
        self,
        account: PoolAccount,
        route: ModelRoute,
        payload: dict[str, Any],
        *,
        timeout: int = 180,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """返回 (OpenAI images 响应字典, credential)。

        payload 同时兼容生成与编辑：带 image 时走图片编辑（先上传参考图）。
        """
        # 提前校验请求，避免无意义地刷新 token。
        upstream.build_image_plan(route.upstream_model, payload)

        def call(force_refresh: bool = False):
            credential = upstream.prepare_credential(account, force_refresh=force_refresh)
            return upstream.generate_images(
                credential,
                route.upstream_model,
                payload,
                timeout=timeout,
            ), credential

        return self._with_refresh(call)


ADAPTER = ChatGPTAdapter()
