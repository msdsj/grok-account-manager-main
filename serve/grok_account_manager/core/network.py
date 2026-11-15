"""Per-round outbound HTTP context for browser-backed registration.

The registration page is driven by a real Chromium process, but a few
follow-up calls (OIDC discovery, device-token polling and account enrichment)
still use Python HTTP.  This module gives those calls the same egress proxy
and browser User-Agent for the lifetime of one registration round.

The helper is deliberately small and returns exactly one ``requests.Session``.
Callers that do not pass that session keep the historical module-level
``requests`` behavior, which preserves compatibility for maintenance jobs and
stand-alone integrations.
"""

from __future__ import annotations

from typing import Any

import requests

from .proxy_pool import get_fixed_egress_proxy, normalize_proxy_server


def build_requests_session(
    *,
    proxy_url: str | None = None,
    user_agent: str | None = None,
) -> requests.Session:
    """Build a requests session pinned to one round's network identity.

    ``trust_env`` follows the caller's intent: it is disabled **only** when an
    explicit proxy is supplied, so ambient ``HTTP(S)_PROXY`` / ``NO_PROXY``
    cannot silently move the Python half of the round to a different egress
    than the browser. Without an explicit proxy the session keeps
    ``trust_env=True`` so the system proxy still works (manual xAI OAuth
    relies on that same route).

    没有显式传代理时，会套用默认出口（本地 VPN，通常是 127.0.0.1:7890）。
    这一步很关键：Chrome 会自动继承 macOS 的系统代理设置，而 ``requests``
    只认 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 环境变量、不认系统代理。两边不一致
    就会出现「浏览器注册走代理、网关调用却是本机直连」——服务端看到同一个
    账号在两个出口 IP 之间跳变，和设备指纹不一致是同一类问题。
    """

    session = requests.Session()
    # Browser rounds may rely on the local system proxy (manual xAI OAuth
    # works through that same route). Only disable ambient proxy variables when
    # an explicit per-round proxy was supplied.
    session.trust_env = not bool(proxy_url)
    if proxy_url:
        normalized = normalize_proxy_server(proxy_url)
        session.proxies.update({"http": normalized, "https": normalized})
    else:
        default_proxy = get_fixed_egress_proxy()
        if default_proxy:
            session.proxies.update({"http": default_proxy, "https": default_proxy})
    if user_agent:
        browser_user_agent = str(user_agent).strip()
        session.headers.update({"User-Agent": browser_user_agent})
        session._grok_browser_user_agent = browser_user_agent
    else:
        session._grok_browser_user_agent = ""
    return session


def build_browser_http_session(browser_session: Any) -> requests.Session:
    """Capture one running browser round's proxy and real User-Agent."""

    proxy_url = getattr(browser_session, "proxy_server", None)
    get_user_agent = getattr(browser_session, "get_user_agent", None)
    if callable(get_user_agent):
        user_agent = str(get_user_agent() or "").strip()
    else:
        try:
            page = getattr(browser_session, "page", None)
            user_agent = str(
                page.run_js("return navigator.userAgent;") if page is not None else ""
            ).strip()
        except Exception:
            user_agent = ""
    return build_requests_session(
        proxy_url=str(proxy_url).strip() if proxy_url else None,
        user_agent=user_agent or None,
    )


__all__ = [
    "build_browser_http_session",
    "build_requests_session",
]
