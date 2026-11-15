"""Persist mailbox source settings in the local project ``.env`` file."""

from __future__ import annotations

import os
import threading
from typing import Any

from dotenv import dotenv_values, set_key, unset_key

from ...core.browser import PROJECT_ROOT


ENV_PATH = PROJECT_ROOT / ".env"
_LOCK = threading.RLock()

def _read_value(values: dict[str, Any], key: str) -> str:
    """Read the file value first, falling back to the current process env."""

    if key in values and values[key] is not None:
        return str(values[key]).strip()
    return str(os.environ.get(key, "") or "").strip()


def _read_env_file() -> dict[str, Any]:
    try:
        return dict(dotenv_values(ENV_PATH))
    except OSError:
        return {}


def _auth_mode(values: dict[str, Any]) -> str:
    configured = _read_value(values, "CLOUD_MAIL_AUTH_MODE").lower()
    if configured in {"public", "login"}:
        return configured
    if _read_value(values, "CLOUD_MAIL_LOGIN_EMAIL") and _read_value(values, "CLOUD_MAIL_LOGIN_PASSWORD"):
        return "login"
    if _read_value(values, "CLOUD_MAIL_PUBLIC_TOKEN"):
        return "public"
    return "login"


def get_mail_config() -> dict[str, Any]:
    """Return non-secret mailbox settings and secret configured flags."""

    with _LOCK:
        values = _read_env_file()
        return {
            "emailSource": _read_value(values, "GROK_ACCOUNT_MANAGER_EMAIL_SOURCE") or "duckmail",
            "duckMailApiBase": _read_value(values, "DUCKMAIL_BASE_URL") or "https://api.duckmail.sbs",
            "duckMailDomain": _read_value(values, "DUCKMAIL_DOMAIN") or "@msdsj.cyou",
            "duckMailApiKeyConfigured": bool(_read_value(values, "DUCKMAIL_API_KEY")),
            "cloudMailApiBase": _read_value(values, "CLOUD_MAIL_API_BASE"),
            "cloudMailDomains": _read_value(values, "CLOUD_MAIL_DOMAINS"),
            "cloudMailAuthMode": _auth_mode(values),
            "cloudMailPublicTokenConfigured": bool(_read_value(values, "CLOUD_MAIL_PUBLIC_TOKEN")),
            "cloudMailLoginEmail": _read_value(values, "CLOUD_MAIL_LOGIN_EMAIL"),
            "cloudMailLoginPasswordConfigured": bool(_read_value(values, "CLOUD_MAIL_LOGIN_PASSWORD")),
        }


def _set_env_value(key: str, value: str) -> None:
    normalized = str(value or "").strip()
    set_key(ENV_PATH, key, normalized, quote_mode="auto")
    os.environ[key] = normalized


def _unset_env_value(key: str) -> None:
    if key in _read_env_file():
        unset_key(ENV_PATH, key)
    os.environ.pop(key, None)


def _set_optional_secret(key: str, value: str | None) -> None:
    """Update a secret only when the caller supplied a new non-empty value."""

    if value is not None and str(value).strip():
        _set_env_value(key, value)


def save_mail_config(data: dict[str, Any]) -> dict[str, Any]:
    """Save mailbox settings without returning any secret values."""

    auth_mode = str(data.get("cloudMailAuthMode") or "login").strip().lower()
    if auth_mode not in {"public", "login"}:
        raise ValueError("Cloud Mail 认证方式必须是 public 或 login")

    email_source = str(data.get("emailSource") or "").strip().lower()
    if email_source and email_source not in {"duckmail", "cloud_mail", "outlook", "google"}:
        raise ValueError(f"未知邮箱来源: {email_source}")

    with _LOCK:
        ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        if email_source:
            _set_env_value("GROK_ACCOUNT_MANAGER_EMAIL_SOURCE", email_source)

        _set_env_value("DUCKMAIL_BASE_URL", str(data.get("duckMailApiBase") or ""))
        _set_env_value("DUCKMAIL_DOMAIN", str(data.get("duckMailDomain") or ""))
        _set_optional_secret("DUCKMAIL_API_KEY", data.get("duckMailApiKey"))

        _set_env_value("CLOUD_MAIL_API_BASE", str(data.get("cloudMailApiBase") or ""))
        _set_env_value("CLOUD_MAIL_DOMAINS", str(data.get("cloudMailDomains") or ""))
        _set_env_value("CLOUD_MAIL_AUTH_MODE", auth_mode)
        _set_env_value("CLOUD_MAIL_LOGIN_EMAIL", str(data.get("cloudMailLoginEmail") or ""))

        if auth_mode == "login":
            _set_optional_secret("CLOUD_MAIL_LOGIN_PASSWORD", data.get("cloudMailLoginPassword"))
            # Prevent an old public token from taking precedence over account login.
            _unset_env_value("CLOUD_MAIL_PUBLIC_TOKEN")
        else:
            _set_optional_secret("CLOUD_MAIL_PUBLIC_TOKEN", data.get("cloudMailPublicToken"))

        try:
            os.chmod(ENV_PATH, 0o600)
        except OSError:
            pass

        return get_mail_config()
