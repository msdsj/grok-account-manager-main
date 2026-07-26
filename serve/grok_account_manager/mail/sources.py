"""邮箱源抽象：DuckMail、Outlook IMAP/Graph 和 Gmail IMAP 取码。"""

from __future__ import annotations

from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
import imaplib
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

import requests

from .duckmail import extract_verification_code, get_email_and_token, get_oai_code

MICROSOFT_CONSUMERS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
MICROSOFT_COMMON_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MICROSOFT_TOKEN_URL = MICROSOFT_CONSUMERS_TOKEN_URL
OUTLOOK_IMAP_HOST = "outlook.office365.com"
OUTLOOK_IMAP_PORT = 993
OUTLOOK_SCAN_DEPTH = 15
OUTLOOK_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
OUTLOOK_GRAPH_INBOX_KEY = "GRAPH:INBOX"
OUTLOOK_GRAPH_SCAN_DEPTH = 15
GOOGLE_IMAP_HOST = "imap.gmail.com"
GOOGLE_IMAP_PORT = 993
GOOGLE_SCAN_DEPTH = 15
OUTLOOK_FALLBACK_FOLDERS = (
    "INBOX",
    "Junk Email",
    "Junk",
    "Spam",
    "Archive",
    "Deleted Items",
    "垃圾邮件",
    "垃圾箱",
    "归档",
    "已删除邮件",
    "已删除项目",
)
GOOGLE_FALLBACK_FOLDERS = (
    "INBOX",
    "[Gmail]/Spam",
    "[Gmail]/All Mail",
    "Spam",
    "Junk",
    "All Mail",
    "垃圾邮件",
    "所有邮件",
)


class VerificationMailbox(Protocol):
    email: str

    def wait_for_code(self, timeout: int = 180, interval: int = 3, stop_event=None) -> str | None:
        ...


LogCallback = Callable[..., None]


class MailboxSource(Protocol):
    name: str

    def create_mailbox(self, log_callback=None) -> VerificationMailbox:
        ...


@dataclass(frozen=True)
class OutlookAccount:
    email: str
    password: str
    client_id: str
    refresh_token: str
    mode: str = "auto"


@dataclass(frozen=True)
class GoogleAccount:
    email: str
    password: str
    recovery_email: str = ""


def _split_by_dashes(line: str) -> list[str]:
    """按 Kiro 的 `----` 格式拆字段；多余横杠归还给前一字段。"""
    parts: list[str] = []
    last = 0
    for match in re.finditer(r"-{4,}", line):
        parts.append(line[last:match.start()] + "-" * (len(match.group(0)) - 4))
        last = match.end()
    parts.append(line[last:])
    return parts


def _split_account_fields(line: str) -> list[str]:
    """Split one account row.

    Supported formats:
    - email----password----clientId----refreshToken
    - email----password----clientId----refreshToken----imap/graph/auto
    - email|password|clientId|refreshToken|imap/graph/auto

    The dash format remains the first choice because refresh tokens can contain
    pipe-like text in rare copied exports, while the new pipe format is mainly
    for Google/Gmail account pools.
    """
    line = str(line or "").strip()
    if not line:
        return []
    if re.search(r"-{4,}", line):
        return [part.strip() for part in _split_by_dashes(line)]
    if "|" in line:
        return [part.strip() for part in line.split("|")]
    return [line]


def _normalize_outlook_mode(mode: str | None) -> str:
    normalized = str(mode or "").strip().lower()
    if normalized in {"imap", "graph", "auto"}:
        return normalized
    return "auto"


def _mail_log(log_callback: LogCallback | None, level: str, message: str, **extra) -> None:
    print(message)
    if log_callback is None:
        return
    try:
        log_callback(level, message, **extra)
    except Exception:
        pass


def _microsoft_oauth_error_message(label: str, status_code: int, data: dict) -> str:
    error = str(data.get("error") or "").strip()
    description = str(data.get("error_description") or data.get("raw") or data).strip()
    aadsts = ""
    match = re.search(r"\b(AADSTS\d+)\b", description)
    if match:
        aadsts = match.group(1)
        description = description[match.end():].lstrip(": ").strip()
    description = re.split(r"\s+(?:Trace ID|Correlation ID|Timestamp):", description, maxsplit=1)[0].strip()
    parts = [part for part in (error, aadsts) if part]
    code = "/".join(parts) if parts else f"HTTP {status_code}"
    if description:
        return f"{label} token 刷新失败 {status_code} {code}: {description}"
    return f"{label} token 刷新失败 {status_code} {code}"


