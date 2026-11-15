"""统一网关：对外提供 OpenAI 兼容的 /v1 接口，内部按 provider 路由到不同上游。"""

from .errors import GatewayError

__all__ = ["GatewayError"]
