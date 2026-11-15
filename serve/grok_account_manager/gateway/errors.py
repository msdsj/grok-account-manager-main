"""网关错误类型。"""

from __future__ import annotations

from typing import Any


class GatewayError(Exception):
    """可以直接转换成 OpenAI 风格错误响应的网关异常。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        code: str = "upstream_error",
        upstream_status: int = 0,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = str(message or "网关请求失败")
        self.status_code = int(status_code or 502)
        self.code = str(code or "upstream_error")
        self.upstream_status = int(upstream_status or 0)
        self.detail = detail or {}

    def to_payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"message": self.message, "type": self.code, "code": self.code}
        if self.detail:
            error["detail"] = self.detail
        return {"error": error}
