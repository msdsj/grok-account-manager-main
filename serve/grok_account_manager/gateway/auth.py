"""客户端密钥鉴权。"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass

from ..core import database as db
from .errors import GatewayError

KEY_PREFIX = "gak_"
VALID_SCOPES = ("chat", "image", "video")


@dataclass(frozen=True)
class AuthContext:
    key_id: str
    name: str
    scopes: tuple[str, ...]
    models: tuple[str, ...]
    local: bool = False


LOCAL_CONTEXT = AuthContext(key_id="", name="local", scopes=VALID_SCOPES, models=(), local=True)


def hash_key(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def extract_bearer(authorization: str | None, api_key: str | None = None) -> str:
    header = str(authorization or "").strip()
    if header:
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in {"bearer", "token"}:
            return parts[1].strip()
        if len(parts) == 1:
            return parts[0].strip()
    return str(api_key or "").strip()


def authenticate(authorization: str | None = None, api_key: str | None = None) -> AuthContext:
    """校验密钥；数据库里还没有任何密钥时按本机直连模式放行。"""
    if db.count_client_keys() == 0:
        return LOCAL_CONTEXT
    token = extract_bearer(authorization, api_key)
    if not token:
        raise GatewayError("缺少 API 密钥", status_code=401, code="authentication_error")
    record = db.find_client_key_by_hash(hash_key(token))
    if not record or not record.get("enabled"):
        raise GatewayError("API 密钥无效或已禁用", status_code=401, code="authentication_error")
    db.touch_client_key(str(record.get("id") or ""))
    return AuthContext(
        key_id=str(record.get("id") or ""),
        name=str(record.get("name") or ""),
        scopes=tuple(record.get("scopes") or ()),
        models=tuple(record.get("models") or ()),
        local=False,
    )


def require_scope(auth: AuthContext, capability: str, model: str) -> None:
    if auth.local:
        return
    if capability and capability not in auth.scopes:
        raise GatewayError(
            f"该密钥没有 {capability} 权限",
            status_code=403,
            code="permission_error",
        )
    if auth.models and model not in auth.models:
        raise GatewayError(
            f"该密钥无权访问模型 {model}",
            status_code=403,
            code="permission_error",
        )


def create_client_key(
    name: str,
    *,
    scopes: list[str] | None = None,
    models: list[str] | None = None,
) -> dict:
    """创建密钥并只在创建响应里返回一次明文。"""
    selected = [scope for scope in (scopes or list(VALID_SCOPES)) if scope in VALID_SCOPES]
    if not selected:
        raise GatewayError("至少选择一个权限范围", status_code=400, code="invalid_request_error")
    token = KEY_PREFIX + secrets.token_urlsafe(32)
    key_id = uuid.uuid4().hex
    db.insert_client_key(
        key_id,
        name or "未命名密钥",
        hash_key(token),
        token[:12],
        scopes=selected,
        models=[str(item).strip() for item in (models or []) if str(item).strip()],
    )
    return {
        "id": key_id,
        "name": name or "未命名密钥",
        "key": token,
        "keyPrefix": token[:12],
        "scopes": selected,
        "models": [str(item).strip() for item in (models or []) if str(item).strip()],
    }
