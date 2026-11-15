"""OpenAI 消息到 Kiro conversationState 的转换。"""

from __future__ import annotations

import uuid
from typing import Any

from ..models import map_model_id

ORIGIN = "AI_EDITOR"
SYSTEM_ACKNOWLEDGEMENT = "I will follow these instructions."
CONTINUE_CONTENT = "Continue."
DEFAULT_USER_CONTENT = "Hello"

BUILDER_ID_PROFILE_ARN = "arn:aws:codewhisperer:us-east-1:638616132270:profile/AAAACCCCXXXX"
SOCIAL_PROFILE_ARN = "arn:aws:codewhisperer:us-east-1:699475941385:profile/EHGA3GRVQMUK"
ENTERPRISE_FALLBACK_REGION = "us-east-1"
ENTERPRISE_FALLBACK_ACCOUNT_ID = "610548660232"
ENTERPRISE_FALLBACK_PROFILE_ID = "VNECVYCYYAWN"

_PLACEHOLDER_PROFILE_ARNS = {BUILDER_ID_PROFILE_ARN}


def _text(value: Any) -> str:
    return str(value or "").strip()


def content_text(content: Any) -> str:
    """提取字符串或文本块列表中的纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = _text(item.get("text") or item.get("input_text") or item.get("output_text"))
                if text:
                    parts.append(text)
        return "\n".join(parts)
    return _text(content)


def _enterprise_fallback_arn(region: str) -> str:
    target = "eu-central-1" if region.startswith("eu-") else ENTERPRISE_FALLBACK_REGION
    return f"arn:aws:codewhisperer:{target}:{ENTERPRISE_FALLBACK_ACCOUNT_ID}:profile/{ENTERPRISE_FALLBACK_PROFILE_ID}"


def resolve_profile_arn(credential: dict[str, Any], provider: str = "") -> str:
    """按账号类型决策 profileArn：真实 ARN > Enterprise > Social > BuilderId。"""
    profile_arn = _text(credential.get("profileArn") or credential.get("profile_arn"))
    if profile_arn and profile_arn not in _PLACEHOLDER_PROFILE_ARNS:
        return profile_arn
    provider_name = _text(provider or credential.get("provider"))
    auth_method = _text(credential.get("authMethod") or credential.get("auth_method")).lower()
    region = _text(credential.get("region")) or "us-east-1"
    if provider_name in {"Enterprise", "ExternalIdp"} or auth_method == "external_idp":
        return _enterprise_fallback_arn(region)
    if auth_method == "social" or provider_name in {"Github", "Google"}:
        return SOCIAL_PROFILE_ARN
    return BUILDER_ID_PROFILE_ARN


def _is_history_user(message: dict[str, Any]) -> bool:
    return isinstance(message.get("userInputMessage"), dict)


def _merge_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    merged: list[tuple[str, str]] = []
    for role, text in pairs:
        if merged and merged[-1][0] == role:
            merged[-1] = (role, f"{merged[-1][1]}\n\n{text}")
        else:
            merged.append((role, text))
    return merged


def _history_message(role: str, text: str, model_id: str) -> dict[str, Any]:
    if role == "assistant":
        return {"assistantResponseMessage": {"content": text or "I understand."}}
    return {"userInputMessage": {"content": text, "modelId": model_id, "origin": ORIGIN}}


def _build_history(
    pairs: list[tuple[str, str]],
    system_prompt: str,
    model_id: str,
) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    if system_prompt:
        history.append(
            {
                "userInputMessage": {
                    "content": system_prompt,
                    "origin": ORIGIN,
                    "userInputMessageContext": {},
                }
            }
        )
        history.append({"assistantResponseMessage": {"content": SYSTEM_ACKNOWLEDGEMENT}})
    history.extend(_history_message(role, text, model_id) for role, text in pairs)
    if history and not _is_history_user(history[0]):
        history.insert(0, {"userInputMessage": {"content": DEFAULT_USER_CONTENT, "origin": ORIGIN}})
    return history


def _inference_config(payload: dict[str, Any]) -> dict[str, Any]:
    config: dict[str, Any] = {}
    max_tokens = payload.get("max_completion_tokens") or payload.get("max_tokens")
    try:
        if max_tokens:
            config["maxTokens"] = int(max_tokens)
    except (TypeError, ValueError):
        pass
    temperature = payload.get("temperature")
    if isinstance(temperature, (int, float)):
        config["temperature"] = float(temperature)
    top_p = payload.get("top_p")
    if isinstance(top_p, (int, float)):
        config["topP"] = float(top_p)
    return config


def build_payload(upstream_model: str, payload: dict[str, Any], profile_arn: str | None = None) -> dict[str, Any]:
    """把 OpenAI chat 请求转换为 Kiro GenerateAssistantResponse 负载。"""
    model_id = map_model_id(upstream_model)
    system_parts: list[str] = []
    pairs: list[tuple[str, str]] = []

    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = _text(message.get("role")) or "user"
            text = content_text(message.get("content"))
            if role in {"system", "developer"}:
                if text:
                    system_parts.append(text)
            elif role == "assistant":
                pairs.append(("assistant", text or "I understand."))
            elif role in {"tool", "function"}:
                pairs.append(("user", text or "Tool result provided."))
            else:
                pairs.append(("user", text or CONTINUE_CONTENT))

    pairs = _merge_pairs([(role, text) for role, text in pairs if text])
    if pairs and pairs[-1][0] == "user":
        current_content = pairs[-1][1]
        history_pairs = pairs[:-1]
    else:
        current_content = CONTINUE_CONTENT
        history_pairs = pairs

    history = _build_history(history_pairs, "\n\n".join(system_parts), model_id)
    conversation_state: dict[str, Any] = {
        "agentContinuationId": uuid.uuid4().hex,
        "agentTaskType": "vibe",
        "chatTriggerType": "MANUAL",
        "conversationId": uuid.uuid4().hex,
        "currentMessage": {
            "userInputMessage": {
                "content": current_content,
                "modelId": model_id,
                "origin": ORIGIN,
            }
        },
    }
    if history:
        conversation_state["history"] = history

    body: dict[str, Any] = {"conversationState": conversation_state}
    if profile_arn:
        body["profileArn"] = profile_arn
    inference_config = _inference_config(payload)
    if inference_config:
        body["inferenceConfig"] = inference_config
    return body
