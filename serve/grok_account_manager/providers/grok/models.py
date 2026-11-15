"""Grok 板块的默认模型与能力配置。"""

from __future__ import annotations

DEFAULT_MODELS: list[dict[str, str]] = [
    {"publicModel": "grok-4.20-auto", "provider": "grok", "upstreamModel": "grok-4.20-auto", "capability": "chat"},
    {"publicModel": "grok-4.20-fast", "provider": "grok", "upstreamModel": "grok-4.20-fast", "capability": "chat"},
    {"publicModel": "grok-4.20-expert", "provider": "grok", "upstreamModel": "grok-4.20-expert", "capability": "chat"},
    {
        "publicModel": "grok-4.20-0309-non-reasoning",
        "provider": "grok",
        "upstreamModel": "grok-4.20-0309-non-reasoning",
        "capability": "chat",
    },
    {
        "publicModel": "grok-4.20-0309-reasoning",
        "provider": "grok",
        "upstreamModel": "grok-4.20-0309-reasoning",
        "capability": "chat",
    },
    {"publicModel": "grok-4.3-beta", "provider": "grok", "upstreamModel": "grok-4.3-beta", "capability": "chat"},
    {"publicModel": "grok-4.5", "provider": "grok", "upstreamModel": "grok-4.5", "capability": "chat"},
    {
        "publicModel": "grok-imagine-image",
        "provider": "grok",
        "upstreamModel": "grok-imagine-image",
        "capability": "image",
    },
    {
        "publicModel": "grok-imagine-image-quality",
        "provider": "grok",
        "upstreamModel": "grok-imagine-image-quality",
        "capability": "image",
    },
    {
        "publicModel": "grok-imagine-video",
        "provider": "grok",
        "upstreamModel": "grok-imagine-video",
        "capability": "video",
    },
    {
        "publicModel": "grok-imagine-video-1.5",
        "provider": "grok",
        "upstreamModel": "grok-imagine-video-1.5-preview",
        "capability": "video",
    },
]
