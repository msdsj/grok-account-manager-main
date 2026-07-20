"""邮箱源抽象：DuckMail  和 Outlook IMAP 取码。"""

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
from typing import Protocol

import requests

from .duckmail import extract_verification_code, get_email_and_token, get_oai_code

MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
OUTLOOK_IMAP_HOST = "outlook.office365.com"
OUTLOOK_IMAP_PORT = 993
OUTLOOK_SCAN_DEPTH = 15
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


class VerificationMailbox(Protocol):
    email: str

    def wait_for_code(self, timeout: int = 180, interval: int = 3, stop_event=None) -> str | None:
        ...


class MailboxSource(Protocol):
    name: str

    def create_mailbox(self) -> VerificationMailbox:
        ...


@dataclass(frozen=True)
class OutlookAccount:
    email: str
    password: str
    client_id: str
    refresh_token: str


def _split_by_dashes(line: str) -> list[str]:
    """按 Kiro 的 `----` 格式拆字段；多余横杠归还给前一字段。"""
    parts: list[str] = []
    last = 0
    for match in re.finditer(r"-{4,}", line):
        parts.append(line[last:match.start()] + "-" * (len(match.group(0)) - 4))
        last = match.end()
    parts.append(line[last:])
    return parts


def _normalize_folder_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip()).lower()


def _looks_like_outlook_folder(name: str) -> bool:
    normalized = _normalize_folder_name(name)
    if not normalized:
        return False
    keywords = ("inbox", "junk", "spam", "archive", "deleted", "trash", "收件箱", "垃圾", "归档", "已删除")
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


def _select_outlook_folder_count(client: imaplib.IMAP4_SSL, folder: str) -> int | None:
    status, data = client.select(folder, readonly=True)
    if status != "OK":
        return None
    return int((data[0] or b"0").decode("ascii", errors="ignore") or "0")


def _load_outlook_folder_counts(account: OutlookAccount) -> dict[str, int]:
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


