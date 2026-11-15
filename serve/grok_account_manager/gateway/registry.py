"""provider 适配器注册表。"""

from __future__ import annotations

from ..providers.gpt.gateway.adapter import ADAPTER as CHATGPT_ADAPTER
from .errors import GatewayError
from ..providers.grok.gateway.adapter import ADAPTER as GROK_ADAPTER
from ..providers.kiro.gateway.adapter import ADAPTER as KIRO_ADAPTER

_ADAPTERS = {
    "grok": GROK_ADAPTER,
    "chatgpt": CHATGPT_ADAPTER,
    "kiro": KIRO_ADAPTER,
}


def get_adapter(provider: str):
    adapter = _ADAPTERS.get(str(provider or "").strip().lower())
    if adapter is None:
        raise GatewayError(
            f"provider {provider} 的网关尚未启用",
            status_code=501,
            code="provider_not_available",
        )
    return adapter