def _is_terminal_microsoft_token_error(error: Exception | str | None) -> bool:
    text = str(error or "").lower()
    terminal_markers = (
        "invalid_grant",
        "aadsts7000012",
        "aadsts70000",
        "aadsts700082",
        "aadsts700084",
        "refresh token has expired",
        "refresh token is invalid",
        "grant was obtained for a different tenant",
    )
    return any(marker in text for marker in terminal_markers)


def _normalize_folder_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip()).lower()


def _looks_like_outlook_folder(name: str) -> bool:
    normalized = _normalize_folder_name(name)
    if not normalized:
        return False
    keywords = ("inbox", "junk", "spam", "archive", "deleted", "trash", "收件箱", "垃圾", "归档", "已删除")
    return any(keyword in normalized for keyword in keywords)


def _looks_like_google_folder(name: str) -> bool:
    normalized = _normalize_folder_name(name)
    if not normalized:
        return False
    keywords = ("inbox", "spam", "junk", "all mail", "收件箱", "垃圾", "所有邮件")
    return any(keyword in normalized for keyword in keywords)


def _decode_imap_list_name(raw_line: bytes | str) -> str:
    line = raw_line.decode("utf-8", errors="ignore") if isinstance(raw_line, bytes) else str(raw_line)
    quoted = re.findall(r'"([^"]+)"', line)
    if quoted:
        return quoted[-1].strip()
    parts = line.split()
    return parts[-1].strip().strip('"') if parts else ""


def _discover_outlook_folders(client: imaplib.IMAP4_SSL) -> list[str]:
    discovered: list[str] = []
    try:
        status, data = client.list()
        if status == "OK":
            for raw_line in data or []:
                folder = _decode_imap_list_name(raw_line)
                if folder:
                    discovered.append(folder)
    except Exception:
        pass

    preferred = [folder for folder in discovered if _looks_like_outlook_folder(folder)]
    source = preferred or discovered
    ordered: list[str] = []
    seen: set[str] = set()
    for folder in [*source, *OUTLOOK_FALLBACK_FOLDERS]:
        normalized = _normalize_folder_name(folder)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(folder)
    return ordered


def _discover_google_folders(client: imaplib.IMAP4_SSL) -> list[str]:
    discovered: list[str] = []
    try:
        status, data = client.list()
        if status == "OK":
            for raw_line in data or []:
                folder = _decode_imap_list_name(raw_line)
                if folder:
                    discovered.append(folder)
    except Exception:
        pass

    preferred = [folder for folder in discovered if _looks_like_google_folder(folder)]
    source = preferred or discovered
    ordered: list[str] = []
    seen: set[str] = set()
    for folder in [*source, *GOOGLE_FALLBACK_FOLDERS]:
        normalized = _normalize_folder_name(folder)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(folder)
    return ordered


def _select_outlook_folder_count(client: imaplib.IMAP4_SSL, folder: str) -> int | None:
    status, data = client.select(folder, readonly=True)
    if status != "OK":
        return None
    return int((data[0] or b"0").decode("ascii", errors="ignore") or "0")


def _load_outlook_folder_counts(account: OutlookAccount) -> dict[str, int]:
    mode = _normalize_outlook_mode(account.mode)
    errors: list[str] = []

    if mode in {"imap", "auto"}:
        try:
            return _load_outlook_imap_folder_counts(account)
        except Exception as error:
            if mode == "imap":
                raise
            errors.append(f"IMAP: {error}")

    if mode in {"graph", "auto"}:
        try:
            return _load_outlook_graph_folder_counts(account)
        except Exception as error:
            if mode == "graph":
                raise
            errors.append(f"Graph: {error}")

    if errors:
        raise RuntimeError("; ".join(errors))
    return {}


def _load_outlook_imap_folder_counts(account: OutlookAccount) -> dict[str, int]:
    access_token = refresh_outlook_token(account)
    client = _connect_outlook_imap(account, access_token)
    try:
        counts: dict[str, int] = {}
        for folder in _discover_outlook_folders(client):
            total = _select_outlook_folder_count(client, folder)
            if total is not None:
                counts[folder] = total
        return counts
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _load_outlook_graph_folder_counts(account: OutlookAccount) -> dict[str, int]:
    access_token = refresh_outlook_graph_token(account)
    total = _get_outlook_graph_inbox_count_with_token(access_token)
    return {OUTLOOK_GRAPH_INBOX_KEY: total}