def parse_outlook_accounts(data: str) -> list[OutlookAccount]:
    data = (data or "").strip()
    if not data:
        return []

    accounts: list[OutlookAccount] = []

    def parse_entry(entry: str) -> None:
        entry = entry.strip()
        if not entry:
            return
        parts = [part.strip() for part in _split_by_dashes(entry)]
        if len(parts) != 4 or not parts[0] or not parts[2] or not parts[3]:
            return
        accounts.append(
            OutlookAccount(
                email=parts[0],
                password=parts[1],
                client_id=parts[2],
                refresh_token=parts[3],
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


def read_outlook_accounts_file(path: str | os.PathLike[str] | None) -> str:
    if not path:
        return ""
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise FileNotFoundError(f"Outlook 账号文件不存在: {file_path}")
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
        # DuckMail 原函数内部已有 180s 轮询；保留原行为以降低改动风险。
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        return get_oai_code(self._token, self.email)


class DuckMailSource:
    name = "duckmail"

    def create_mailbox(self) -> DuckMailMailbox:
        return DuckMailMailbox.create()


class OutlookAccountPool:
    name = "outlook"

    def __init__(self, accounts: list[OutlookAccount]) -> None:
        if not accounts:
            raise ValueError("Outlook 邮箱源需要至少 1 行账号，格式：邮箱----密码----clientId----refreshToken")
        self._accounts = accounts
        self._lock = threading.Lock()
        self._next = 0

    @property
    def count(self) -> int:
        return len(self._accounts)

    def create_mailbox(self) -> "OutlookMailbox":
        with self._lock:
            account = self._accounts[self._next % len(self._accounts)]
            self._next += 1
        mailbox = OutlookMailbox(account)
        mailbox.prepare()
        return mailbox


class OutlookMailbox:
    def __init__(self, account: OutlookAccount) -> None:
        self.account = account
        self.email = account.email
        self._folder_counts: dict[str, int] = {}

    def prepare(self) -> None:
        print(f"[*] 使用 Outlook 邮箱: {self.email}")
        try:
            self._folder_counts = _load_outlook_folder_counts(self.account)
            inbox_count = self._folder_counts.get("INBOX", 0)
            print(f"[*] Outlook 发送前邮件数: {inbox_count}")
            if self._folder_counts:
                summary = ", ".join(f"{folder}={count}" for folder, count in self._folder_counts.items())
                print(f"[*] Outlook 监控文件夹: {summary}")
        except Exception as error:
            self._folder_counts = {}
            print(f"[警告] 获取 Outlook 邮件数失败，改用 0 作为基线: {error}")

    def wait_for_code(self, timeout: int = 180, interval: int = 5, stop_event=None) -> str | None:
        return wait_for_outlook_code(
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
) -> MailboxSource:
    source = (email_source or "duckmail").strip().lower()
    if source == "duckmail":
        return DuckMailSource()
    if source == "outlook":
        data = load_outlook_accounts_data(outlook_data, outlook_file)
        accounts = parse_outlook_accounts(data)
        return OutlookAccountPool(accounts)
    raise ValueError(f"未知邮箱源: {email_source}")


def refresh_outlook_token(account: OutlookAccount) -> str:
    response = requests.post(
        MICROSOFT_TOKEN_URL,
        data={
            "client_id": account.client_id,
            "refresh_token": account.refresh_token,
            "grant_type": "refresh_token",
            "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}
    if response.status_code != 200:
        raise RuntimeError(f"Outlook token 刷新失败 {response.status_code}: {str(data)[:300]}")
    token = str(data.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("Outlook token 响应缺少 access_token")
    return token


def _xoauth2_auth_string(email: str, access_token: str) -> bytes:
    return f"user={email}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")


def _connect_outlook_imap(account: OutlookAccount, access_token: str) -> imaplib.IMAP4_SSL:
    client = imaplib.IMAP4_SSL(OUTLOOK_IMAP_HOST, OUTLOOK_IMAP_PORT, timeout=20)
    client.authenticate("XOAUTH2", lambda _: _xoauth2_auth_string(account.email, access_token))
    return client


def get_outlook_inbox_count(account: OutlookAccount) -> int:
    return _load_outlook_folder_counts(account).get("INBOX", 0)


def wait_for_outlook_code(
    account: OutlookAccount,
    before_counts: dict[str, int] | None,
    timeout: int,
    interval: int,
    stop_event=None,
) -> str | None:
    print(f"[*] Outlook IMAP 等待验证码: {account.email}")
    deadline = time.time() + timeout
    folder_counts = dict(before_counts or {})
    if not folder_counts:
        folder_counts = {"INBOX": 0}
    access_token = refresh_outlook_token(account)

    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")

        client: imaplib.IMAP4_SSL | None = None
        try:
            client = _connect_outlook_imap(account, access_token)
            for folder in _discover_outlook_folders(client):
                total = _select_outlook_folder_count(client, folder)
                if total is None or total <= 0:
                    continue

                before_count = folder_counts.get(folder, 0)
                start = max(1, total - OUTLOOK_SCAN_DEPTH + 1)
                if total > before_count:
                    start = max(start, before_count + 1)

                if start > total:
                    continue

                if total > before_count:
                    print(f"[*] Outlook 文件夹更新: {folder} {before_count} -> {total}")

                for seq in range(total, start - 1, -1):
                    subject, text, html = _fetch_message_content(client, seq)
                    code = extract_verification_code(subject, text, html)
                    if code:
                        if "-" in code:
                            code = code.replace("-", "")
                        folder_counts[folder] = max(folder_counts.get(folder, 0), total)
                        print(f"[*] Outlook IMAP 获取到验证码: {code}（文件夹: {folder}）")
                        return code
            time.sleep(interval)
        except Exception as error:
            print(f"[警告] Outlook IMAP 读取失败，稍后重试: {error}")
            try:
                access_token = refresh_outlook_token(account)
            except Exception:
                pass
            time.sleep(interval)
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass

    print("[Error] Outlook IMAP 轮询超时，未获取到验证码")
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
