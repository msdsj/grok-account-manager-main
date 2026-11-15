"""Grok Imagine 视频任务：创建与状态查询的直连封装。"""

from __future__ import annotations

from typing import Any

import requests

from ....gateway.errors import GatewayError
from . import upstream


def _image_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str) and value.strip():
        return {"url": value.strip()}
    if isinstance(value, dict):
        url = str(value.get("url") or value.get("image_url") or "").strip()
        if url:
            return {"url": url}
    return None


def build_video_body(upstream_model: str, payload: dict[str, Any]) -> dict[str, Any]:
    prompt = str(payload.get("prompt") or "").strip()
    body: dict[str, Any] = {"model": upstream_model}
    if prompt:
        body["prompt"] = prompt
    image = _image_payload(payload.get("image"))
    if image:
        body["image"] = image
    references = payload.get("reference_images")
    if isinstance(references, list):
        converted = [item for item in (_image_payload(value) for value in references) if item]
        if converted:
            body["reference_images"] = converted
    for key in ("duration", "resolution", "aspect_ratio"):
        if payload.get(key) not in (None, ""):
            body[key] = payload.get(key)
    if not body.get("prompt") and not body.get("image"):
        raise GatewayError("视频生成需要 prompt 或 image", status_code=400, code="invalid_request_error")
    return body


def request_video(
    credential: dict[str, Any],
    body: dict[str, Any],
    *,
    timeout: int = 180,
) -> dict[str, Any]:
    base_url = upstream.media_base_url(credential)
    response = requests.post(
        f"{base_url}/videos/generations",
        headers=upstream.upstream_headers(credential, base_url),
        json=body,
        timeout=max(5, int(timeout or 180)),
    )
    upstream.raise_for_upstream(response)
    try:
        data = response.json()
    except Exception as error:
        raise GatewayError(f"视频创建响应解析失败: {error}", status_code=502, code="upstream_error") from error
    if not isinstance(data, dict):
        raise GatewayError("视频创建响应格式异常", status_code=502, code="upstream_error")
    return data


def query_video(
    credential: dict[str, Any],
    video_id: str,
    *,
    timeout: int = 180,
) -> dict[str, Any]:
    base_url = upstream.media_base_url(credential)
    response = requests.get(
        f"{base_url}/videos/{video_id}",
        headers=upstream.upstream_headers(credential, base_url),
        timeout=max(5, int(timeout or 180)),
    )
    upstream.raise_for_upstream(response)
    try:
        data = response.json()
    except Exception as error:
        raise GatewayError(f"视频状态响应解析失败: {error}", status_code=502, code="upstream_error") from error
    if not isinstance(data, dict):
        raise GatewayError("视频状态响应格式异常", status_code=502, code="upstream_error")
    return data