def _load_google_folder_counts(account: GoogleAccount) -> dict[str, int]:
    client = _connect_google_imap(account)
    try:
        counts: dict[str, int] = {}
        for folder in _discover_google_folders(client):
            total = _select_outlook_folder_count(client, folder)
            if total is not None:
                counts[folder] = total
        return counts
    finally:
        try:
            client.logout()
        except Exception:
            pass


def parse_outlook_accounts(data: str) -> list[OutlookAccount]:
    data = (data or "").strip()
    if not data:
        return []

    accounts: list[OutlookAccount] = []

    def parse_entry(entry: str) -> None:
        entry = entry.strip()
        if not entry:
            return
        parts = _split_account_fields(entry)
        if len(parts) not in {4, 5} or not parts[0] or not parts[2] or not parts[3]:
            return
        accounts.append(
            OutlookAccount(
                email=parts[0],
                password=parts[1],
                client_id=parts[2],
                refresh_token=parts[3],
                mode=_normalize_outlook_mode(parts[4] if len(parts) == 5 else "auto"),
            )
        )

    lines = data.splitlines()
    if len(lines) == 1:
        for part in data.split():
            parse_entry(part)
    else:
        for line in lines:
            parse_entry(line)
    return accounts


def parse_google_accounts(data: str) -> list[GoogleAccount]:
    data = (data or "").strip()
    if not data:
        return []

    accounts: list[GoogleAccount] = []

    def parse_entry(entry: str) -> None:
        entry = entry.strip()
        if not entry:
            return
        parts = _split_account_fields(entry)
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return
        accounts.append(
            GoogleAccount(
                email=parts[0],
                password=parts[1],
                recovery_email=parts[2] if len(parts) > 2 else "",
            )
        )

    for line in data.splitlines():
        parse_entry(line)
    return accounts


def read_outlook_accounts_file(path: str | os.PathLike[str] | None) -> str:
    if not path:
        return ""
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise FileNotFoundError(f"Outlook 账号文件不存在: {file_path}")
    return file_path.read_text(encoding="utf-8")


def read_google_accounts_file(path: str | os.PathLike[str] | None) -> str:
    if not path:
        return ""
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise FileNotFoundError(f"Google 账号文件不存在: {file_path}")
    return file_path.read_text(encoding="utf-8")


def load_outlook_accounts_data(inline_data: str = "", file_path: str = "") -> str:
    inline_data = (inline_data or "").strip()
    if inline_data:
        return inline_data
    if file_path:
        return read_outlook_accounts_file(file_path)

    env_inline = os.environ.get("OUTLOOK_ACCOUNTS", "").strip()
    if env_inline:
        return env_inline

    env_file = os.environ.get("OUTLOOK_ACCOUNTS_FILE", "").strip()
    if env_file:
        return read_outlook_accounts_file(env_file)
    return ""


def load_google_accounts_data(inline_data: str = "", file_path: str = "") -> str:
    inline_data = (inline_data or "").strip()
    if inline_data:
        return inline_data
    if file_path:
        return read_google_accounts_file(file_path)

    env_inline = os.environ.get("GOOGLE_ACCOUNTS", "").strip()
    if env_inline:
        return env_inline

    env_file = os.environ.get("GOOGLE_ACCOUNTS_FILE", "").strip()
    if env_file:
        return read_google_accounts_file(env_file)
    return ""


class DuckMailMailbox:
    def __init__(self, email: str, token: str) -> None:
        self.email = email
        self._token = token

    @classmethod
    def create(cls) -> "DuckMailMailbox":
        email, token = get_email_and_token()
        if not email or not token:
            raise RuntimeError("获取 DuckMail 邮箱失败")
        return cls(email=email, token=token)

    def wait_for_code(self, timeout: int = 180, interval: int = 3, stop_event=None) -> str | None:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        return get_oai_code(self._token, self.email, stop_event=stop_event)


class DuckMailSource:
    name = "duckmail"

    def create_mailbox(self, log_callback=None) -> DuckMailMailbox:
        return DuckMailMailbox.create()


