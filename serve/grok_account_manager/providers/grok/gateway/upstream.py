"""Grok 上游协议封装。"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import requests

from ..account_tester import extract_response_text
from ....gateway.errors import GatewayError

DEFAULT_API_BASE_URL = os.environ.get("GROK_API_BASE_URL", "https://api.x.ai/v1").rstrip("/")
DEFAULT_CLI_BASE_URL = os.environ.get("GROK_CLI_BASE_URL", "https://cli-chat-proxy.grok.com/v1").rstrip("/")
GROK_CLI_VERSION = "0.2.93"
GROK_UPSTREAM_USER_AGENT = "grok-account-manager/0.1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _is_oauth_credential(credential: dict[str, Any]) -> bool:
    return _text(credential.get("auth_mode")).lower() in {"", "oauth"} or bool(_text(credential.get("refresh_token")))


def _is_parseable(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme and parsed.netloc)
    except Exception:
        return False


def responses_base_url(credential: dict[str, Any]) -> str:
    raw = _text(credential.get("base_url"))
    if raw and _is_parseable(raw):
        return raw.rstrip("/")
    if _is_oauth_credential(credential):
        return DEFAULT_CLI_BASE_URL
    return DEFAULT_API_BASE_URL


def media_base_url(credential: dict[str, Any]) -> str:
    base_url = responses_base_url(credential)
    if urlparse(base_url).hostname == "cli-chat-proxy.grok.com":
        return DEFAULT_API_BASE_URL
    return base_url


def upstream_headers(credential: dict[str, Any], base_url: str, *, accept: str = "application/json") -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_text(credential.get('access_token'))}",
        "Accept": accept,
        "Content-Type": "application/json",
        "Connection": "Keep-Alive",
    }
    use_cli_headers = _is_oauth_credential(credential) or urlparse(base_url).hostname == "cli-chat-proxy.grok.com"
    if use_cli_headers:
        headers.update(
            {
                "User-Agent": GROK_UPSTREAM_USER_AGENT,
                "X-Grok-Client-Version": GROK_CLI_VERSION,
                "X-Grok-Client-Mode": "interactive",
            }
        )
    return headers


def _content_text(content: Any) -> str:
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


def convert_messages(messages: Any) -> list[dict[str, str]]:
    converted: list[dict[str, str]] = []
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = _text(message.get("role")) or "user"
            if role in {"developer"}:
                role = "system"
            elif role in {"tool", "function"}:
                role = "user"
            elif role not in {"system", "assistant", "user"}:
                role = "user"
            text = _content_text(message.get("content"))
            if text:
                converted.append({"role": role, "content": text})
    if not converted:
        converted = [{"role": "user", "content": "Reply with OK."}]
    return converted


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def build_chat_body(upstream_model: str, payload: dict[str, Any], *, stream: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": upstream_model,
        "input": convert_messages(payload.get("messages")),
        "stream": bool(stream),
    }
    max_tokens = _positive_int(payload.get("max_completion_tokens")) or _positive_int(payload.get("max_tokens"))
    if max_tokens:
        body["max_output_tokens"] = max_tokens
    temperature = payload.get("temperature")
    if isinstance(temperature, (int, float)):
        body["temperature"] = float(temperature)
    return body


def build_image_body(upstream_model: str, payload: dict[str, Any]) -> dict[str, Any]:
    prompt = _text(payload.get("prompt"))
    if not prompt:
        raise GatewayError("prompt 不能为空", status_code=400, code="invalid_request_error")
    count = _positive_int(payload.get("n")) or 1
    body: dict[str, Any] = {
        "model": upstream_model,
        "prompt": prompt,
        "n": max(1, min(4, count)),
    }
    return body


def error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return _text(error.get("message")) or json.dumps(error, ensure_ascii=False)[:500]
        if isinstance(error, str):
            return error
        if payload.get("message"):
            return _text(payload.get("message"))
    return ""


def parse_error_text(response: requests.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return _text(response.text)[:500] or f"上游返回 HTTP {response.status_code}"
    return error_message(payload) or f"上游返回 HTTP {response.status_code}"


def raise_for_upstream(response: requests.Response) -> None:
    if response.ok:
        return
    status = int(response.status_code or 502)
    message = parse_error_text(response)
    if status in {401, 403}:
        code = "authentication_error"
    elif status == 429:
        code = "rate_limit_error"
    elif status >= 500:
        code = "upstream_error"
    else:
        code = "invalid_request_error"
    raise GatewayError(message, status_code=status, code=code, upstream_status=status)


def request_chat(
    credential: dict[str, Any],
    body: dict[str, Any],
    *,
    timeout: int,
    stream: bool,
) -> requests.Response:
    base_url = responses_base_url(credential)
    response = requests.post(
        f"{base_url}/responses",
        headers=upstream_headers(credential, base_url, accept="text/event-stream" if stream else "application/json"),
        json=body,
        timeout=max(5, int(timeout or 180)),
        stream=stream,
    )
    raise_for_upstream(response)
    return response


def request_image(credential: dict[str, Any], body: dict[str, Any], *, timeout: int) -> requests.Response:
    base_url = media_base_url(credential)
    response = requests.post(
        f"{base_url}/images/generations",
        headers=upstream_headers(credential, base_url),
        json=body,
        timeout=max(5, int(timeout or 180)),
    )
    raise_for_upstream(response)
    return response


def _usage_from(upstream_payload: dict[str, Any]) -> dict[str, int]:
    raw = upstream_payload.get("usage") if isinstance(upstream_payload, dict) else None
    if not isinstance(raw, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt = _positive_int(raw.get("input_tokens")) or _positive_int(raw.get("prompt_tokens"))
    completion = _positive_int(raw.get("output_tokens")) or _positive_int(raw.get("completion_tokens"))
    total = _positive_int(raw.get("total_tokens")) or prompt + completion
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def completion_payload(upstream_payload: dict[str, Any], public_model: str) -> dict[str, Any]:
    usage = _usage_from(upstream_payload)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": public_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": extract_response_text(upstream_payload)},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def image_payload(upstream_payload: dict[str, Any]) -> dict[str, Any]:
    data = upstream_payload.get("data") if isinstance(upstream_payload, dict) else None
    if not isinstance(data, list) or not data:
        raise GatewayError("上游没有返回图片数据", status_code=502, code="upstream_error")
    items = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        item: dict[str, Any] = {}
        if entry.get("url"):
            item["url"] = entry["url"]
        if entry.get("b64_json"):
            item["b64_json"] = entry["b64_json"]
        if item:
            items.append(item)
    if not items:
        raise GatewayError("上游返回的图片格式无法识别", status_code=502, code="upstream_error")
    return {"created": int(time.time()), "data": items}


def _sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chunk(completion_id: str, created: int, public_model: str, delta: dict[str, Any], finish_reason: str | None) -> str:
    return _sse(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": public_model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
    )


def _delta_from_event(payload: dict[str, Any]) -> str:
    event_type = _text(payload.get("type"))
    if event_type == "response.output_text.delta":
        return _text(payload.get("delta"))
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
        return _text(delta.get("content"))
    return ""


def iter_openai_chunks(
    response: requests.Response,
    *,
    public_model: str,
    usage_out: dict[str, int],
) -> Iterator[str]:
    """把上游 SSE 转成 OpenAI chat.completion.chunk 流。"""
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    try:
        for raw in response.iter_lines(decode_unicode=False):
            if not raw:
                continue
            line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("error"):
                message = error_message(payload) or "上游流式响应返回错误"
                raise GatewayError(message, status_code=502, code="upstream_error")
            text = _delta_from_event(payload)
            if text:
                yield _chunk(completion_id, created, public_model, {"content": text}, None)
            if payload.get("type") == "response.completed":
                completed = payload.get("response")
                if isinstance(completed, dict):
                    usage_out.update(_usage_from(completed))
    finally:
        response.close()
    yield _chunk(completion_id, created, public_model, {}, "stop")
    yield "data: [DONE]\n\n"
