"""Convert stored Grok OAuth credentials to CLIProxyAPI xAI auth files."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import io
import json
import re
import time
import zipfile


CPA_TYPE = "xai"
CPA_AUTH_KIND = "oauth"
CPA_BASE_URL = "https://api.x.ai/v1"
CPA_TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
CPA_REDIRECT_URI = "http://127.0.0.1:56121/callback"
CPA_DEFAULT_EXPIRES_IN = 21_600


def _text(value) -> str:
    return str(value or "").strip()


def _positive_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _timestamp_seconds(value) -> int | None:
    parsed = _positive_int(value)
    if parsed is None:
        return None
    return parsed // 1000 if parsed > 10_000_000_000 else parsed


def _jwt_claims(token: str) -> dict:
    """Decode unverified JWT claims only for CPA metadata fields."""
    parts = _text(token).split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return {}


def _claim_int(claims: dict, key: str) -> int | None:
    return _positive_int(claims.get(key))


def _rfc3339(timestamp: int) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_cpa_credential(credential: dict, now: int | None = None) -> dict:
    """Build one CLIProxyAPI xAI auth-file object.

    CPA can only refresh a credential when both OAuth access and refresh tokens are
    present. Browser ``sso`` cookies are deliberately not accepted as access tokens.
    """
    access_token = _text(credential.get("access_token"))
    refresh_token = _text(credential.get("refresh_token"))
    email_hint = _text(credential.get("email")) or "unknown"
    missing = [
        name
        for name, value in (("access_token", access_token), ("refresh_token", refresh_token))
        if not value
    ]
    if missing:
        raise ValueError(f"账号 {email_hint} 缺少 {', '.join(missing)}，无法导出 CPA OAuth 凭证")

    id_token = _text(credential.get("id_token"))
    access_claims = _jwt_claims(access_token)
    id_claims = _jwt_claims(id_token)

    issued_at = _claim_int(access_claims, "iat") or _claim_int(id_claims, "iat")
    expires_at = (
        _claim_int(access_claims, "exp")
        or _claim_int(id_claims, "exp")
        or _timestamp_seconds(credential.get("expires_at"))
    )
    created_at = _timestamp_seconds(credential.get("created_at"))
    now_seconds = int(now if now is not None else time.time())

    expires_in = _positive_int(credential.get("expires_in"))
    if expires_in is None and issued_at and expires_at and expires_at > issued_at:
        expires_in = expires_at - issued_at
    if expires_in is None and created_at and expires_at and expires_at > created_at:
        expires_in = expires_at - created_at
    expires_in = expires_in or CPA_DEFAULT_EXPIRES_IN

    last_refresh = _text(credential.get("last_refresh"))
    if not last_refresh:
        last_refresh = _rfc3339(issued_at or created_at or now_seconds)

    expired = _text(credential.get("expired"))
    if not expired:
        expired = _rfc3339(expires_at or ((issued_at or created_at or now_seconds) + expires_in))

    email = (
        _text(credential.get("email"))
        or _text(id_claims.get("email"))
        or _text(access_claims.get("email"))
    )
    subject = (
        _text(id_claims.get("sub"))
        or _text(access_claims.get("sub"))
        or _text(credential.get("principal_id"))
        or _text(credential.get("user_id"))
    )

    auth_raw = credential.get("auth_raw")
    auth_raw = auth_raw if isinstance(auth_raw, dict) else {}
    redirect_uri = (
        _text(credential.get("redirect_uri"))
        or _text(auth_raw.get("redirect_uri") or auth_raw.get("redirectURI"))
        or CPA_REDIRECT_URI
    )

    result = {
        "type": CPA_TYPE,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": _text(credential.get("token_type")) or "Bearer",
        "expires_in": expires_in,
        "expired": expired,
        "last_refresh": last_refresh,
        "base_url": _text(credential.get("base_url")) or CPA_BASE_URL,
        "redirect_uri": redirect_uri,
        "token_endpoint": _text(credential.get("token_endpoint")) or CPA_TOKEN_ENDPOINT,
        "auth_kind": CPA_AUTH_KIND,
    }
    if id_token:
        result["id_token"] = id_token
    if email:
        result["email"] = email
    if subject:
        result["sub"] = subject

    # Keep the same stable field ordering produced by CLIProxyAPI.
    order = (
        "type",
        "access_token",
        "refresh_token",
        "id_token",
        "token_type",
        "expires_in",
        "expired",
        "last_refresh",
        "email",
        "sub",
        "base_url",
        "redirect_uri",
        "token_endpoint",
        "auth_kind",
    )
    return {key: result[key] for key in order if key in result}


def cpa_credential_filename(credential: dict) -> str:
    value = _text(credential.get("email") or credential.get("sub"))
    value = re.sub(r"[^A-Za-z0-9@._-]", "-", value).strip("-")
    return f"xai-{value or int(time.time() * 1000)}.json"


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_cpa_download(
    credentials: list[dict],
    now: int | None = None,
) -> tuple[bytes, str, str]:
    """Return download bytes, filename, and content type for selected accounts."""
    if not credentials:
        raise ValueError("请选择要导出的账号")

    items = [build_cpa_credential(item, now=now) for item in credentials]
    if len(items) == 1:
        return _json_bytes(items[0]), cpa_credential_filename(items[0]), "application/json; charset=utf-8"

    output = io.BytesIO()
    used_names: dict[str, int] = {}
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in items:
            base_name = cpa_credential_filename(item)
            occurrence = used_names.get(base_name, 0) + 1
            used_names[base_name] = occurrence
            if occurrence > 1:
                base_name = f"{base_name[:-5]}-{occurrence}.json"
            archive.writestr(base_name, _json_bytes(item))

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now or time.time()))
    return output.getvalue(), f"xai-cpa-credentials-{stamp}.zip", "application/zip"
