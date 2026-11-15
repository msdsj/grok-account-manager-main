"""Kiro 模型名到 CodeWhisperer 内部模型 ID 的映射。"""

from __future__ import annotations

import re

# 参考项目 Kiro API 的 CodeWhisperer 默认模型 ID。
DEFAULT_MODEL_ID = "CLAUDE_SONNET_4_20250514_V1_0"

CLAUDE_SONNET_4_5_MODEL_ID = "CLAUDE_SONNET_4_5_20251001_V1_0"
CLAUDE_OPUS_4_MODEL_ID = "CLAUDE_OPUS_4_20250514_V1_0"
CLAUDE_3_7_SONNET_MODEL_ID = "CLAUDE_3_7_SONNET_20250219_V1_0"
CLAUDE_HAIKU_4_5_MODEL_ID = "CLAUDE_HAIKU_4_5_20251001_V1_0"

_MODEL_ID_MAP: dict[str, str] = {
    "kiro-auto": DEFAULT_MODEL_ID,
    "kiro-claude-sonnet-4": DEFAULT_MODEL_ID,
    "kiro-claude-sonnet-4.0": DEFAULT_MODEL_ID,
    "kiro-claude-sonnet-4.5": CLAUDE_SONNET_4_5_MODEL_ID,
    "kiro-claude-opus-4": CLAUDE_OPUS_4_MODEL_ID,
    "kiro-claude-3.7-sonnet": CLAUDE_3_7_SONNET_MODEL_ID,
    "kiro-claude-haiku-4.5": CLAUDE_HAIKU_4_5_MODEL_ID,
    "claude-sonnet-4": DEFAULT_MODEL_ID,
    "claude-sonnet-4-20250514": DEFAULT_MODEL_ID,
    "claude-sonnet-4-5": CLAUDE_SONNET_4_5_MODEL_ID,
    "claude-sonnet-4.5": CLAUDE_SONNET_4_5_MODEL_ID,
    "claude-opus-4": CLAUDE_OPUS_4_MODEL_ID,
    "claude-3-7-sonnet": CLAUDE_3_7_SONNET_MODEL_ID,
    "claude-3.7-sonnet": CLAUDE_3_7_SONNET_MODEL_ID,
    "claude-3-5-sonnet": CLAUDE_SONNET_4_5_MODEL_ID,
    "claude-haiku-4-5": CLAUDE_HAIKU_4_5_MODEL_ID,
    "claude-haiku-4.5": CLAUDE_HAIKU_4_5_MODEL_ID,
}

# CodeWhisperer 内部 ID 形如 CLAUDE_SONNET_4_20250514_V1_0，原样透传。
_INTERNAL_MODEL_ID = re.compile(r"^[A-Z0-9_]+$")
_VERSION_PATTERN = re.compile(r"^(claude-(?:sonnet|haiku|opus))-(\d+)-(\d{1,2})(?=$|[^\d])", re.IGNORECASE)


def normalize_claude_version(model: str) -> str:
    """把 claude-opus-4-6 这类版本短横转成点号，兼容不支持小数点的客户端。"""
    return _VERSION_PATTERN.sub(r"\1-\2.\3", model)


def map_model_id(model: str) -> str:
    """把公共模型名映射为上游 modelId；未知的 claude*/kiro* 回退到默认模型。"""
    name = str(model or "").strip()
    if not name:
        return DEFAULT_MODEL_ID
    if "_" in name and _INTERNAL_MODEL_ID.match(name):
        return name
    normalized = normalize_claude_version(name)
    mapped = _MODEL_ID_MAP.get(normalized.lower())
    if mapped:
        return mapped
    return DEFAULT_MODEL_ID


DEFAULT_MODELS: list[dict[str, str]] = [
    {"publicModel": "kiro-auto", "provider": "kiro", "upstreamModel": "kiro-auto", "capability": "chat"},
    {
        "publicModel": "kiro-claude-sonnet-4",
        "provider": "kiro",
        "upstreamModel": "kiro-claude-sonnet-4",
        "capability": "chat",
    },
    {
        "publicModel": "kiro-claude-sonnet-4.5",
        "provider": "kiro",
        "upstreamModel": "kiro-claude-sonnet-4.5",
        "capability": "chat",
    },
    {
        "publicModel": "kiro-claude-opus-4",
        "provider": "kiro",
        "upstreamModel": "kiro-claude-opus-4",
        "capability": "chat",
    },
    {
        "publicModel": "kiro-claude-3.7-sonnet",
        "provider": "kiro",
        "upstreamModel": "kiro-claude-3.7-sonnet",
        "capability": "chat",
    },
]
