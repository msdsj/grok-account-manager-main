"""模型目录：公共模型名到 provider 与上游模型的映射。"""

from __future__ import annotations

from dataclasses import dataclass

from ..core import database as db
from ..providers.gpt.models import DEFAULT_MODELS as GPT_MODELS
from ..providers.grok.models import DEFAULT_MODELS as GROK_MODELS
from ..providers.kiro.models import DEFAULT_MODELS as KIRO_MODELS
from .errors import GatewayError

# 每个业务板块独立维护默认模型，这里只做汇总后写入数据库。
DEFAULT_MODEL_CATALOG: list[dict[str, str]] = [*GROK_MODELS, *GPT_MODELS, *KIRO_MODELS]

# 模型前缀兜底：目录里没有显式配置时，仍能按命名识别 provider 与能力。
_PROVIDER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("grok", "grok"),
    ("gpt-image", "chatgpt"),
    ("gpt-", "chatgpt"),
    ("dall-e", "chatgpt"),
    ("kiro-", "kiro"),
    ("claude", "kiro"),
)

_CAPABILITY_HINTS: tuple[tuple[str, str], ...] = (
    ("imagine", "image"),
    ("image", "image"),
    ("video", "video"),
)


@dataclass(frozen=True)
class ModelRoute:
    public_model: str
    provider: str
    upstream_model: str
    capability: str
    config: dict


def ensure_seeded() -> None:
    """把默认模型目录补进数据库，已存在的记录不覆盖。"""
    db.seed_model_catalog(DEFAULT_MODEL_CATALOG)


def list_models(*, enabled_only: bool = True) -> list[dict]:
    ensure_seeded()
    entries = db.list_model_catalog()
    if not entries:
        entries = [
            {
                "publicModel": item["publicModel"],
                "provider": item["provider"],
                "upstreamModel": item["upstreamModel"],
                "capability": item["capability"],
                "enabled": True,
                "weight": 1,
                "config": {},
            }
            for item in DEFAULT_MODEL_CATALOG
        ]
    if enabled_only:
        entries = [entry for entry in entries if entry.get("enabled", True)]
    return entries


def _infer_route(model: str) -> ModelRoute | None:
    lowered = model.lower()
    provider = ""
    for prefix, candidate in _PROVIDER_PREFIXES:
        if lowered.startswith(prefix):
            provider = candidate
            break
    if not provider:
        return None
    capability = "chat"
    for hint, value in _CAPABILITY_HINTS:
        if hint in lowered:
            capability = value
            break
    return ModelRoute(
        public_model=model,
        provider=provider,
        upstream_model=model,
        capability=capability,
        config={},
    )


def resolve_model(model: str) -> ModelRoute:
    """把请求里的公共模型名解析为 provider 路由，未知模型回退到命名前缀推断。"""
    name = str(model or "").strip()
    if not name:
        raise GatewayError("缺少 model 参数", status_code=400, code="invalid_request_error")
    ensure_seeded()
    for entry in db.list_model_catalog():
        if str(entry.get("publicModel") or "").lower() == name.lower():
            if not entry.get("enabled", True):
                raise GatewayError(f"模型 {name} 已被禁用", status_code=404, code="model_not_found")
            return ModelRoute(
                public_model=str(entry.get("publicModel") or name),
                provider=str(entry.get("provider") or "grok"),
                upstream_model=str(entry.get("upstreamModel") or name),
                capability=str(entry.get("capability") or "chat"),
                config=entry.get("config") or {},
            )
    inferred = _infer_route(name)
    if inferred is None:
        raise GatewayError(f"未知模型: {name}", status_code=404, code="model_not_found")
    return inferred
