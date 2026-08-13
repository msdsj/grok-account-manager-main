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

from .proxy_pool import normalize_proxy_server


def build_requests_session(
    *,
    proxy_url: str | None = None,
    user_agent: str | None = None,
) -> requests.Session:
    """Build a requests session pinned to one round's network identity.

    ``trust_env=False`` is intentional: ambient ``HTTP(S)_PROXY`` and
    ``NO_PROXY`` settings must not silently move only the Python half of a
    browser round to a different egress.
    """

    session = requests.Session()
    session.trust_env = False
    if proxy_url:
        normalized = normalize_proxy_server(proxy_url)
        session.proxies.update({"http": normalized, "https": normalized})
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