class OutlookAccountPool:
    name = "outlook"

    def __init__(self, accounts: list[OutlookAccount]) -> None:
        if not accounts:
            raise ValueError("Outlook 邮箱源需要至少 1 行账号，格式：邮箱----密码----clientId----refreshToken----auto/imap/graph")
        self._accounts = accounts
        self._lock = threading.Lock()
        self._next = 0

    @property
    def count(self) -> int:
        return len(self._accounts)

    def create_mailbox(self, log_callback=None) -> "OutlookMailbox":
        with self._lock:
            account = self._accounts[self._next % len(self._accounts)]
            self._next += 1
        mailbox = OutlookMailbox(account, log_callback=log_callback)
        mailbox.prepare()
        return mailbox


class OutlookMailbox:
    def __init__(self, account: OutlookAccount, log_callback: LogCallback | None = None) -> None:
        self.account = account
        self.email = account.email
        self._log_callback = log_callback
        self._folder_counts: dict[str, int] = {}

    def prepare(self) -> None:
        _mail_log(
            self._log_callback,
            "info",
            f"[*] 使用 Outlook 邮箱: {self.email}（认证模式: {_normalize_outlook_mode(self.account.mode)}）",
            email=self.email,
            stage="create_mailbox",
        )
        try:
            self._folder_counts = _load_outlook_folder_counts(self.account)
            inbox_count = self._folder_counts.get("INBOX", self._folder_counts.get(OUTLOOK_GRAPH_INBOX_KEY, 0))
            _mail_log(
                self._log_callback,
                "info",
                f"[*] Outlook 发送前邮件数: {inbox_count}",
                email=self.email,
                stage="create_mailbox",
            )
            if self._folder_counts:
                summary = ", ".join(f"{folder}={count}" for folder, count in self._folder_counts.items())
                _mail_log(
                    self._log_callback,
                    "info",
                    f"[*] Outlook 监控文件夹: {summary}",
                    email=self.email,
                    stage="create_mailbox",
                )
        except Exception as error:
            self._folder_counts = {}
            _mail_log(
                self._log_callback,
                "warning",
                f"[警告] 获取 Outlook 邮件数失败，改用 0 作为基线: {error}",
                email=self.email,
                stage="create_mailbox",
            )

    def wait_for_code(self, timeout: int = 180, interval: int = 5, stop_event=None) -> str | None:
        return wait_for_outlook_code(
            self.account,
            before_counts=self._folder_counts,
            timeout=timeout,
            interval=interval,
            stop_event=stop_event,
            log_callback=self._log_callback,
        )


class GoogleAccountPool:
    def __init__(self, accounts: list[GoogleAccount], name: str = "gmail") -> None:
        if not accounts:
            if name == "google":
                raise ValueError("Google 账号注册需要至少 1 行账号，格式：邮箱----密码----辅助邮箱(可选)")
            raise ValueError("Gmail 邮箱源需要至少 1 行账号，格式：邮箱----应用专用密码")
        self.name = name
        self.registration_mode = "google" if name == "google" else "email"
        self._accounts = accounts
        self._lock = threading.Lock()
        self._next = 0

    @property
    def count(self) -> int:
        return len(self._accounts)

    def create_mailbox(self, log_callback=None) -> "GoogleMailbox":
        with self._lock:
            account = self._accounts[self._next % len(self._accounts)]
            self._next += 1
        mailbox = GoogleMailbox(account, use_imap=self.registration_mode == "email")
        mailbox.prepare()
        return mailbox


class GoogleMailbox:
    def __init__(self, account: GoogleAccount, use_imap: bool = True) -> None:
        self.account = account
        self.email = account.email
        self.password = account.password
        self.recovery_email = account.recovery_email
        self._use_imap = use_imap
        self._folder_counts: dict[str, int] = {}

    def prepare(self) -> None:
        print(f"[*] 使用 Google 邮箱: {self.email}")
        if not self._use_imap:
            return
        try:
            self._folder_counts = _load_google_folder_counts(self.account)
            inbox_count = self._folder_counts.get("INBOX", 0)
            print(f"[*] Gmail 发送前邮件数: {inbox_count}")
            if self._folder_counts:
                summary = ", ".join(f"{folder}={count}" for folder, count in self._folder_counts.items())
                print(f"[*] Gmail 监控文件夹: {summary}")
        except Exception as error:
            self._folder_counts = {}
            print(f"[警告] 获取 Gmail 邮件数失败，改用 0 作为基线: {error}")

    def wait_for_code(self, timeout: int = 180, interval: int = 5, stop_event=None) -> str | None:
        return wait_for_google_code(
            self.account,
            before_counts=self._folder_counts,
            timeout=timeout,
            interval=interval,
            stop_event=stop_event,
        )


