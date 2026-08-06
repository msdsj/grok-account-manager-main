"""Persistent local mailbox pools used only for verification-code retrieval."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from ...mail.sources import OutlookAccount, parse_outlook_accounts
from ..config import OUTLOOK_MAILBOX_POOL_PATH

_MAX_POOL_BYTES = 1_000_000


def _entries(data: str) -> list[str]:
    lines = [line.strip() for line in data.splitlines() if line.strip()]
    if len(lines) == 1:
        return lines[0].split()
    return lines


def _summary(accounts: list[OutlookAccount]) -> list[dict[str, str]]:
    return [
        {
            "email": account.email,
            "mode": account.mode,
        }
        for account in accounts
    ]


def inspect_outlook_mailbox_pool(data: str) -> dict:
    normalized = str(data or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    accounts = parse_outlook_accounts(normalized)
    return {
        "count": len(accounts),
        "accounts": _summary(accounts),
        "invalid": max(0, len(_entries(normalized)) - len(accounts)),
    }


def load_outlook_mailbox_pool() -> dict:
    try:
        data = OUTLOOK_MAILBOX_POOL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        data = ""
    except OSError as error:
        raise RuntimeError(f"读取 Outlook 账号池失败: {error}") from error
    return {"data": data, **inspect_outlook_mailbox_pool(data)}


def _validate_outlook_mailbox_pool(data: str) -> tuple[str, list[OutlookAccount]]:
    normalized = str(data or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("Outlook 账号池不能为空")
    if len(normalized.encode("utf-8")) > _MAX_POOL_BYTES:
        raise ValueError("Outlook 账号池过大")

    entries = _entries(normalized)
    accounts = parse_outlook_accounts(normalized)
    invalid = len(entries) - len(accounts)
    if invalid:
        raise ValueError(
            f"账号池中有 {invalid} 条格式无效的记录；每行应为 "
            "email----password----clientId----refreshToken----auto/imap/graph"
        )

    emails = [account.email.lower() for account in accounts]
    duplicates = sorted({email for email in emails if emails.count(email) > 1})
    if duplicates:
        raise ValueError(f"账号池存在重复邮箱: {', '.join(duplicates[:3])}")
    return normalized + "\n", accounts


def _write_private_file(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            # Windows does not expose POSIX file permissions in the same way.
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def save_outlook_mailbox_pool(data: str) -> dict:
    normalized, accounts = _validate_outlook_mailbox_pool(data)
    try:
        _write_private_file(OUTLOOK_MAILBOX_POOL_PATH, normalized)
    except OSError as error:
        raise RuntimeError(f"保存 Outlook 账号池失败: {error}") from error
    return {"count": len(accounts), "accounts": _summary(accounts)}
