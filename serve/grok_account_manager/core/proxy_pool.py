"""Proxy list parsing and thread-safe random selection.

The browser accepts a single Chromium proxy server per process.  This module
keeps list handling separate from browser lifecycle code and deliberately does
not log the raw endpoint (proxy credentials, when present, are sensitive).
"""

from __future__ import annotations

import ipaddress
import random
import re
import threading
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

# DrissionPage's bundled Chromium option support only covers unauthenticated
# HTTP-family proxy URLs.  Keep validation aligned with what the browser can
# actually launch instead of accepting a pool that will fail at runtime.
SUPPORTED_PROXY_SCHEMES = frozenset({"http", "https"})
_PROXY_ARGUMENT_PATTERN = re.compile(r"(?i)(--proxy-server(?:=|\s+))[^\s'\"<>]+")
_PROXY_CREDENTIAL_URL_PATTERN = re.compile(
    r"(?i)(?:https?|socks[45])://[^\s/@]*@[^\s/\"'<>()]+"
)


class ProxyPoolError(ValueError):
    """Base error for malformed proxy pool input."""


class ProxyPoolExhaustedError(RuntimeError):
    """Raised when a pool has no unused proxy endpoint left."""


def redact_proxy_secrets(value: object) -> str:
    """Remove proxy credentials and command-line values from diagnostic text."""

    text = str(value)
    text = _PROXY_ARGUMENT_PATTERN.sub(r"\1<redacted>", text)
    return _PROXY_CREDENTIAL_URL_PATTERN.sub("代理地址（凭据已隐藏）", text)


def normalize_proxy_server(value: str) -> str:
    """Return a Chromium-compatible proxy URL.

    Plain ``HOST:PORT`` entries are treated as HTTP proxies.  Explicit HTTP
    and HTTPS proxy URLs are accepted.  Authentication and SOCKS proxy modes
    are rejected because the installed browser adapter does not support them.
    """

    raw = str(value or "").strip()
    if not raw:
        raise ProxyPoolError("代理地址为空")
    if any(character.isspace() for character in raw):
        raise ProxyPoolError("代理地址不能包含空白字符")

    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urlsplit(candidate)
    except ValueError as error:
        raise ProxyPoolError("代理地址格式无效") from error
    scheme = parsed.scheme.lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        supported = ", ".join(sorted(SUPPORTED_PROXY_SCHEMES))
        raise ProxyPoolError(f"代理协议不支持：{parsed.scheme or '(空)'}；支持 {supported}")
    if "@" in parsed.netloc:
        raise ProxyPoolError("代理地址不支持用户名或密码")
    if not parsed.hostname:
        raise ProxyPoolError("代理地址缺少主机")
    try:
        port = parsed.port
    except ValueError as error:
        raise ProxyPoolError("代理端口无效") from error
    if port is None or not 1 <= port <= 65535:
        raise ProxyPoolError("代理端口必须是 1-65535")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ProxyPoolError("代理地址不能包含路径、查询参数或片段")

    hostname = parsed.hostname.lower()
    try:
        hostname = ipaddress.ip_address(hostname).compressed
    except ValueError:
        pass
    if "%" in hostname:
        raise ProxyPoolError("代理地址不支持 IPv6 作用域")
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{scheme}://{display_host}:{port}"


def mask_proxy_server(value: str | None) -> str:
    """Return a short display-safe representation of a proxy endpoint."""

    raw = str(value or "").strip()
    if not raw:
        return "直连"
    try:
        normalized = normalize_proxy_server(raw)
        parsed = urlsplit(normalized)
        hostname = parsed.hostname or "?"
        if ":" in hostname:
            masked_host = "[IPv6]"
        elif hostname.count(".") == 3 and all(part.isdigit() for part in hostname.split(".")):
            parts = hostname.split(".")
            masked_host = f"{parts[0]}.***.***.{parts[-1]}"
        elif len(hostname) <= 3:
            masked_host = "***"
        else:
            masked_host = f"{hostname[:2]}***{hostname[-2:]}"
        port = parsed.port or "?"
        return f"{parsed.scheme}://{masked_host}:{port}"
    except (ProxyPoolError, ValueError):
        # Do not echo malformed input: callers often include the value in a
        # status event, and it can contain an accidentally pasted credential.
        return "代理（配置无效）"


def parse_proxy_lines(lines: Iterable[str], *, source: str = "代理列表") -> tuple[str, ...]:
    """Parse and validate proxy entries from an iterable of text lines.

    Empty lines and lines beginning with ``#`` are ignored.  An inline ``#``
    starts a comment.  Duplicate canonical endpoints are retained once, in
    first-seen order.
    """

    parsed: list[str] = []
    seen: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        text = str(line).split("#", 1)[0].strip()
        if not text:
            continue
        try:
            endpoint = normalize_proxy_server(text)
        except ProxyPoolError as error:
            raise ProxyPoolError(f"{source}第 {line_number} 行：{error}") from error
        if endpoint in seen:
            continue
        seen.add(endpoint)
        parsed.append(endpoint)
    return tuple(parsed)


def load_proxy_file(path: str | Path) -> tuple[str, ...]:
    """Read and parse a UTF-8 proxy list file."""

    source = Path(path).expanduser()
    text = source.read_text(encoding="utf-8-sig")
    return parse_proxy_lines(text.splitlines(), source=str(source))


class ProxyPool:
    """Thread-safe random, without-replacement proxy pool.

    ``acquire()`` permanently consumes an endpoint from this pool.  A new pool
    should be created for a new registration job when endpoint reuse is wanted.
    """

    def __init__(
        self,
        proxies: Iterable[str],
        *,
        rng: random.Random | random.SystemRandom | None = None,
    ) -> None:
        self._proxies = parse_proxy_lines(proxies)
        self._remaining = list(self._proxies)
        self._lock = threading.RLock()
        self._rng = rng or random.SystemRandom()

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        rng: random.Random | random.SystemRandom | None = None,
    ) -> "ProxyPool":
        return cls(load_proxy_file(path), rng=rng)

    @classmethod
    def from_lines(
        cls,
        lines: Iterable[str],
        *,
        rng: random.Random | random.SystemRandom | None = None,
    ) -> "ProxyPool":
        return cls(lines, rng=rng)

    @property
    def total(self) -> int:
        return len(self._proxies)

    @property
    def remaining(self) -> int:
        with self._lock:
            return len(self._remaining)

    @property
    def used(self) -> int:
        return self.total - self.remaining

    def __len__(self) -> int:
        return self.remaining

    def acquire(self) -> str:
        """Select one random unused endpoint, or raise when exhausted."""

        with self._lock:
            if not self._remaining:
                raise ProxyPoolExhaustedError("代理池已耗尽")
            index = self._rng.randrange(len(self._remaining))
            return self._remaining.pop(index)

    def try_acquire(self) -> str | None:
        """Select one endpoint, returning ``None`` when the pool is exhausted."""

        try:
            return self.acquire()
        except ProxyPoolExhaustedError:
            return None

    def summaries(self) -> tuple[str, ...]:
        """Return display-safe summaries for all configured endpoints."""

        return tuple(mask_proxy_server(proxy) for proxy in self._proxies)


__all__ = [
    "ProxyPool",
    "ProxyPoolError",
    "ProxyPoolExhaustedError",
    "SUPPORTED_PROXY_SCHEMES",
    "load_proxy_file",
    "mask_proxy_server",
    "normalize_proxy_server",
    "parse_proxy_lines",
    "redact_proxy_secrets",
]