def build_mailbox_source(
    email_source: str = "duckmail",
    outlook_data: str = "",
    outlook_file: str = "",
    google_data: str = "",
    google_file: str = "",
) -> MailboxSource:
    source = (email_source or "duckmail").strip().lower()
    if source == "duckmail":
        return DuckMailSource()
    if source == "outlook":
        data = load_outlook_accounts_data(outlook_data, outlook_file)
        accounts = parse_outlook_accounts(data)
        return OutlookAccountPool(accounts)
    if source in {"gmail", "google"}:
        data = load_google_accounts_data(google_data, google_file)
        accounts = parse_google_accounts(data)
        return GoogleAccountPool(accounts, name=source)
    raise ValueError(f"未知邮箱源: {email_source}")


def refresh_outlook_token(account: OutlookAccount) -> str:
    return _refresh_microsoft_access_token(
        account=account,
        scope="https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
        token_urls=(MICROSOFT_CONSUMERS_TOKEN_URL,),
        label="Outlook IMAP",
    )


def refresh_outlook_graph_token(account: OutlookAccount) -> str:
    return _refresh_microsoft_access_token(
        account=account,
        scope="https://graph.microsoft.com/Mail.Read offline_access",
        token_urls=(MICROSOFT_COMMON_TOKEN_URL, MICROSOFT_CONSUMERS_TOKEN_URL),
        label="Outlook Graph",
    )


def _refresh_microsoft_access_token(
    account: OutlookAccount,
    scope: str,
    token_urls: tuple[str, ...],
    label: str,
) -> str:
    last_error = ""
    for token_url in token_urls:
        response = requests.post(
            token_url,
            data={
                "client_id": account.client_id,
                "refresh_token": account.refresh_token,
                "grant_type": "refresh_token",
                "scope": scope,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}
        if response.status_code != 200:
            last_error = _microsoft_oauth_error_message(label, response.status_code, data)
            if _is_terminal_microsoft_token_error(last_error):
                raise RuntimeError(last_error)
            continue
        token = str(data.get("access_token") or "").strip()
        if not token:
            last_error = f"{label} token 响应缺少 access_token"
            continue
        return token
    raise RuntimeError(last_error or f"{label} token 刷新失败")


def _outlook_graph_get(access_token: str, path: str, params: dict[str, str] | None = None) -> dict:
    response = requests.get(
        f"{OUTLOOK_GRAPH_BASE_URL}{path}",
        params=params or {},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Prefer": 'outlook.body-content-type="text"',
        },
        timeout=30,
    )
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}
    if not response.ok:
        raise RuntimeError(f"Outlook Graph 请求失败 {response.status_code}: {str(data)[:300]}")
    return data


def _get_outlook_graph_inbox_count_with_token(access_token: str) -> int:
    data = _outlook_graph_get(
        access_token,
        "/me/mailFolders/inbox",
        params={"$select": "totalItemCount"},
    )
    return int(data.get("totalItemCount") or 0)


def _xoauth2_auth_string(email: str, access_token: str) -> bytes:
    return f"user={email}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")


def _connect_outlook_imap(account: OutlookAccount, access_token: str) -> imaplib.IMAP4_SSL:
    client = imaplib.IMAP4_SSL(OUTLOOK_IMAP_HOST, OUTLOOK_IMAP_PORT, timeout=20)
    client.authenticate("XOAUTH2", lambda _: _xoauth2_auth_string(account.email, access_token))
    return client


def _connect_google_imap(account: GoogleAccount) -> imaplib.IMAP4_SSL:
    client = imaplib.IMAP4_SSL(GOOGLE_IMAP_HOST, GOOGLE_IMAP_PORT, timeout=20)
    client.login(account.email, account.password)
    return client


def get_outlook_inbox_count(account: OutlookAccount) -> int:
    counts = _load_outlook_folder_counts(account)
    return counts.get("INBOX", counts.get(OUTLOOK_GRAPH_INBOX_KEY, 0))


def get_google_inbox_count(account: GoogleAccount) -> int:
    return _load_google_folder_counts(account).get("INBOX", 0)


def wait_for_outlook_code(
    account: OutlookAccount,
    before_counts: dict[str, int] | None,
    timeout: int,
    interval: int,
    stop_event=None,
    log_callback: LogCallback | None = None,
) -> str | None:
    mode = _normalize_outlook_mode(account.mode)
    if mode == "graph":
        return _wait_for_outlook_graph_code(account, before_counts, timeout, interval, stop_event, log_callback)
    if mode == "imap":
        return _wait_for_outlook_imap_code(account, before_counts, timeout, interval, stop_event, log_callback)
    return _wait_for_outlook_auto_code(account, before_counts, timeout, interval, stop_event, log_callback)


