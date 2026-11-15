"""内置管理员认证：为前端提供与旧网关兼容的登录/刷新/会话接口。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from ...core import audit
from ...core import database as db

router = APIRouter(tags=["admin-auth"])

_ACCESS_TTL_SECONDS = 3600
_REFRESH_TTL_SECONDS = 7 * 24 * 3600
_REFRESH_COOKIE = "admin_refresh"
_USERNAME_KEY = "admin_username"
_PASSWORD_KEY = "admin_password_hash"
_SECRET_KEY = "admin_token_secret"
_PBKDF2_ROUNDS = 120_000


def _now() -> int:
    return int(time.time())


def _iso(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _secret() -> bytes:
    raw = db.get_meta(_SECRET_KEY)
    if not raw:
        raw = secrets.token_hex(32)
        db.set_meta(_SECRET_KEY, raw)
    return raw.encode("utf-8")


def _hash_password(password: str, salt: str | None = None) -> str:
    selected = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), selected.encode("utf-8"), _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${selected}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2" or not salt or not digest:
        return False
    computed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(rounds))
    return hmac.compare_digest(computed.hex(), digest)


def ensure_admin() -> None:
    """首次启动时写入默认管理员，密码可用 ADMIN_PASSWORD 覆盖。"""
    if db.get_meta(_PASSWORD_KEY):
        return
    username = str(os.environ.get("ADMIN_USERNAME") or "admin").strip() or "admin"
    password = str(os.environ.get("ADMIN_PASSWORD") or "admin")
    db.set_meta(_USERNAME_KEY, username)
    db.set_meta(_PASSWORD_KEY, _hash_password(password))


def _username() -> str:
    return db.get_meta(_USERNAME_KEY, "admin") or "admin"


def _issue(kind: str, ttl: int) -> str:
    payload = {"sub": _username(), "typ": kind, "exp": _now() + ttl, "nonce": secrets.token_hex(8)}
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64encode(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def _verify(token: str, kind: str) -> dict[str, Any] | None:
    try:
        body, signature = str(token or "").split(".", 1)
    except ValueError:
        return None
    expected = _b64encode(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64decode(body))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("typ") != kind:
        return None
    if int(payload.get("exp") or 0) <= _now():
        return None
    return payload


def _tokens() -> dict[str, str]:
    access_exp = _now() + _ACCESS_TTL_SECONDS
    refresh_exp = _now() + _REFRESH_TTL_SECONDS
    return {
        "accessToken": _issue("access", _ACCESS_TTL_SECONDS),
        "accessTokenExpiresAt": _iso(access_exp),
        "refreshTokenExpiresAt": _iso(refresh_exp),
    }


def _admin_payload() -> dict[str, str]:
    return {"id": "admin", "username": _username()}


def _error(message: str, status_code: int = 401, code: str = "authentication_error") -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


def _data(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse({"data": payload}, status_code=status_code)


def _bearer(request: Request) -> str:
    header = str(request.headers.get("authorization") or "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def require_admin(request: Request) -> dict[str, Any] | None:
    payload = _verify(_bearer(request), "access")
    return payload


@router.post("/api/admin/v1/auth/login")
def login(request: Request, body: dict[str, Any] = Body(...)) -> Any:
    ensure_admin()
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    stored = db.get_meta(_PASSWORD_KEY)
    if username != _username() or not _verify_password(password, stored):
        audit.log_operation(
            username or "anonymous",
            "admin.login",
            target_type="admin",
            result="failed",
            detail={"reason": "invalid_credentials"},
        )
        return _error("用户名或密码错误")
    tokens = _tokens()
    audit.log_operation(_username(), "admin.login", target_type="admin", result="success")
    response = _data({"admin": _admin_payload(), "tokens": tokens})
    response.set_cookie(
        _REFRESH_COOKIE,
        _issue("refresh", _REFRESH_TTL_SECONDS),
        max_age=_REFRESH_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/api/admin/v1/auth/refresh")
def refresh(request: Request) -> Any:
    ensure_admin()
    token = str(request.cookies.get(_REFRESH_COOKIE) or "")
    payload = _verify(token, "refresh")
    if payload is None:
        response = _error("登录已过期")
        response.delete_cookie(_REFRESH_COOKIE, path="/")
        return response
    return _data(_tokens())


@router.post("/api/admin/v1/auth/logout")
def logout(request: Request) -> Any:
    response = _data({"loggedOut": True})
    response.delete_cookie(_REFRESH_COOKIE, path="/")
    return response


@router.get("/api/admin/v1/me")
def me(request: Request) -> Any:
    ensure_admin()
    if require_admin(request) is None:
        return _error("未登录或登录已过期")
    return _data(_admin_payload())


@router.put("/api/admin/v1/me/password")
def change_password(request: Request, body: dict[str, Any] = Body(...)) -> Any:
    ensure_admin()
    if require_admin(request) is None:
        return _error("未登录或登录已过期")
    current = str(body.get("currentPassword") or "")
    new_password = str(body.get("newPassword") or "")
    if not _verify_password(current, db.get_meta(_PASSWORD_KEY)):
        return _error("当前密码不正确", 400)
    if len(new_password) < 6:
        return _error("新密码至少 6 位", 400)
    db.set_meta(_PASSWORD_KEY, _hash_password(new_password))
    audit.log_operation(_username(), "admin.password.change", target_type="admin", result="success")
    return _data({"updated": True})
