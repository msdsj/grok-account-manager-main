"""ChatGPT Web 图片上游协议封装。

移植参考项目 chatgpt2api 的图片链路最小实现：
bootstrap -> sentinel chat requirements -> /f/conversation/prepare
-> /f/conversation(SSE) -> conversation 轮询 -> 下载图片。

不引入参考项目的 storage/config/database 依赖。
"""

from __future__ import annotations

import base64
import json
import os
import re
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from ....gateway.errors import GatewayError
from ....gateway.pool import PoolAccount, normalize_provider
from .pow import build_legacy_requirements_token, build_proof_token, parse_pow_resources

CHATGPT_BASE_URL = os.environ.get("CHATGPT_BASE_URL", "https://chatgpt.com").rstrip("/")
CHAT_REQUIREMENTS_PATH = "/backend-api/sentinel/chat-requirements"
CONVERSATION_PREPARE_PATH = "/backend-api/f/conversation/prepare"
CONVERSATION_PATH = "/backend-api/f/conversation"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
DEFAULT_CLIENT_VERSION = "prod-a194cd50d4416d3c0b47c740f206b12ce60f5887"
DEFAULT_CLIENT_BUILD_NUMBER = "6708908"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)

_MAX_IMAGES = 4
_POLL_INTERVAL_SECONDS = 5.0

FILE_SERVICE_ID_RE = re.compile(r"file-service://([A-Za-z0-9_-]+)")
REAL_IMAGE_FILE_ID_RE = re.compile(r"\bfile_00000000[a-f0-9]{24}\b")
SEDIMENT_ID_RE = re.compile(r"sediment://([A-Za-z0-9_-]+)")
DATA_URL_RE = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,([A-Za-z0-9+/=]+)")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _positive_int(value: Any, default: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def access_token(credential: dict[str, Any]) -> str:
    return _text(credential.get("access_token") or credential.get("accessToken"))


def _refresh_token(credential: dict[str, Any]) -> str:
    return _text(credential.get("refresh_token") or credential.get("refreshToken"))


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def chatgpt_account_id(credential: dict[str, Any]) -> str:
    """优先读取显式字段，其次从 access token 的 JWT 声明里解析。"""
    for key in ("chatgpt_account_id", "chatgptAccountId"):
        value = _text(credential.get(key))
        if value:
            return value
    claims = _jwt_claims(access_token(credential))
    auth_claim = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claim, dict):
        return _text(auth_claim.get("chatgpt_account_id"))
    return ""


def _cookie_header(credential: dict[str, Any]) -> str:
    raw = credential.get("cookie") or credential.get("cookies")
    if isinstance(raw, dict):
        return "; ".join(f"{key}={value}" for key, value in raw.items() if _text(key))
    return _text(raw)


