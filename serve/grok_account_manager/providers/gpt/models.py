"""GPT 板块的默认图片模型配置。

内部 provider id 继续使用 `chatgpt`，与账号数据库和上游命名保持一致。
"""

from __future__ import annotations

DEFAULT_MODELS: list[dict[str, str]] = [
    {
        "publicModel": "gpt-image-1",
        "provider": "chatgpt",
        "upstreamModel": "gpt-image-1",
        "capability": "image",
    },
    {
        "publicModel": "gpt-image-1-mini",
        "provider": "chatgpt",
        "upstreamModel": "gpt-image-1-mini",
        "capability": "image",
    },
    {
        "publicModel": "gpt-image-2",
        "provider": "chatgpt",
        "upstreamModel": "gpt-image-2",
        "capability": "image",
    },
]