def _wait_for_outlook_auto_code(
    account: OutlookAccount,
    before_counts: dict[str, int] | None,
    timeout: int,
    interval: int,
    stop_event=None,
    log_callback: LogCallback | None = None,
) -> str | None:
    _mail_log(
        log_callback,
        "info",
        f"[*] Outlook 自动模式等待验证码（IMAP + Graph）: {account.email}",
        email=account.email,
        stage="wait_email_code",
    )
    deadline = time.time() + timeout
    folder_counts = dict(before_counts or {})
    if not folder_counts:
        folder_counts = {"INBOX": 0}

    imap_access_token: str | None = None
    graph_access_token: str | None = None
    imap_terminal_error = False
    graph_terminal_error = False
    token_errors: list[str] = []
    attempt = 0

    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        attempt += 1
        if attempt == 1 or attempt % 3 == 0:
            remaining = max(0, int(deadline - time.time()))
            _mail_log(
                log_callback,
                "info",
                f"[*] 仍在等待 Outlook 验证码：{account.email}，剩余约 {remaining}s",
                email=account.email,
                stage="wait_email_code",
            )

        if imap_access_token is None and not imap_terminal_error:
            try:
                imap_access_token = refresh_outlook_token(account)
            except Exception as error:
                if _is_terminal_microsoft_token_error(error):
                    imap_terminal_error = True
                    token_errors.append(str(error))
                _mail_log(
                    log_callback,
                    "warning",
                    f"[警告] Outlook IMAP token 刷新失败，将尝试 Graph: {error}",
                    email=account.email,
                    stage="wait_email_code",
                )

        if imap_access_token:
            try:
                code = _scan_outlook_imap_once(account, imap_access_token, folder_counts, log_callback)
                if code:
                    return code
            except Exception as error:
                _mail_log(
                    log_callback,
                    "warning",
                    f"[警告] Outlook IMAP 读取失败，将尝试 Graph: {error}",
                    email=account.email,
                    stage="wait_email_code",
                )
                try:
                    imap_access_token = refresh_outlook_token(account)
                except Exception:
                    imap_access_token = None

        if graph_access_token is None and not graph_terminal_error:
            try:
                graph_access_token = refresh_outlook_graph_token(account)
            except Exception as error:
                if _is_terminal_microsoft_token_error(error):
                    graph_terminal_error = True
                    token_errors.append(str(error))
                _mail_log(
                    log_callback,
                    "warning",
                    f"[警告] Outlook Graph token 刷新失败: {error}",
                    email=account.email,
                    stage="wait_email_code",
                )

        if imap_terminal_error and graph_terminal_error:
            detail = "；".join(token_errors[-2:])
            raise RuntimeError(f"Outlook refresh_token 无效或租户不匹配，无法读取邮箱：{detail}")

        if graph_access_token:
            try:
                code = _scan_outlook_graph_once(graph_access_token, folder_counts, account.email, log_callback)
                if code:
                    return code
            except Exception as error:
                _mail_log(
                    log_callback,
                    "warning",
                    f"[警告] Outlook Graph 读取失败，稍后重试: {error}",
                    email=account.email,
                    stage="wait_email_code",
                )
                try:
                    graph_access_token = refresh_outlook_graph_token(account)
                except Exception:
                    graph_access_token = None

        time.sleep(interval)

    _mail_log(
        log_callback,
        "error",
        "[Error] Outlook 自动模式轮询超时，未获取到验证码",
        email=account.email,
        stage="wait_email_code",
    )
    return None


