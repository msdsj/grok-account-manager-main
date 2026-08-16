"""Cloud Mail mailbox source.

Cloud Mail supports two authentication modes:

- a public token using ``/api/public/*``;
- a user login token using ``/api/login`` and account-scoped endpoints.

Every HTTP session is short lived and keeps the default TLS verification
enabled.  Authorization values are sent exactly as Cloud Mail expects them,
without a ``Bearer`` prefix.
"""

from __future__ import annotations

import re
import secrets
import string
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

from ..core.network import build_requests_session
from ..core.proxy_pool import get_fixed_egress_proxy
from .duckmail import extract_verification_code


LogCallback = Callable[..., None]
_RECIPIENT_KEYS = (
    "to",
    "toEmail",
    "mailTo",
    "receiver",
    "receivers",
    "recipient",
    "recipients",
    "address",
    "email",
    "envelope_to",
)


def parse_cloud_mail_domains(value: str) -> tuple[str, ...]:
    """Parse newline/comma separated domains into a stable unique tuple."""

    domains: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[\r\n,]+", str(value or "")):
        domain = item.strip().lower().lstrip("@")
        if not domain or domain in seen:
            continue
        if any(character.isspace() for character in domain) or "." not in domain:
            raise ValueError(f"Cloud Mail 邮箱域名无效: {item.strip() or '(空)'}")
        seen.add(domain)
        domains.append(domain)
    return tuple(domains)


def _random_text(length: int) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _log(callback: LogCallback | None, level: str, message: str, **extra: Any) -> None:
    print(message)
    if callback is None:
        return
    try:
        callback(level, message, **extra)
    except Exception:
        pass


def _positive_float(value: float, default: float, minimum: float = 0.1) -> float:
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return default


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _text_candidates(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for key in ("address", "email", "name", "value"):
            if value.get(key):
                result.extend(_text_candidates(value.get(key)))
        return result
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_text_candidates(item))
        return result
    return []


def _message_matches_recipient(message: dict[str, Any], email: str) -> bool:
    target = str(email or "").strip().lower()
    candidates: list[str] = []
    for key in _RECIPIENT_KEYS:
        if key in message:
            candidates.extend(_text_candidates(message.get(key)))
    if not target or not candidates:
        return True
    for item in candidates:
        normalized = item.strip().lower()
        if not normalized:
            continue
        addresses = {
            match.lower()
            for match in re.findall(
                r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+",
                normalized,
                re.IGNORECASE,
            )
        }
        if target == normalized or target in addresses:
            return True
    return False


