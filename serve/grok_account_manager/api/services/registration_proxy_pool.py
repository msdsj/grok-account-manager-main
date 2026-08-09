"""Persistent local proxy nodes used by the registration browser workers."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from ...core.proxy_pool import ProxyPoolError, mask_proxy_server, parse_proxy_lines
from ..config import REGISTRATION_PROXY_POOL_PATH

_MAX_POOL_BYTES = 1_000_000
_MAX_POOL_ENTRIES = 20_000
_LOCK = threading.RLock()


def _write_private_file(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_locked() -> tuple[str, ...]:
    try:
        raw = REGISTRATION_PROXY_POOL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except OSError as error:
        raise RuntimeError(f"读取已保存注册节点失败: {error}") from error

    if not raw.strip():
        return ()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"已保存注册节点文件格式错误: {error}") from error
    values = document.get("proxies") if isinstance(document, dict) else None
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise RuntimeError("已保存注册节点文件结构无效")
    if len(values) > _MAX_POOL_ENTRIES:
        raise RuntimeError("已保存注册节点数量超出限制")
    try:
        return parse_proxy_lines(values, source="已保存注册节点")
    except ProxyPoolError as error:
        raise RuntimeError(str(error)) from error


def load_saved_registration_proxies() -> tuple[str, ...]:
    """Return canonical saved endpoints for use by a newly started job."""
    with _LOCK:
        return _read_locked()


def registration_proxy_pool_snapshot() -> dict:
    """Return a display-safe view. Raw proxy URLs never leave the local API."""
    with _LOCK:
        proxies = _read_locked()
    return {
        "count": len(proxies),
        "items": [mask_proxy_server(proxy) for proxy in proxies],
    }


def save_registration_proxy_nodes(data: str, *, replace: bool = False) -> dict:
    """Validate imported endpoints and atomically persist a deduplicated pool."""
    normalized = str(data or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("节点列表不能为空")
    if len(normalized.encode("utf-8")) > _MAX_POOL_BYTES:
        raise ValueError("节点列表过大")
    try:
        imported = parse_proxy_lines(normalized.splitlines(), source="导入节点")
    except ProxyPoolError as error:
        raise ValueError(str(error)) from error
    if not imported:
        raise ValueError("节点列表不能为空")

    with _LOCK:
        existing = () if replace else _read_locked()
        merged = tuple(dict.fromkeys((*existing, *imported)))
        if len(merged) > _MAX_POOL_ENTRIES:
            raise ValueError(f"节点数量不能超过 {_MAX_POOL_ENTRIES}")
        document = {
            "version": 1,
            "proxies": list(merged),
        }
        try:
            _write_private_file(
                REGISTRATION_PROXY_POOL_PATH,
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            )
        except OSError as error:
            raise RuntimeError(f"保存注册节点失败: {error}") from error

    return {
        "count": len(merged),
        "added": len(merged) if replace else len(merged) - len(existing),
        "skipped": len(imported) - (len(merged) if replace else len(merged) - len(existing)),
        "items": [mask_proxy_server(proxy) for proxy in merged],
    }


def clear_saved_registration_proxies() -> dict:
    """Remove all locally saved registration nodes."""
    with _LOCK:
        existing = _read_locked()
        try:
            REGISTRATION_PROXY_POOL_PATH.unlink(missing_ok=True)
        except OSError as error:
            raise RuntimeError(f"清空注册节点失败: {error}") from error
    return {"count": 0, "removed": len(existing), "items": []}