def _wait_for_outlook_imap_code(
    account: OutlookAccount,
    before_counts: dict[str, int] | None,
    timeout: int,
    interval: int,
    stop_event=None,
    log_callback: LogCallback | None = None,
) -> str | None:
    _mail_log(log_callback, "info", f"[*] Outlook IMAP 等待验证码: {account.email}", email=account.email, stage="wait_email_code")
    deadline = time.time() + timeout
    folder_counts = dict(before_counts or {})
    if not folder_counts:
        folder_counts = {"INBOX": 0}
    access_token = refresh_outlook_token(account)
    attempt = 0

    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        attempt += 1
        if attempt == 1 or attempt % 3 == 0:
            remaining = max(0, int(deadline - time.time()))
            _mail_log(log_callback, "info", f"[*] Outlook IMAP 仍在轮询，剩余约 {remaining}s", email=account.email, stage="wait_email_code")

        try:
            code = _scan_outlook_imap_once(account, access_token, folder_counts, log_callback)
            if code:
                return code
            time.sleep(interval)
        except Exception as error:
            _mail_log(log_callback, "warning", f"[警告] Outlook IMAP 读取失败，稍后重试: {error}", email=account.email, stage="wait_email_code")
            try:
                access_token = refresh_outlook_token(account)
            except Exception:
                pass
            time.sleep(interval)

    _mail_log(log_callback, "error", "[Error] Outlook IMAP 轮询超时，未获取到验证码", email=account.email, stage="wait_email_code")
    return None


def _wait_for_outlook_graph_code(
    account: OutlookAccount,
    before_counts: dict[str, int] | None,
    timeout: int,
    interval: int,
    stop_event=None,
    log_callback: LogCallback | None = None,
) -> str | None:
    _mail_log(log_callback, "info", f"[*] Outlook Graph 等待验证码: {account.email}", email=account.email, stage="wait_email_code")
    deadline = time.time() + timeout
    folder_counts = dict(before_counts or {})
    if not folder_counts:
        folder_counts = {OUTLOOK_GRAPH_INBOX_KEY: 0}
    access_token = refresh_outlook_graph_token(account)
    attempt = 0

    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        attempt += 1
        if attempt == 1 or attempt % 3 == 0:
            remaining = max(0, int(deadline - time.time()))
            _mail_log(log_callback, "info", f"[*] Outlook Graph 仍在轮询，剩余约 {remaining}s", email=account.email, stage="wait_email_code")

        try:
            code = _scan_outlook_graph_once(access_token, folder_counts, account.email, log_callback)
            if code:
                return code
            time.sleep(interval)
        except Exception as error:
            _mail_log(log_callback, "warning", f"[警告] Outlook Graph 读取失败，稍后重试: {error}", email=account.email, stage="wait_email_code")
            try:
                access_token = refresh_outlook_graph_token(account)
            except Exception:
                pass
            time.sleep(interval)

    _mail_log(log_callback, "error", "[Error] Outlook Graph 轮询超时，未获取到验证码", email=account.email, stage="wait_email_code")
    return None


def _normalize_verification_code(code: str) -> str:
    code = str(code or "").strip()
    return code.replace("-", "") if "-" in code else code


def _scan_outlook_imap_once(
    account: OutlookAccount,
    access_token: str,
    folder_counts: dict[str, int],
    log_callback: LogCallback | None = None,
) -> str | None:
    client: imaplib.IMAP4_SSL | None = None
    try:
        client = _connect_outlook_imap(account, access_token)
        for folder in _discover_outlook_folders(client):
            total = _select_outlook_folder_count(client, folder)
            if total is None or total <= 0:
                continue

            before_count = folder_counts.get(folder, 0)
            if total <= before_count:
                continue

            start = max(1, total - OUTLOOK_SCAN_DEPTH + 1)
            start = max(start, before_count + 1)

            if start > total:
                continue

            _mail_log(
                log_callback,
                "info",
                f"[*] Outlook 文件夹更新: {folder} {before_count} -> {total}",
                email=account.email,
                stage="wait_email_code",
            )

            for seq in range(total, start - 1, -1):
                subject, text, html = _fetch_message_content(client, seq)
                code = extract_verification_code(subject, text, html)
                if code:
                    code = _normalize_verification_code(code)
                    folder_counts[folder] = max(folder_counts.get(folder, 0), total)
                    _mail_log(
                        log_callback,
                        "success",
                        f"[*] Outlook IMAP 获取到验证码: {code}（文件夹: {folder}）",
                        email=account.email,
                        stage="wait_email_code",
                    )
                    return code
        return None
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def _scan_outlook_graph_once(
    access_token: str,
    folder_counts: dict[str, int],
    email: str = "",
    log_callback: LogCallback | None = None,
) -> str | None:
    before_count = folder_counts.get(OUTLOOK_GRAPH_INBOX_KEY, folder_counts.get("INBOX", 0))
    total = _get_outlook_graph_inbox_count_with_token(access_token)
    if total <= 0:
        return None

    start_count = max(before_count, 0)
    if total <= start_count:
        return None

    limit = max(1, total - start_count)
    limit = min(limit, OUTLOOK_GRAPH_SCAN_DEPTH)

    _mail_log(
        log_callback,
        "info",
        f"[*] Outlook Graph 收件箱更新: {start_count} -> {total}",
        email=email,
        stage="wait_email_code",
    )

    data = _outlook_graph_get(
        access_token,
        "/me/mailFolders/inbox/messages",
        params={
            "$top": str(limit),
            "$orderby": "receivedDateTime desc",
            "$select": "subject,bodyPreview,body,receivedDateTime",
        },
    )
    messages = data.get("value") or []
    for message in messages:
        if not isinstance(message, dict):
            continue
        subject = str(message.get("subject") or "")
        preview = str(message.get("bodyPreview") or "")
        body = message.get("body") if isinstance(message.get("body"), dict) else {}
        body_content = str(body.get("content") or "")
        code = extract_verification_code(subject, preview, body_content)
        if code:
            code = _normalize_verification_code(code)
            folder_counts[OUTLOOK_GRAPH_INBOX_KEY] = max(folder_counts.get(OUTLOOK_GRAPH_INBOX_KEY, 0), total)
            _mail_log(
                log_callback,
                "success",
                f"[*] Outlook Graph 获取到验证码: {code}",
                email=email,
                stage="wait_email_code",
            )
            return code
    return None