def _message_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("list", "records", "rows", "items", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _payload_secrets(value: Any) -> tuple[str, ...]:
    secrets_found: list[str] = []

    def collect(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                normalized_key = str(key).strip().lower().replace("-", "_")
                if normalized_key in {"password", "token", "public_token"}:
                    secret = str(nested or "")
                    if secret:
                        secrets_found.append(secret)
                else:
                    collect(nested)
        elif isinstance(item, list):
            for nested in item:
                collect(nested)

    collect(value)
    return tuple(secrets_found)


class CloudMailRequestError(RuntimeError):
    """Cloud Mail returned a non-success HTTP status or business code."""

    def __init__(
        self,
        method: str,
        path: str,
        status_code: int,
        api_code: Any,
        detail: str,
    ) -> None:
        self.status_code = int(status_code or 0)
        self.api_code = _integer(api_code)
        self.unauthorized = self.status_code == 401 or self.api_code == 401
        super().__init__(
            f"Cloud Mail 请求失败: {method.upper()} {path}, "
            f"HTTP {self.status_code}, code={api_code}, body={detail}"
        )


class CloudMailSource:
    """Create disposable Cloud Mail mailboxes for registration rounds."""

    name = "cloud_mail"

    def __init__(
        self,
        api_base: str,
        public_token: str = "",
        login_email: str = "",
        login_password: str = "",
        domains: str = "",
        request_timeout: float = 30,
    ) -> None:
        normalized_base = str(api_base or "").strip().rstrip("/")
        if normalized_base.lower().endswith("/api"):
            normalized_base = normalized_base[:-4].rstrip("/")
        self.api_base = normalized_base
        self.public_token = str(public_token or "").strip()
        self.login_email = str(login_email or "").strip()
        self.login_password = str(login_password or "").strip()
        self.domains = parse_cloud_mail_domains(domains)
        self.request_timeout = _positive_float(request_timeout, 30.0)
        self._token_lock = threading.Lock()
        self._login_token = ""

        if not self.api_base or not self.domains:
            raise ValueError("Cloud Mail 缺少 API 地址或邮箱域名")
        if not self.public_token and not (self.login_email and self.login_password):
            raise ValueError("Cloud Mail 需要 Public Token，或登录邮箱和密码")

    def _next_domain(self) -> str:
        return secrets.choice(self.domains)

    def _new_session(self) -> requests.Session:
        return build_requests_session(proxy_url=get_fixed_egress_proxy())

    def _request(
        self,
        session: requests.Session,
        method: str,
        path: str,
        *,
        token: str = "",
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "grok-account-manager/0.1",
        }
        if token:
            headers["Authorization"] = token
        response = session.request(
            method.upper(),
            f"{self.api_base}{path}",
            headers=headers,
            params=params,
            json=payload,
            timeout=_positive_float(timeout or self.request_timeout, self.request_timeout),
        )
        try:
            data = response.json()
        except Exception:
            data = None
        api_code = data.get("code") if isinstance(data, dict) else None
        try:
            api_success = int(api_code) == 200
        except (TypeError, ValueError):
            api_success = False
        if response.status_code != 200 or not api_success:
            detail = str(getattr(response, "text", "") or "")[:300]
            if isinstance(data, dict):
                detail = str(data.get("message") or detail or data)[:300]
            for secret in (
                self.public_token,
                self.login_password,
                self._login_token,
                token,
                *_payload_secrets(payload),
            ):
                if secret:
                    detail = detail.replace(secret, "[REDACTED]")
            raise CloudMailRequestError(
                method,
                path,
                response.status_code,
                api_code,
                detail,
            )
        return data.get("data") if isinstance(data, dict) else None

    def _login(
        self,
        session: requests.Session,
        *,
        force: bool = False,
        stale_token: str = "",
    ) -> str:
        with self._token_lock:
            if self._login_token and (
                not force or (stale_token and self._login_token != stale_token)
            ):
                return self._login_token
            data = self._request(
                session,
                "POST",
                "/api/login",
                payload={
                    "email": self.login_email,
                    "password": self.login_password,
                },
            )
            token = str(data.get("token") or "").strip() if isinstance(data, dict) else ""
            if not token:
                raise RuntimeError("Cloud Mail 登录未返回 token")
            self._login_token = token
            return token

    def _user_request(
        self,
        session: requests.Session,
        method: str,
        path: str,
        *,
        token: str = "",
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> tuple[Any, str]:
        active_token = token or self._login(session)
        try:
            data = self._request(
                session,
                method,
                path,
                token=active_token,
                params=params,
                payload=payload,
                timeout=timeout,
            )
            return data, active_token
        except CloudMailRequestError as error:
            if not error.unauthorized:
                raise
        active_token = self._login(
            session,
            force=True,
            stale_token=active_token,
        )
        data = self._request(
            session,
            method,
            path,
            token=active_token,
            params=params,
            payload=payload,
            timeout=timeout,
        )
        return data, active_token

    def create_mailbox(self, log_callback: LogCallback | None = None) -> "CloudMailMailbox":
        email = f"{_random_text(12)}@{self._next_domain()}"
        password = _random_text(20)
        session = self._new_session()
        try:
            if self.public_token:
                self._request(
                    session,
                    "POST",
                    "/api/public/addUser",
                    token=self.public_token,
                    payload={"list": [{"email": email, "password": password}]},
                )
                auth_mode = "public"
                token = self.public_token
                account_id = 0
            else:
                data, token = self._user_request(
                    session,
                    "POST",
                    "/api/account/add",
                    payload={"email": email, "token": ""},
                )
                account_id = _integer(data.get("accountId")) if isinstance(data, dict) else 0
                if account_id <= 0:
                    raise RuntimeError("Cloud Mail 创建邮箱未返回 accountId")
                auth_mode = "user"
        finally:
            session.close()

        _log(
            log_callback,
            "info",
            f"[*] 使用 Cloud Mail 邮箱: {email}",
            email=email,
            stage="create_mailbox",
        )
        return CloudMailMailbox(
            source=self,
            email=email,
            password=password,
            auth_mode=auth_mode,
            token=token,
            account_id=account_id,
            log_callback=log_callback,
        )


class CloudMailMailbox:
    """One generated mailbox and its message cursor."""

    def __init__(
        self,
        *,
        source: CloudMailSource,
        email: str,
        password: str,
        auth_mode: str,
        token: str,
        account_id: int,
        log_callback: LogCallback | None,
    ) -> None:
        self._source = source
        self.email = email
        self.password = password
        self.auth_mode = auth_mode
        self._token = token
        self._account_id = account_id
        self._last_email_id = 0
        self._log_callback = log_callback

    def _fetch_messages(
        self,
        session: requests.Session,
        *,
        timeout: float,
    ) -> list[dict[str, Any]]:
        if self.auth_mode == "public":
            data = self._source._request(
                session,
                "POST",
                "/api/public/emailList",
                token=self._source.public_token,
                payload={
                    "toEmail": self.email,
                    "type": 0,
                    "isDel": 0,
                    "timeSort": "desc",
                    "num": 1,
                    "size": 20,
                },
                timeout=timeout,
            )
        else:
            data, self._token = self._source._user_request(
                session,
                "GET",
                "/api/email/latest",
                token=self._token,
                params={
                    "emailId": self._last_email_id,
                    "accountId": self._account_id,
                    "allReceive": 0,
                },
                timeout=timeout,
            )
        return _message_list(data)

    def _extract_new_code(self, messages: list[dict[str, Any]]) -> str | None:
        candidates = [
            item
            for item in messages
            if _integer(item.get("emailId") or item.get("id")) > self._last_email_id
            and _message_matches_recipient(item, self.email)
        ]
        candidates.sort(
            key=lambda item: (
                _integer(item.get("emailId") or item.get("id")),
                str(item.get("createTime") or item.get("created_at") or ""),
            ),
            reverse=True,
        )
        highest_seen = self._last_email_id
        for item in candidates:
            email_id = _integer(item.get("emailId") or item.get("id"))
            highest_seen = max(highest_seen, email_id)
            explicit_code = str(item.get("code") or "").strip()
            text_parts = [
                f"Verification code: {explicit_code}" if explicit_code else "",
                str(item.get("text") or item.get("textContent") or item.get("body") or ""),
            ]
            code = extract_verification_code(
                subject=str(item.get("subject") or ""),
                text="\n".join(part for part in text_parts if part),
                html_text=str(item.get("content") or item.get("html") or item.get("htmlContent") or ""),
                sender=str(item.get("sendEmail") or item.get("sendName") or item.get("from") or ""),
            )
            if code:
                self._last_email_id = highest_seen
                return code.replace("-", "")
        self._last_email_id = highest_seen
        return None

    @staticmethod
    def _sleep(interval: float, stop_event=None) -> None:
        deadline = time.monotonic() + max(0.0, interval)
        while True:
            if stop_event and stop_event.is_set():
                raise RuntimeError("任务已停止")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.2, remaining))

    def wait_for_code(
        self,
        timeout: int = 180,
        interval: int = 3,
        stop_event=None,
    ) -> str | None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        poll_interval = max(0.0, float(interval))
        session = self._source._new_session()
        try:
            while time.monotonic() < deadline:
                if stop_event and stop_event.is_set():
                    raise RuntimeError("任务已停止")
                remaining = max(0.1, deadline - time.monotonic())
                try:
                    messages = self._fetch_messages(
                        session,
                        timeout=min(self._source.request_timeout, remaining),
                    )
                    code = self._extract_new_code(messages)
                    if code:
                        _log(
                            self._log_callback,
                            "success",
                            "[*] Cloud Mail 已获取验证码",
                            email=self.email,
                            stage="wait_email_code",
                        )
                        return code
                except CloudMailRequestError as error:
                    if error.unauthorized:
                        raise
                    _log(
                        self._log_callback,
                        "warning",
                        f"[警告] Cloud Mail 读取失败，稍后重试: {error}",
                        email=self.email,
                        stage="wait_email_code",
                    )
                except requests.RequestException as error:
                    _log(
                        self._log_callback,
                        "warning",
                        f"[警告] Cloud Mail 网络暂时不可用，稍后重试: {type(error).__name__}",
                        email=self.email,
                        stage="wait_email_code",
                    )
                self._sleep(min(poll_interval, max(0.0, deadline - time.monotonic())), stop_event)
        finally:
            session.close()

        _log(
            self._log_callback,
            "error",
            "[Error] Cloud Mail 轮询超时，未获取到验证码",
            email=self.email,
            stage="wait_email_code",
        )
        return None


__all__ = [
    "CloudMailMailbox",
    "CloudMailRequestError",
    "CloudMailSource",
    "parse_cloud_mail_domains",
]