def _base_headers(credential: dict[str, Any], *, user_agent: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": user_agent or _text(credential.get("user_agent")) or DEFAULT_USER_AGENT,
        "Origin": CHATGPT_BASE_URL,
        "Referer": f"{CHATGPT_BASE_URL}/",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Priority": "u=1, i",
        "Sec-Ch-Ua": '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        "Sec-Ch-Ua-Arch": '"x86"',
        "Sec-Ch-Ua-Bitness": '"64"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "OAI-Device-Id": _text(credential.get("oai_device_id") or credential.get("device_id")) or uuid.uuid4().hex,
        "OAI-Session-Id": _text(credential.get("oai_session_id") or credential.get("session_id")) or uuid.uuid4().hex,
        "OAI-Language": "zh-CN",
        "OAI-Client-Version": _text(credential.get("client_version")) or DEFAULT_CLIENT_VERSION,
        "OAI-Client-Build-Number": _text(credential.get("client_build_number")) or DEFAULT_CLIENT_BUILD_NUMBER,
    }
    token = access_token(credential)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    account_id = chatgpt_account_id(credential)
    if account_id:
        headers["chatgpt-account-id"] = account_id
    cookie = _cookie_header(credential)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def target_headers(
    credential: dict[str, Any],
    path: str,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    headers = _base_headers(credential)
    headers["X-OpenAI-Target-Path"] = path
    headers["X-OpenAI-Target-Route"] = path
    if extra:
        headers.update(extra)
    return headers


def create_session(credential: dict[str, Any]) -> requests.Session:
    # 同一次请求内保持设备与会话指纹一致，sentinel 校验会比对。
    credential.setdefault("oai_device_id", _text(credential.get("device_id")) or uuid.uuid4().hex)
    credential.setdefault("oai_session_id", _text(credential.get("session_id")) or uuid.uuid4().hex)
    session = requests.Session()
    session.headers.update(_base_headers(credential))
    return session


def error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return _text(error.get("message")) or json.dumps(error, ensure_ascii=False)[:500]
        if isinstance(error, str):
            return error
        for key in ("message", "detail", "error_description"):
            if payload.get(key):
                return _text(payload.get(key))
    return ""


def parse_error_text(response: requests.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return _text(response.text)[:500] or f"上游返回 HTTP {response.status_code}"
    return error_message(payload) or f"上游返回 HTTP {response.status_code}"


def raise_for_upstream(response: requests.Response) -> None:
    """把上游 HTTP 状态映射成网关错误。"""
    status = int(getattr(response, "status_code", 502) or 502)
    if 200 <= status < 300:
        return
    message = parse_error_text(response)
    if status in {401, 403}:
        raise GatewayError(message, status_code=401, code="authentication_error", upstream_status=status)
    if status == 429:
        raise GatewayError(message, status_code=429, code="rate_limit_error", upstream_status=status)
    if status >= 500:
        raise GatewayError(message, status_code=502, code="upstream_error", upstream_status=status)
    raise GatewayError(message, status_code=400, code="invalid_request_error", upstream_status=status)


def _expires_soon(credential: dict[str, Any]) -> bool:
    access = access_token(credential)
    expires_at = 0
    try:
        expires_at = int(credential.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if not expires_at and access:
        try:
            expires_at = int(_jwt_claims(access).get("exp") or 0)
        except (TypeError, ValueError):
            expires_at = 0
    if expires_at > 10_000_000_000:  # 兼容毫秒时间戳
        expires_at //= 1000
    if expires_at <= 0:
        return False
    return expires_at - 120 <= int(time.time())


def refresh_access_token(refresh_token: str, *, timeout: int = 30) -> dict[str, Any]:
    """按参考项目实现刷新 ChatGPT OAuth access token。"""
    try:
        response = requests.post(
            OAUTH_TOKEN_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": DEFAULT_USER_AGENT,
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": OAUTH_CLIENT_ID,
            },
            timeout=max(5, int(timeout or 30)),
        )
    except requests.RequestException as error:
        raise GatewayError(
            f"刷新 ChatGPT access token 失败: {error}",
            status_code=401,
            code="authentication_error",
        ) from error
    if response.status_code != 200:
        raise GatewayError(
            f"刷新 ChatGPT access token 失败: {parse_error_text(response)}",
            status_code=401,
            code="authentication_error",
            upstream_status=response.status_code,
        )
    try:
        data = response.json()
    except Exception as error:
        raise GatewayError(
            f"刷新 ChatGPT access token 响应解析失败: {error}",
            status_code=401,
            code="authentication_error",
        ) from error
    if not isinstance(data, dict) or not _text(data.get("access_token")):
        raise GatewayError("刷新 ChatGPT access token 未返回 access_token", status_code=401, code="authentication_error")
    credential: dict[str, Any] = {
        "access_token": _text(data.get("access_token")),
        "refresh_token": _text(data.get("refresh_token")) or refresh_token,
    }
    if _text(data.get("id_token")):
        credential["id_token"] = _text(data.get("id_token"))
    expires_at = _jwt_claims(credential["access_token"]).get("exp")
    if isinstance(expires_at, (int, float)):
        credential["expires_at"] = int(expires_at)
    return credential


def _persist(account: PoolAccount, credential: dict[str, Any]) -> None:
    """把刷新后的凭证回写账号库；失败不阻断请求。"""
    if not account.export_key:
        return
    try:
        from ....api.services.accounts import account_from_credential
        from ....core import database as db

        refreshed = account_from_credential(
            credential,
            Path("database"),
            export_key=account.export_key,
            account_id=str(account.account.get("id") or ""),
            provider=account.provider,
        )
        if account.account.get("tags") is not None:
            refreshed["tags"] = account.account["tags"]
        db.update_account_credential(account.export_key, refreshed, credential)
    except Exception as error:  # pragma: no cover - 回写失败不应该阻断请求
        print(f"[gateway] 回写 ChatGPT 刷新后的凭证失败: {error}")


def prepare_credential(
    account: PoolAccount,
    *,
    force_refresh: bool = False,
    timeout: int = 30,
) -> dict[str, Any]:
    """返回可用于上游请求的凭证副本，必要时刷新 access token。"""
    credential = dict(account.credential)
    if normalize_provider(account.provider) != "chatgpt":
        return credential
    access = access_token(credential)
    refresh = _refresh_token(credential)
    if not refresh:
        if not access:
            raise GatewayError("账号缺少可用凭证", status_code=401, code="authentication_error")
        return credential
    if not force_refresh and access and not _expires_soon(credential):
        return credential
    tokens = refresh_access_token(refresh, timeout=timeout)
    credential.update(tokens)
    _persist(account, credential)
    return credential


@dataclass
class ChatRequirements:
    token: str
    proof_token: str = ""
    turnstile_token: str = ""
    so_token: str = ""


@dataclass
class ImagePlan:
    model: str
    prompt: str
    n: int
    size: str | None
    quality: str
    response_format: str
    thinking_effort: str = ""
    images: list[str] = field(default_factory=list)


@dataclass
class StreamResult:
    conversation_id: str = ""
    file_ids: list[str] = field(default_factory=list)
    sediment_ids: list[str] = field(default_factory=list)
    inline_images: list[str] = field(default_factory=list)
    text: str = ""


def split_image_model(model: str) -> tuple[str, str]:
    """拆出上游模型名与思考强度后缀。"""
    name = _text(model)
    lowered = name.lower()
    for prefix in ("plus-", "team-", "pro-"):
        if lowered.startswith(prefix):
            name = name[len(prefix) :]
            break
    base, separator, suffix = name.rpartition("-")
    if separator and suffix.lower() in {"standard", "extended", "max"}:
        return base, suffix.lower()
    return name, ""


def build_image_prompt(prompt: str, size: str | None, quality: str = "auto") -> str:
    """按参考实现把尺寸与质量拼进提示词。"""
    hints: list[str] = []
    if size:
        hints.append(f"输出图片尺寸为 {size}。")
    if quality:
        hints.append(f"输出图片质量为 {quality}。")
    text = _text(prompt)
    return f"{text}\n\n{''.join(hints)}" if hints else text


def _image_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [_text(item) for item in value if _text(item)]
    if _text(value):
        return [_text(value)]
    return []


def build_image_plan(upstream_model: str, payload: dict[str, Any]) -> ImagePlan:
    """校验并归一化 OpenAI Images 请求。"""
    prompt = _text(payload.get("prompt"))
    if not prompt:
        raise GatewayError("prompt 不能为空", status_code=400, code="invalid_request_error")
    if _text(payload.get("mask")):
        raise GatewayError("暂不支持 mask 参数", status_code=400, code="invalid_request_error")
    model, thinking_effort = split_image_model(upstream_model)
    size = _text(payload.get("size")) or None
    quality = _text(payload.get("quality")) or "auto"
    count = max(1, min(_MAX_IMAGES, _positive_int(payload.get("n"), 1)))
    return ImagePlan(
        model=model or upstream_model,
        prompt=build_image_prompt(prompt, size, quality),
        n=count,
        size=size,
        quality=quality,
        response_format=_text(payload.get("response_format")) or "b64_json",
        thinking_effort=thinking_effort,
        images=_image_list(payload.get("image")),
    )


def build_prepare_body(plan: ImagePlan) -> dict[str, Any]:
    """构造 /backend-api/f/conversation/prepare 请求体。"""
    body: dict[str, Any] = {
        "action": "next",
        "fork_from_shared_post": False,
        "parent_message_id": uuid.uuid4().hex,
        "model": plan.model,
        "client_prepare_state": "success",
        "timezone_offset_min": -480,
        "timezone": "Asia/Shanghai",
        "conversation_mode": {"kind": "primary_assistant"},
        "system_hints": ["picture_v2"],
        "partial_query": {
            "id": uuid.uuid4().hex,
            "author": {"role": "user"},
            "content": {"content_type": "text", "parts": [plan.prompt]},
        },
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "client_contextual_info": {"app_name": "chatgpt.com"},
    }
    if plan.thinking_effort:
        body["thinking_effort"] = plan.thinking_effort
    return body


def build_conversation_body(plan: ImagePlan, references: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """构造图片生成/编辑的 /backend-api/f/conversation 请求体。"""
    references = references or []
    parts: list[Any] = [
        {
            "content_type": "image_asset_pointer",
            "asset_pointer": f"file-service://{item['file_id']}",
            "width": item["width"],
            "height": item["height"],
            "size_bytes": item["file_size"],
        }
        for item in references
    ]
    parts.append(plan.prompt)
    if references:
        content: dict[str, Any] = {"content_type": "multimodal_text", "parts": parts}
    else:
        content = {"content_type": "text", "parts": [plan.prompt]}
    metadata: dict[str, Any] = {
        "developer_mode_connector_ids": [],
        "selected_github_repos": [],
        "selected_all_github_repos": False,
        "system_hints": ["picture_v2"],
        "serialization_metadata": {"custom_symbol_offsets": []},
    }
    if references:
        metadata["attachments"] = [
            {
                "id": item["file_id"],
                "mimeType": item["mime_type"],
                "name": item["file_name"],
                "size": item["file_size"],
                "width": item["width"],
                "height": item["height"],
            }
            for item in references
        ]
    body: dict[str, Any] = {
        "action": "next",
        "messages": [
            {
                "id": uuid.uuid4().hex,
                "author": {"role": "user"},
                "create_time": time.time(),
                "content": content,
                "metadata": metadata,
            }
        ],
        "parent_message_id": uuid.uuid4().hex,
        "model": plan.model,
        "client_prepare_state": "sent",
        "timezone_offset_min": -480,
        "timezone": "Asia/Shanghai",
        "conversation_mode": {"kind": "primary_assistant"},
        "enable_message_followups": True,
        "system_hints": ["picture_v2"],
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "client_contextual_info": {
            "is_dark_mode": False,
            "time_since_loaded": 1200,
            "page_height": 1072,
            "page_width": 1724,
            "pixel_ratio": 1.2,
            "screen_height": 1440,
            "screen_width": 2560,
            "app_name": "chatgpt.com",
        },
        "paragen_cot_summary_display_override": "allow",
        "force_parallel_switch": "auto",
    }
    if plan.thinking_effort:
        body["thinking_effort"] = plan.thinking_effort
    return body


def bootstrap(session: requests.Session, credential: dict[str, Any], *, timeout: int = 30) -> tuple[list[str], str]:
    """预热首页并提取 PoW 脚本地址。"""
    headers = {
        "User-Agent": _base_headers(credential)["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    response = session.get(f"{CHATGPT_BASE_URL}/", headers=headers, timeout=max(5, min(30, int(timeout or 30))))
    raise_for_upstream(response)
    return parse_pow_resources(response.text)


def get_chat_requirements(
    session: requests.Session,
    credential: dict[str, Any],
    *,
    script_sources: list[str] | None = None,
    data_build: str = "",
    timeout: int = 30,
) -> ChatRequirements:
    """执行 sentinel prepare + finalize，返回对话所需 token。"""
    user_agent = _base_headers(credential)["User-Agent"]
    p_token = build_legacy_requirements_token(user_agent, script_sources, data_build)
    prepare_path = f"{CHAT_REQUIREMENTS_PATH}/prepare"
    response = session.post(
        f"{CHATGPT_BASE_URL}{prepare_path}",
        headers=target_headers(credential, prepare_path, {"Content-Type": "application/json"}),
        json={"p": p_token},
        timeout=max(5, min(30, int(timeout or 30))),
    )
    raise_for_upstream(response)
    try:
        prepare_data = response.json()
    except Exception as error:
        raise GatewayError(f"chat requirements 响应解析失败: {error}", status_code=502, code="upstream_error") from error
    if not isinstance(prepare_data, dict):
        raise GatewayError("chat requirements 返回格式异常", status_code=502, code="upstream_error")
    if isinstance(prepare_data.get("arkose"), dict) and prepare_data["arkose"].get("required"):
        raise GatewayError("上游要求 Arkose 验证，当前网关暂不支持", status_code=502, code="upstream_error")

    proof_token = ""
    proof_info = prepare_data.get("proofofwork")
    if isinstance(proof_info, dict) and proof_info.get("required"):
        try:
            proof_token = build_proof_token(
                _text(proof_info.get("seed")),
                _text(proof_info.get("difficulty")),
                user_agent,
                script_sources=script_sources,
                data_build=data_build,
            )
        except Exception as error:
            raise GatewayError(f"计算 ChatGPT PoW 失败: {error}", status_code=502, code="upstream_error") from error

    turnstile_info = prepare_data.get("turnstile")
    if isinstance(turnstile_info, dict) and turnstile_info.get("required"):
        raise GatewayError("上游要求 Turnstile 验证，当前网关暂不支持自动处理", status_code=502, code="upstream_error")

    finalize_path = f"{CHAT_REQUIREMENTS_PATH}/finalize"
    response = session.post(
        f"{CHATGPT_BASE_URL}{finalize_path}",
        headers=target_headers(credential, finalize_path, {"Content-Type": "application/json"}),
        json={
            "prepare_token": _text(prepare_data.get("prepare_token")),
            "proof_token": proof_token,
            "turnstile_token": "",
        },
        timeout=max(5, min(30, int(timeout or 30))),
    )
    raise_for_upstream(response)
    try:
        data = response.json()
    except Exception as error:
        raise GatewayError(f"chat requirements finalize 响应解析失败: {error}", status_code=502, code="upstream_error") from error
    if not isinstance(data, dict):
        raise GatewayError("chat requirements finalize 返回格式异常", status_code=502, code="upstream_error")
    token = _text(data.get("token"))
    if not token:
        raise GatewayError("上游未返回 chat requirements token", status_code=502, code="upstream_error")
    return ChatRequirements(token=token, proof_token=proof_token, so_token=_text(data.get("so_token")))


def image_headers(
    credential: dict[str, Any],
    path: str,
    requirements: ChatRequirements,
    *,
    conduit_token: str = "",
    accept: str = "*/*",
) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": accept,
        "OpenAI-Sentinel-Chat-Requirements-Token": requirements.token,
    }
    if requirements.proof_token:
        headers["OpenAI-Sentinel-Proof-Token"] = requirements.proof_token
    if requirements.turnstile_token:
        headers["OpenAI-Sentinel-Turnstile-Token"] = requirements.turnstile_token
    if requirements.so_token:
        headers["OpenAI-Sentinel-SO-Token"] = requirements.so_token
    if conduit_token:
        headers["X-Conduit-Token"] = conduit_token
    if accept == "text/event-stream":
        headers["X-Oai-Turn-Trace-Id"] = uuid.uuid4().hex
    return target_headers(credential, path, headers)


def prepare_image_conversation(
    session: requests.Session,
    credential: dict[str, Any],
    requirements: ChatRequirements,
    plan: ImagePlan,
    *,
    timeout: int = 60,
) -> str:
    response = session.post(
        f"{CHATGPT_BASE_URL}{CONVERSATION_PREPARE_PATH}",
        headers=image_headers(credential, CONVERSATION_PREPARE_PATH, requirements),
        json=build_prepare_body(plan),
        timeout=max(5, int(timeout or 60)),
    )
    raise_for_upstream(response)
    try:
        data = response.json()
    except Exception as error:
        raise GatewayError(f"图片会话准备响应解析失败: {error}", status_code=502, code="upstream_error") from error
    conduit_token = _text(data.get("conduit_token")) if isinstance(data, dict) else ""
    if not conduit_token:
        raise GatewayError("上游未返回 conduit_token", status_code=502, code="upstream_error")
    return conduit_token


def start_image_generation(
    session: requests.Session,
    credential: dict[str, Any],
    requirements: ChatRequirements,
    plan: ImagePlan,
    conduit_token: str,
    *,
    references: list[dict[str, Any]] | None = None,
    timeout: int = 180,
) -> requests.Response:
    response = session.post(
        f"{CHATGPT_BASE_URL}{CONVERSATION_PATH}",
        headers=image_headers(credential, CONVERSATION_PATH, requirements, conduit_token=conduit_token, accept="text/event-stream"),
        json=build_conversation_body(plan, references),
        timeout=(10, max(5, int(timeout or 180))),
        stream=True,
    )
    raise_for_upstream(response)
    return response


def decode_image_bytes(image: str) -> tuple[bytes, str]:
    """解码 base64 或 data URL 图片，返回 (bytes, mime)。"""
    text = _text(image)
    mime = "image/png"
    if text.startswith("data:"):
        header, separator, text = text.partition(",")
        if separator and ";" in header:
            mime = header[5:].split(";", 1)[0] or mime
    try:
        data = base64.b64decode(text, validate=False)
    except Exception as error:
        raise GatewayError("图片 base64 解码失败", status_code=400, code="invalid_request_error") from error
    if data.startswith(b"\xff\xd8"):
        mime = "image/jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    return data, mime


def _image_dimensions(data: bytes) -> tuple[int, int]:
    """读取 PNG/JPEG 尺寸，无法识别时返回 1024x1024。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height, width = struct.unpack(">HH", data[index + 5 : index + 9])
                return int(width), int(height)
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = struct.unpack(">H", data[index + 2 : index + 4])[0]
            index += 2 + length
    return 1024, 1024


def upload_image(
    session: requests.Session,
    credential: dict[str, Any],
    image: str,
    file_name: str,
    *,
    timeout: int = 60,
) -> dict[str, Any]:
    """上传一张 base64 图片，返回 file-service 元数据。"""
    data, mime_type = decode_image_bytes(image)
    extension = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime_type, "png")
    file_name = f"{file_name.rsplit('.', 1)[0]}.{extension}" if "." in file_name else f"{file_name}.{extension}"
    width, height = _image_dimensions(data)
    path = "/backend-api/files"
    response = session.post(
        f"{CHATGPT_BASE_URL}{path}",
        headers=target_headers(credential, path, {"Content-Type": "application/json", "Accept": "application/json"}),
        json={
            "file_name": file_name,
            "file_size": len(data),
            "use_case": "multimodal",
            "width": width,
            "height": height,
        },
        timeout=max(5, int(timeout or 60)),
    )
    raise_for_upstream(response)
    try:
        upload_meta = response.json()
    except Exception as error:
        raise GatewayError(f"图片上传响应解析失败: {error}", status_code=502, code="upstream_error") from error
    upload_url = _text((upload_meta or {}).get("upload_url"))
    file_id = _text((upload_meta or {}).get("file_id"))
    if not upload_url or not file_id:
        raise GatewayError("上游未返回图片上传地址", status_code=502, code="upstream_error")
    upload_response = requests.put(
        upload_url,
        headers={
            "Content-Type": mime_type,
            "x-ms-blob-type": "BlockBlob",
            "x-ms-version": "2020-04-08",
            "Origin": CHATGPT_BASE_URL,
            "Referer": f"{CHATGPT_BASE_URL}/",
            "User-Agent": _base_headers(credential)["User-Agent"],
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.8",
        },
        data=data,
        timeout=max(5, int(timeout or 60)),
    )
    raise_for_upstream(upload_response)
    uploaded_path = f"/backend-api/files/{file_id}/uploaded"
    response = session.post(
        f"{CHATGPT_BASE_URL}{uploaded_path}",
        headers=target_headers(credential, uploaded_path, {"Content-Type": "application/json", "Accept": "application/json"}),
        data="{}",
        timeout=max(5, int(timeout or 60)),
    )
    raise_for_upstream(response)
    return {
        "file_id": file_id,
        "file_name": file_name,
        "file_size": len(data),
        "mime_type": mime_type,
        "width": width,
        "height": height,
    }


def _add_unique(values: list[str], candidates: list[str]) -> None:
    for candidate in candidates:
        if candidate and candidate not in values:
            values.append(candidate)


def _extract_ids(value: Any) -> tuple[list[str], list[str]]:
    file_ids: list[str] = []
    sediment_ids: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, str):
            _add_unique(file_ids, FILE_SERVICE_ID_RE.findall(item))
            _add_unique(file_ids, REAL_IMAGE_FILE_ID_RE.findall(item))
            _add_unique(sediment_ids, SEDIMENT_ID_RE.findall(item))
            return
        if isinstance(item, dict):
            for child in item.values():
                walk(child)
            return
        if isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return file_ids, sediment_ids


def _has_image_pointer(value: Any) -> bool:
    if isinstance(value, dict):
        if _text(value.get("content_type")) == "image_asset_pointer":
            return True
        asset_pointer = _text(value.get("asset_pointer"))
        if asset_pointer.startswith(("file-service://", "sediment://")):
            return True
        return any(_has_image_pointer(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_image_pointer(child) for child in value)
    return False


def _message_of(event: dict[str, Any]) -> dict[str, Any]:
    message = event.get("message")
    if isinstance(message, dict):
        return message
    value = event.get("v")
    if isinstance(value, dict):
        nested = value.get("message")
        if isinstance(nested, dict):
            return nested
    return {}


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    if isinstance(content, str):
        return _text(content)
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if isinstance(parts, list):
        text = "".join(part for part in parts if isinstance(part, str))
        if text:
            return text
    return _text(content.get("text"))


def _collect_inline_images(value: Any, images: list[str]) -> None:
    if isinstance(value, str):
        for item in DATA_URL_RE.findall(value):
            if item not in images:
                images.append(item)
        return
    if isinstance(value, dict):
        b64_json = value.get("b64_json")
        if isinstance(b64_json, str) and b64_json and b64_json not in images:
            images.append(b64_json)
        for child in value.values():
            _collect_inline_images(child, images)
        return
    if isinstance(value, list):
        for child in value:
            _collect_inline_images(child, images)


def _collect_event(event: dict[str, Any], payload: str, result: StreamResult) -> None:
    # 部分上游事件直接携带 b64_json 或 data URL。
    _collect_inline_images(event, result.inline_images)
    match = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
    if match and not result.conversation_id:
        result.conversation_id = match.group(1)
    value = event.get("v")
    if isinstance(value, dict) and _text(value.get("conversation_id")) and not result.conversation_id:
        result.conversation_id = _text(value.get("conversation_id"))
    if _text(event.get("conversation_id")) and not result.conversation_id:
        result.conversation_id = _text(event.get("conversation_id"))

    message = _message_of(event)
    if message:
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        role = _text(author.get("role")).lower()
        if role == "assistant":
            text = _text(_message_text(message))
            if text:
                result.text = text
        if role == "tool" or (role and role != "user" and _has_image_pointer(message)):
            file_ids, sediment_ids = _extract_ids(message)
            _add_unique(result.file_ids, file_ids)
            _add_unique(result.sediment_ids, sediment_ids)
        return

    # patch 事件可能只带 asset_pointer，且用户消息里的是输入图片，需要排除。
    if event.get("o") == "patch" and ("asset_pointer" in payload or "file-service://" in payload):
        value = event.get("v")
        author = value.get("author") if isinstance(value, dict) else None
        role = _text(author.get("role")).lower() if isinstance(author, dict) else ""
        if role != "user":
            file_ids, sediment_ids = _extract_ids(event)
            _add_unique(result.file_ids, file_ids)
            _add_unique(result.sediment_ids, sediment_ids)


def parse_image_stream(response: requests.Response) -> StreamResult:
    """解析 /f/conversation 的 SSE 流。"""
    result = StreamResult()
    try:
        for raw in response.iter_lines(decode_unicode=False):
            if not raw:
                continue
            line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            if payload == "[DONE]":
                break
            _collect_inline_images(payload, result.inline_images)
            try:
                event = json.loads(payload)
            except Exception:
                if not result.conversation_id:
                    match = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
                    if match:
                        result.conversation_id = match.group(1)
                continue
            if isinstance(event, dict):
                _collect_event(event, payload, result)
            else:
                file_ids, sediment_ids = _extract_ids(event)
                _add_unique(result.file_ids, file_ids)
                _add_unique(result.sediment_ids, sediment_ids)
    finally:
        response.close()
    return result


def extract_conversation_image_ids(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """从 conversation 明细里提取图片工具输出的 file/sediment id。"""
    file_ids: list[str] = []
    sediment_ids: list[str] = []
    mapping = data.get("mapping") or {}
    if not isinstance(mapping, dict):
        return file_ids, sediment_ids
    for node in mapping.values():
        message = (node or {}).get("message") or {}
        if not isinstance(message, dict):
            continue
        author = message.get("author") or {}
        metadata = message.get("metadata") or {}
        content = message.get("content") or {}
        role = _text(author.get("role")).lower()
        if role not in {"tool", "assistant"}:
            continue
        is_image_gen = isinstance(metadata, dict) and metadata.get("async_task_type") == "image_gen"
        has_pointer = _has_image_pointer(content) or _has_image_pointer(metadata)
        if role == "assistant" and not (is_image_gen or has_pointer):
            continue
        found_file_ids, found_sediment_ids = _extract_ids({"content": content, "metadata": metadata})
        if not is_image_gen and not has_pointer and not found_file_ids and not found_sediment_ids:
            continue
        _add_unique(file_ids, found_file_ids)
        _add_unique(sediment_ids, found_sediment_ids)
    return file_ids, sediment_ids


def _get_conversation(
    session: requests.Session,
    credential: dict[str, Any],
    conversation_id: str,
    *,
    timeout: int = 60,
) -> dict[str, Any]:
    path = f"/backend-api/conversation/{conversation_id}"
    response = session.get(
        f"{CHATGPT_BASE_URL}{path}",
        headers=target_headers(credential, path, {"Accept": "application/json"}),
        timeout=max(5, int(timeout or 60)),
    )
    raise_for_upstream(response)
    try:
        data = response.json()
    except Exception as error:
        raise GatewayError(f"conversation 响应解析失败: {error}", status_code=502, code="upstream_error") from error
    return data if isinstance(data, dict) else {}


def poll_image_ids(
    session: requests.Session,
    credential: dict[str, Any],
    conversation_id: str,
    *,
    timeout: float,
) -> tuple[list[str], list[str]]:
    """轮询 conversation 文档，直到出现图片 id 或超时。"""
    if not conversation_id:
        return [], []
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        try:
            data = _get_conversation(session, credential, conversation_id)
        except GatewayError as error:
            if error.upstream_status not in {429, 500, 502, 503, 504}:
                raise
            data = {}
        except requests.RequestException:
            data = {}
        file_ids, sediment_ids = extract_conversation_image_ids(data)
        if file_ids or sediment_ids:
            return file_ids, sediment_ids
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))
    return [], []


def _get_file_download_url(
    session: requests.Session,
    credential: dict[str, Any],
    file_id: str,
) -> str:
    path = f"/backend-api/files/{file_id}/download"
    response = session.get(
        f"{CHATGPT_BASE_URL}{path}",
        headers=target_headers(credential, path, {"Accept": "application/json"}),
        timeout=60,
    )
    raise_for_upstream(response)
    data = response.json() if response.text else {}
    if not isinstance(data, dict):
        return ""
    return _text(data.get("download_url") or data.get("url"))


def _get_attachment_download_url(
    session: requests.Session,
    credential: dict[str, Any],
    conversation_id: str,
    attachment_id: str,
) -> str:
    path = f"/backend-api/conversation/{conversation_id}/attachment/{attachment_id}/download"
    response = session.get(
        f"{CHATGPT_BASE_URL}{path}",
        headers=target_headers(credential, path, {"Accept": "application/json"}),
        timeout=60,
    )
    raise_for_upstream(response)
    data = response.json() if response.text else {}
    if not isinstance(data, dict):
        return ""
    return _text(data.get("download_url") or data.get("url"))


def _download_bytes(session: requests.Session, url: str) -> bytes:
    try:
        response = session.get(url, timeout=120)
        if 200 <= response.status_code < 300 and response.content:
            return response.content
    except requests.RequestException:
        return b""
    return b""


def _collect_download_items(
    session: requests.Session,
    credential: dict[str, Any],
    conversation_id: str,
    file_ids: list[str],
    sediment_ids: list[str],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    urls: list[str] = []
    for file_id in file_ids:
        if file_id == "file_upload":
            continue
        try:
            url = _get_file_download_url(session, credential, file_id)
        except GatewayError as error:
            if error.upstream_status in {401, 403, 429} or error.upstream_status >= 500:
                raise
            continue
        if url and url not in urls:
            urls.append(url)
    if conversation_id:
        for sediment_id in sediment_ids:
            try:
                url = _get_attachment_download_url(session, credential, conversation_id, sediment_id)
            except GatewayError as error:
                if error.upstream_status in {401, 403, 429} or error.upstream_status >= 500:
                    raise
                continue
            if url and url not in urls:
                urls.append(url)
    for url in urls:
        data = _download_bytes(session, url)
        item: dict[str, Any] = {"url": url}
        if data:
            item["b64_json"] = base64.b64encode(data).decode("ascii")
            key = item["b64_json"]
        else:
            key = url
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def _generate_once(
    session: requests.Session,
    credential: dict[str, Any],
    requirements: ChatRequirements,
    plan: ImagePlan,
    references: list[dict[str, Any]],
    *,
    timeout: int,
) -> list[dict[str, Any]]:
    conduit_token = prepare_image_conversation(session, credential, requirements, plan, timeout=min(60, timeout))
    response = start_image_generation(
        session,
        credential,
        requirements,
        plan,
        conduit_token,
        references=references,
        timeout=timeout,
    )
    stream_result = parse_image_stream(response)
    if stream_result.inline_images:
        return [{"b64_json": item} for item in stream_result.inline_images]

    file_ids = list(stream_result.file_ids)
    sediment_ids = list(stream_result.sediment_ids)
    if not file_ids and not sediment_ids and stream_result.conversation_id:
        poll_timeout = max(30, min(180, int(timeout)))
        file_ids, sediment_ids = poll_image_ids(
            session,
            credential,
            stream_result.conversation_id,
            timeout=poll_timeout,
        )
    if not file_ids and not sediment_ids:
        message = _text(stream_result.text) or "上游没有返回图片数据"
        raise GatewayError(message[:500], status_code=502, code="upstream_error")

    items = _collect_download_items(
        session,
        credential,
        stream_result.conversation_id,
        file_ids,
        sediment_ids,
    )
    if not items:
        raise GatewayError("上游返回的图片无法下载", status_code=502, code="upstream_error")
    return items


def generate_images(
    credential: dict[str, Any],
    upstream_model: str,
    payload: dict[str, Any],
    *,
    timeout: int = 180,
) -> dict[str, Any]:
    """生成或编辑图片，返回 OpenAI images 响应字典。"""
    plan = build_image_plan(upstream_model, payload)
    if not access_token(credential):
        raise GatewayError("账号缺少 access token", status_code=401, code="authentication_error")
    timeout = max(5, int(timeout or 180))
    session = create_session(credential)
    try:
        script_sources, data_build = bootstrap(session, credential, timeout=min(30, timeout))
        references: list[dict[str, Any]] = []
        for index, image in enumerate(plan.images, start=1):
            references.append(upload_image(session, credential, image, f"image_{index}.png"))
        items: list[dict[str, Any]] = []
        for _ in range(plan.n):
            requirements = get_chat_requirements(
                session,
                credential,
                script_sources=script_sources,
                data_build=data_build,
            )
            items.extend(
                _generate_once(
                    session,
                    credential,
                    requirements,
                    plan,
                    references,
                    timeout=timeout,
                )
            )
        if not items:
            raise GatewayError("上游没有返回图片数据", status_code=502, code="upstream_error")
        return {"created": int(time.time()), "data": items}
    finally:
        session.close()