def wait_for_google_code(
    account: GoogleAccount,
    before_counts: dict[str, int] | None,
    timeout: int,
    interval: int,
    stop_event=None,
) -> str | None:
    print(f"[*] Gmail IMAP 等待验证码: {account.email}")
    deadline = time.time() + timeout
    folder_counts = dict(before_counts or {})
    if not folder_counts:
        folder_counts = {"INBOX": 0}

    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")

        client: imaplib.IMAP4_SSL | None = None
        try:
            client = _connect_google_imap(account)
            for folder in _discover_google_folders(client):
                total = _select_outlook_folder_count(client, folder)
                if total is None or total <= 0:
                    continue

                before_count = folder_counts.get(folder, 0)
                start = max(1, total - GOOGLE_SCAN_DEPTH + 1)
                if total > before_count:
                    start = max(start, before_count + 1)

                if start > total:
                    continue

                if total > before_count:
                    print(f"[*] Gmail 文件夹更新: {folder} {before_count} -> {total}")

                for seq in range(total, start - 1, -1):
                    subject, text, html = _fetch_message_content(client, seq)
                    code = extract_verification_code(subject, text, html)
                    if code:
                        if "-" in code:
                            code = code.replace("-", "")
                        folder_counts[folder] = max(folder_counts.get(folder, 0), total)
                        print(f"[*] Gmail IMAP 获取到验证码: {code}（文件夹: {folder}）")
                        return code
            time.sleep(interval)
        except Exception as error:
            print(f"[警告] Gmail IMAP 读取失败，稍后重试: {error}")
            time.sleep(interval)
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass

    print("[Error] Gmail IMAP 轮询超时，未获取到验证码")
    return None


def _fetch_message_content(client: imaplib.IMAP4_SSL, seq: int) -> tuple[str, str, str]:
    status, data = client.fetch(str(seq), "(BODY.PEEK[])")
    if status != "OK":
        raise RuntimeError(f"FETCH {seq} 失败: {status}")

    raw_parts: list[bytes] = []
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            raw_parts.append(item[1])
    if not raw_parts:
        return "", "", ""

    message = message_from_bytes(b"".join(raw_parts))
    subject = _decode_header_value(message.get("Subject", ""))
    text_parts: list[str] = []
    html_parts: list[str] = []
    _collect_message_text(message, text_parts, html_parts)
    return subject, "\n".join(text_parts), "\n".join(html_parts)


def _decode_header_value(value: str) -> str:
    decoded: list[str] = []
    for part, charset in decode_header(value or ""):
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return "".join(decoded)


def _decode_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _collect_message_text(message: Message, text_parts: list[str], html_parts: list[str]) -> None:
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            _collect_leaf_text(part, text_parts, html_parts)
        return
    _collect_leaf_text(message, text_parts, html_parts)


def _collect_leaf_text(part: Message, text_parts: list[str], html_parts: list[str]) -> None:
    content_type = (part.get_content_type() or "").lower()
    if content_type == "text/plain":
        text_parts.append(_decode_payload(part))
    elif content_type == "text/html":
        html_parts.append(_decode_payload(part))
