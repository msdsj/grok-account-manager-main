"""使用 xAI Device OAuth Flow 换取 Grok OAuth tokens。

cockpit-tools 的 Grok refresh_token 获取流程走的是 xAI device flow，而不是
PKCE loopback callback。这里复用注册完成后的同一个独立浏览器登录态，打开
device 授权页并自动推进登录。xAI 最终 consent 必须用浏览器真实鼠标事件点击
提交按钮，才能把按钮自身的 action 一起提交；直接调用 form.requestSubmit() 会得到
Invalid action。授权完成后轮询 token endpoint 获取 OAuth tokens。
"""

from __future__ import annotations

import threading
import time
from functools import lru_cache
from typing import Any, Callable, TypedDict
from urllib.parse import quote, urljoin, urlparse

import requests

from ..mail.duckmail import get_oai_code


def _http_client(http_session: Any = None):
    """Return the round-pinned HTTP client, or the legacy module client."""

    return http_session if http_session is not None else requests


class OAuthTokens(TypedDict, total=False):
    """OAuth token 响应。"""

    access_token: str
    refresh_token: str
    id_token: str
    token_type: str
    expires_in: int
    token_endpoint: str
    redirect_uri: str


OIDC_ISSUER = "https://auth.x.ai"
OIDC_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_SCOPE = (
    "openid profile email offline_access grok-cli:access api:access "
    "conversations:read conversations:write"
)

DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
DEVICE_AUTHORIZATION_ENDPOINT = "https://auth.x.ai/oauth2/device/code"
TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

DEFAULT_INTERVAL_SECONDS = 5
MAX_LOGIN_SECONDS = 30 * 60
MAX_CONSECUTIVE_POLL_TRANSPORT_ERRORS = 3
MAX_CONSECUTIVE_PAGE_ERRORS = 6
DISCOVERY_TIMEOUT_SECONDS = 8
DEVICE_CODE_TIMEOUT_SECONDS = 15
OAUTH_ENDPOINT_CACHE_TTL_SECONDS = 10 * 60

_SESSION_ENDPOINT_CACHE_LOCK = threading.Lock()
_SESSION_ENDPOINT_CACHE: tuple[float, dict[str, str | None]] | None = None


class OAuthTerminalError(RuntimeError):
    """Device Flow 已被服务端终止，重新申请 code 不会恢复。"""


class CloudflareBlockedError(OAuthTerminalError):
    """Cloudflare 已明确拒绝当前浏览器，继续等待或重试没有意义。"""


def _validate_xai_endpoint(raw: str | None, field: str, fallback: str | None = None) -> str:
    value = str(raw or fallback or "").strip()
    if not value:
        raise RuntimeError(f"Grok OAuth {field} 为空")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or (host != "x.ai" and not host.endswith(".x.ai")):
        if fallback:
            print(f"[OAuth Exchange] discovery 的 {field} 非 x.ai HTTPS，使用默认端点")
            return fallback
        raise RuntimeError(f"Grok OAuth {field} URL 无效: {value}")
    return value


def _positive_int(value, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


@lru_cache(maxsize=1)
def _discover_oauth_endpoints_cached(timeout: int = 30) -> dict[str, str | None]:
    """读取 OIDC discovery；失败时回退到 cockpit-tools 使用的已知端点。"""
    return _discover_oauth_endpoints_uncached(requests, timeout=timeout)


def _discover_oauth_endpoints_uncached(http_client: Any, timeout: int = 30) -> dict[str, str | None]:
    """Read OIDC discovery through a caller-selected HTTP client."""
    fallback: dict[str, str | None] = {
        "device_authorization_endpoint": DEVICE_AUTHORIZATION_ENDPOINT,
        "token_endpoint": TOKEN_ENDPOINT,
        "userinfo_endpoint": None,
    }
    try:
        response = http_client.get(
            DISCOVERY_URL,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        return {
            "device_authorization_endpoint": _validate_xai_endpoint(
                data.get("device_authorization_endpoint"),
                "device_authorization_endpoint",
                DEVICE_AUTHORIZATION_ENDPOINT,
            ),
            "token_endpoint": _validate_xai_endpoint(
                data.get("token_endpoint"),
                "token_endpoint",
                TOKEN_ENDPOINT,
            ),
            "userinfo_endpoint": _validate_xai_endpoint(
                data.get("userinfo_endpoint"),
                "userinfo_endpoint",
                None,
            )
            if data.get("userinfo_endpoint")
            else None,
        }
    except Exception as e:
        print(f"[OAuth Exchange] 读取 discovery 失败，使用默认端点: {e}")
        return fallback


def _discover_oauth_endpoints(
    timeout: int = 30,
    http_session: Any = None,
) -> dict[str, str | None]:
    """Read OIDC endpoints while reusing the static discovery response briefly.

    A real registration round still uses its pinned HTTP session for the device
    request and token polling.  Discovery only publishes endpoint URLs, so a
    short process cache avoids one extra auth.x.ai round trip for every account.
    Test doubles and non-requests clients intentionally keep the old uncached
    behavior so callers can validate their own egress context.
    """

    global _SESSION_ENDPOINT_CACHE

    if http_session is None:
        return _discover_oauth_endpoints_cached(timeout)
    if isinstance(http_session, requests.Session):
        now = time.monotonic()
        with _SESSION_ENDPOINT_CACHE_LOCK:
            cached = _SESSION_ENDPOINT_CACHE
            if cached is not None and now - cached[0] < OAUTH_ENDPOINT_CACHE_TTL_SECONDS:
                return dict(cached[1])
        result = _discover_oauth_endpoints_uncached(http_session, timeout=timeout)
        with _SESSION_ENDPOINT_CACHE_LOCK:
            _SESSION_ENDPOINT_CACHE = (time.monotonic(), dict(result))
        return result
    return _discover_oauth_endpoints_uncached(http_session, timeout=timeout)


def _request_device_code(
    device_authorization_endpoint: str,
    timeout: int = 30,
    http_session: Any = None,
) -> dict:
    response = _http_client(http_session).post(
        device_authorization_endpoint,
        data={
            "client_id": OIDC_CLIENT_ID,
            "scope": OIDC_SCOPE,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not str(data.get("device_code") or "").strip():
        raise RuntimeError("xAI device flow 响应缺少 device_code")
    if not str(data.get("user_code") or "").strip():
        raise RuntimeError("xAI device flow 响应缺少 user_code")
    return data


def _verification_url(device_data: dict, prefer_email_login: bool = True) -> str:
    complete = str(device_data.get("verification_uri_complete") or "").strip()
    if complete:
        return _validate_xai_endpoint(complete, "verification_uri_complete")

    user_code = str(device_data["user_code"]).strip()
    return_to = quote(f"/oauth2/device?user_code={user_code}", safe="")
    url = (
        "https://accounts.x.ai/sign-in"
        f"?redirect=oauth2-provider&return_to={return_to}"
    )
    if prefer_email_login:
        url += "&email=true"
    return url


def _device_redirect_state(location: str | None, base_url: str) -> str:
    """Classify the auth.x.ai redirect without loading accounts.x.ai in Chrome."""

    raw_location = str(location or "").strip()
    if not raw_location:
        return ""
    try:
        parsed = urlparse(urljoin(base_url, raw_location))
    except Exception:
        return ""
    host = (parsed.hostname or "").lower()
    if host != "x.ai" and not host.endswith(".x.ai"):
        return ""
    path = parsed.path.rstrip("/").lower()
    if path == "/oauth2/device/consent":
        return "consent"
    if path == "/oauth2/device/done":
        return "done"
    if path in {"/sign-in", "/signin", "/login"}:
        return "sign-in"
    return ""


def _try_direct_device_authorization(
    http_session: Any,
    user_code: str,
    sso_token: str | None,
    timeout: int = DEVICE_CODE_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """Approve a device code through xAI's same-session HTTP flow.

    A newly registered account already has the ``sso`` cookie. xAI accepts that
    cookie on ``device/verify`` and ``device/approve``, so there is no reason to
    open a second browser page or wait for the Build consent UI. If the direct
    flow is rejected (Cloudflare, changed endpoint, missing cookie), callers
    fall back to the browser driver below.
    """

    token = str(sso_token or "").strip()
    code = str(user_code or "").strip()
    if http_session is None or not token or not code:
        return False, "缺少同一浏览器会话或 sso cookie"

    try:
        # requests.Session keeps these cookies for both auth.x.ai and the
        # accounts.x.ai redirect while preserving the round's pinned proxy.
        http_session.cookies.set("sso", token, domain=".x.ai", path="/")
        http_session.cookies.set("sso-rw", token, domain=".x.ai", path="/")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        verify = http_session.post(
            "https://auth.x.ai/oauth2/device/verify",
            data={"user_code": code},
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        verify_state = _device_redirect_state(
            verify.headers.get("Location"), "https://auth.x.ai/oauth2/device/verify"
        )
        if verify.status_code == 401 or verify_state == "sign-in":
            return False, "xAI verify 要求重新登录"
        if not 200 <= verify.status_code < 400 or verify_state != "consent":
            return False, f"xAI verify 未进入 consent（HTTP {verify.status_code}，state={verify_state or '-'}）"

        approve = http_session.post(
            "https://auth.x.ai/oauth2/device/approve",
            data={
                "user_code": code,
                "action": "allow",
                "principal_type": "User",
                "principal_id": "",
            },
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        approve_state = _device_redirect_state(
            approve.headers.get("Location"), "https://auth.x.ai/oauth2/device/approve"
        )
        if approve.status_code == 401 or approve_state == "sign-in":
            return False, "xAI approve 要求重新登录"
        if not 200 <= approve.status_code < 400 or approve_state != "done":
            return False, f"xAI approve 未完成（HTTP {approve.status_code}，state={approve_state or '-'}）"
        return True, "HTTP verify/approve 已完成"
    except Exception as error:
        return False, f"{error.__class__.__name__}: {error}"


def _open_oauth_url(session, url: str) -> tuple[object, bool]:
    """在新 tab 打开 OAuth URL，但不要卡在页面完整加载等待上。"""
    opener = getattr(session, "open_new_tab", None)
    opened_new_tab = False
    try:
        if callable(opener):
            page = opener()
            opened_new_tab = True
        else:
            browser = getattr(session, "browser", None)
            if browser is not None:
                page = browser.new_tab()
                opened_new_tab = True
            else:
                page = session.refresh_page()
        print("[OAuth Exchange] 已新开 tab 处理 device 授权")
    except Exception as e:
        print(f"[OAuth Exchange] 新开授权 tab 失败，改用当前 tab: {e}")
        page = session.refresh_page()

    try:
        ok = page.get(url, retry=0, timeout=8)
        if ok:
            return page, opened_new_tab
        print("[OAuth Exchange] 授权页未在 8 秒内完成加载，继续用当前页面推进")
        try:
            page.stop_loading()
        except Exception:
            pass
    except Exception as e:
        print(f"[OAuth Exchange] 授权页导航等待失败，继续检查当前页面: {e}")
        try:
            page.stop_loading()
        except Exception:
            pass
    return page, opened_new_tab


def _page_status(session, page=None) -> str:
    try:
        target_page = page or session.refresh_page()
        return str(target_page.url)
    except Exception as e:
        return f"<无法读取页面状态: {e}>"


def exchange_sso_for_oauth_tokens(
    session,
    email: str | None = None,
    password: str | None = None,
    dev_token: str | None = None,
    code_getter: Callable[[], str | None] | None = None,
    recovery_email: str | None = None,
    prefer_google_login: bool = False,
    stop_event=None,
    http_session: Any = None,
    sso_token: str | None = None,
) -> OAuthTokens | None:
    """使用当前浏览器登录态执行 xAI device flow，返回真正的 OAuth tokens。"""
    print("[OAuth Exchange] 开始使用 xAI Device Flow 换取 OAuth tokens...")

    try:
        endpoints = _discover_oauth_endpoints(
            timeout=DISCOVERY_TIMEOUT_SECONDS,
            http_session=http_session,
        )
        device_endpoint = str(
            endpoints.get("device_authorization_endpoint") or DEVICE_AUTHORIZATION_ENDPOINT
        )
        token_endpoint = str(endpoints.get("token_endpoint") or TOKEN_ENDPOINT)

        # 一个 device code 对应一个授权 tab。失败后自动生成新 code 会留下多个页面，
        # 也容易触发 xAI 限流，让浏览器看起来在几个授权步骤之间来回切换。
        max_attempts = 1
        for attempt in range(1, max_attempts + 1):
            if stop_event and stop_event.is_set():
                raise RuntimeError("任务已停止")

            print(f"[OAuth Exchange] 正在请求 Device Code: {device_endpoint}")
            try:
                device_data = _request_device_code(
                    device_endpoint,
                    timeout=DEVICE_CODE_TIMEOUT_SECONDS,
                    http_session=http_session,
                )
            except requests.exceptions.Timeout as error:
                raise RuntimeError(
                    "xAI Device Code 请求超时，授权页不会打开；请检查本轮是否真正使用了可访问 "
                    "auth.x.ai 的代理或出口"
                ) from error
            except requests.exceptions.RequestException as error:
                raise RuntimeError(
                    f"xAI Device Code 请求失败，授权页不会打开：{error}"
                ) from error
            user_code = str(device_data["user_code"]).strip()
            verification_url = _verification_url(
                device_data,
                prefer_email_login=not prefer_google_login,
            )
            interval = _positive_int(
                device_data.get("interval"),
                DEFAULT_INTERVAL_SECONDS,
                minimum=DEFAULT_INTERVAL_SECONDS,
            )
            expires_in = _positive_int(
                device_data.get("expires_in"),
                900,
                minimum=1,
                maximum=MAX_LOGIN_SECONDS,
            )

            print(
                f"[OAuth Exchange] Device Code 已生成 ({attempt}/{max_attempts})，"
                f"User Code: {user_code}"
            )
            direct_authorized = False
            oauth_page = None
            opened_new_tab = False
            if sso_token and http_session is not None:
                direct_authorized, direct_description = _try_direct_device_authorization(
                    http_session=http_session,
                    user_code=user_code,
                    sso_token=sso_token,
                    timeout=DEVICE_CODE_TIMEOUT_SECONDS,
                )
                if direct_authorized:
                    print(f"[OAuth Exchange] 已通过同一 sso 会话直接完成 Build 授权（{direct_description}）")
                else:
                    print(f"[OAuth Exchange] 直连 Build 授权未完成，回退浏览器自动化：{direct_description}")

            if not direct_authorized:
                print("[OAuth Exchange] 正在浏览器中打开授权页面")
                oauth_page, opened_new_tab = _open_oauth_url(session, verification_url)
            try:
                tokens = _drive_device_authorization_and_poll(
                    session=session,
                    oauth_page=oauth_page,
                    device_code=str(device_data["device_code"]).strip(),
                    user_code=user_code,
                    token_endpoint=token_endpoint,
                    interval=interval,
                    expires_in=expires_in,
                    email=email,
                    password=password,
                    dev_token=dev_token,
                    code_getter=code_getter,
                    recovery_email=recovery_email,
                    prefer_google_login=prefer_google_login,
                    stop_event=stop_event,
                    http_session=http_session,
                    drive_browser=not direct_authorized,
                )
            finally:
                if opened_new_tab:
                    try:
                        oauth_page.close()
                    except Exception:
                        pass
            if not tokens:
                raise RuntimeError("OAuth device 授权未完成，未返回 token")

            tokens["token_endpoint"] = token_endpoint
            if not tokens.get("refresh_token"):
                print("[OAuth Exchange] 警告：token 响应没有 refresh_token")
            else:
                print("[OAuth Exchange] 成功获取 OAuth tokens 和 refresh_token!")
            return tokens

        return None
    except Exception as e:
        print(f"[OAuth Exchange] Device Flow 失败: {e}")
        raise


def _poll_device_token_once(
    device_code: str,
    token_endpoint: str,
    timeout: int = 30,
    http_session: Any = None,
) -> tuple[str, OAuthTokens | str | None]:
    try:
        response = _http_client(http_session).post(
            token_endpoint,
            data={
                "grant_type": DEVICE_GRANT_TYPE,
                "device_code": device_code,
                "client_id": OIDC_CLIENT_ID,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
    except Exception as e:
        return "transport_error", str(e)

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text[:300]}

    if response.status_code == 200:
        if not str(data.get("access_token") or "").strip():
            return "fatal", "xAI token response missing access_token"
        return "complete", data

    if response.status_code in {408, 425, 429} or 500 <= response.status_code < 600:
        return "transport_error", f"HTTP {response.status_code}: {data}"

    error_code = str(data.get("error") or "").strip()
    error_description = str(data.get("error_description") or "").strip()
    if error_code == "authorization_pending":
        return "pending", None
    if error_code == "slow_down":
        return "slow_down", None
    if error_code == "access_denied":
        return "fatal", "Grok OAuth 授权已被拒绝"
    if error_code == "expired_token":
        return "expired", "Grok OAuth 验证码已过期"
    if error_code:
        suffix = f" ({error_description})" if error_description else ""
        return "fatal", f"Grok OAuth 失败: {error_code}{suffix}"
    return "fatal", f"Grok OAuth token 返回 {response.status_code}: {data}"


def _drive_device_authorization_and_poll(
    session,
    oauth_page,
    device_code: str,
    user_code: str,
    token_endpoint: str,
    interval: int,
    expires_in: int,
    email: str | None = None,
    password: str | None = None,
    dev_token: str | None = None,
    code_getter: Callable[[], str | None] | None = None,
    recovery_email: str | None = None,
    stop_event=None,
    prefer_google_login: bool = False,
    http_session: Any = None,
    drive_browser: bool = True,
) -> OAuthTokens | None:
    deadline = time.monotonic() + expires_in
    next_poll_at = time.monotonic()
    email_code: str | None = None
    notices: set[str] = set()
    last_status_at = 0.0
    consecutive_transport_errors = 0
    consecutive_page_errors = 0
    consent_click_sent = False
    consent_click_at = 0.0
    consent_click_attempts = 0
    device_code_click_sent = False
    device_code_click_at = 0.0
    device_code_click_attempts = 0

    if drive_browser:
        print("[OAuth Exchange] 已进入 device 授权页驱动；如果停在人机验证页，请手动完成")
    else:
        print("[OAuth Exchange] 已完成 HTTP 授权，直接轮询 token，不再等待 Build 页面")

    while time.monotonic() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")

        now = time.monotonic()
        if now >= next_poll_at:
            poll_result, payload = _poll_device_token_once(
                device_code,
                token_endpoint,
                http_session=http_session,
            )
            if poll_result == "complete":
                return payload if isinstance(payload, dict) else None
            if poll_result == "pending":
                consecutive_transport_errors = 0
                next_poll_at = now + interval
            elif poll_result == "slow_down":
                consecutive_transport_errors = 0
                interval += DEFAULT_INTERVAL_SECONDS
                next_poll_at = now + interval
                print(f"[OAuth Exchange] token endpoint 要求降速，轮询间隔调整为 {interval} 秒")
            elif poll_result == "transport_error":
                consecutive_transport_errors += 1
                print(
                    "[OAuth Exchange] token 轮询传输失败 "
                    f"({consecutive_transport_errors}/{MAX_CONSECUTIVE_POLL_TRANSPORT_ERRORS}): {payload}"
                )
                if consecutive_transport_errors >= MAX_CONSECUTIVE_POLL_TRANSPORT_ERRORS:
                    raise RuntimeError(f"token 轮询连续传输失败，最后错误：{payload}")
                next_poll_at = now + interval
            elif poll_result == "expired":
                raise RuntimeError(str(payload))
            else:
                raise OAuthTerminalError(str(payload))

        if now - last_status_at >= 5:
            remaining = max(0, int(deadline - now))
            page_label = _page_status(session, oauth_page) if drive_browser else "HTTP 直连已授权"
            print(f"[OAuth Exchange] 等待 device 授权，剩余约 {remaining} 秒；页面: {page_label}")
            last_status_at = now

        if not drive_browser:
            wait_seconds = max(0.05, min(0.5, next_poll_at - time.monotonic()))
            time.sleep(wait_seconds)
            continue

        try:
            page = oauth_page or session.refresh_page()
            action = _drive_device_authorization_page(
                page=page,
                email=email,
                password=password,
                recovery_email=recovery_email,
                user_code=user_code,
                email_code=email_code,
                prefer_google_login=prefer_google_login,
            )
        except Exception as error:
            consecutive_page_errors += 1
            if consecutive_page_errors >= MAX_CONSECUTIVE_PAGE_ERRORS:
                raise RuntimeError(
                    "授权页连续驱动失败 "
                    f"({consecutive_page_errors} 次)：{error}"
                ) from error
            try:
                oauth_page = session.refresh_page()
            except Exception:
                pass
            time.sleep(0.5)
            continue

        consecutive_page_errors = 0

        if action == "device-code-ready":
            now = time.monotonic()
            if (
                not device_code_click_sent
                and device_code_click_attempts < 8
                and (device_code_click_attempts == 0 or now - device_code_click_at >= 1)
            ):
                device_code_click_attempts += 1
                device_code_click_at = now
                clicked, description = _click_device_consent_button(page, purpose="device-code")
                if clicked:
                    device_code_click_sent = True
                    print(f"[OAuth Exchange] 已用真实鼠标事件提交 Device Code ({description})")
                elif "device-code-button" not in notices:
                    print(f"[OAuth Exchange] Device Code 提交按钮尚未就绪：{description}")
                    notices.add("device-code-button")
            time.sleep(1)
            continue

        if action == "needs-email-code" and not email_code:
            print("[OAuth Exchange] 授权登录要求邮箱验证码，开始读取验证码")
            if code_getter is not None:
                email_code = code_getter()
            elif email and dev_token:
                email_code = get_oai_code(dev_token, email)
            if not email_code:
                raise RuntimeError("授权登录要求邮箱验证码，但邮箱源没有返回验证码")
            continue

        if action == "needs-human-turnstile":
            clicked = _try_click_turnstile_checkbox(page)
            if clicked:
                if "turnstile" not in notices:
                    print("[OAuth Exchange] 已自动点击 Cloudflare 验证复选框，等待通过...")
                    notices.add("turnstile")
            elif "turnstile" not in notices:
                print("[OAuth Exchange] 自动点击失败，等待你手动完成 Cloudflare 真人验证...")
                notices.add("turnstile")
            time.sleep(2)
            continue

        if action == "device-invalid-action":
            raise RuntimeError(
                "[OAuth Exchange] xAI 拒绝了授权提交 (Invalid action)。"
                "本轮不会重复提交或生成新标签页。"
            )

        if action == "blocked":
            raise CloudflareBlockedError(
                "授权页检测到 Cloudflare 封禁（You have been blocked），已终止当前任务"
            )

        if action == "device-consent-ready":
            now = time.monotonic()
            if (
                not consent_click_sent
                and consent_click_attempts < 8
                and (consent_click_attempts == 0 or now - consent_click_at >= 1)
            ) or (
                consent_click_sent
                and now - consent_click_at >= 8
                and consent_click_attempts < 3
            ):
                consent_click_attempts += 1
                consent_click_at = now
                clicked, description = _click_device_consent_button(page, purpose="consent")
                if clicked:
                    consent_click_sent = True
                    print(f"[OAuth Exchange] 已用真实鼠标事件点击 xAI 授权按钮 ({description})")
                    time.sleep(1)
                    continue
                if "device-consent-button" not in notices:
                    print(f"[OAuth Exchange] 暂未找到可点击的 xAI 授权按钮: {description}")
                    notices.add("device-consent-button")
            elif time.monotonic() - consent_click_at >= 20:
                current_url = _page_status(session, oauth_page)
                if "/oauth2/device/consent" in current_url.lower():
                    raise RuntimeError("自动点击 Build 授权按钮后页面 20 秒仍未跳转")
            time.sleep(1)
            continue

        if action == "needs-manual-consent":
            # Some xAI builds do not expose the consent path in the URL. Give
            # the real-CDP clicker one more chance instead of waiting for a
            # human to press the Build button.
            clicked, description = _click_device_consent_button(page, purpose="consent")
            if clicked:
                consent_click_sent = True
                consent_click_at = time.monotonic()
                consent_click_attempts += 1
                print(f"[OAuth Exchange] 已补点击 Build 授权按钮 ({description})")
            elif action not in notices:
                print(f"[OAuth Exchange] 授权按钮尚未就绪：{description}")
                notices.add(action)
            time.sleep(1)
            continue
        if action == "needs-google-password":
            if action not in notices:
                print("[OAuth Exchange] Google 授权页要求密码，但账号池没有可用密码，等待人工处理...")
                notices.add(action)
            time.sleep(2)
            continue

        if action and action not in {"idle", "authorized-page"}:
            print(f"[OAuth Exchange] 授权页动作: {action}")

        time.sleep(0.5)

    raise RuntimeError("device 授权超时")


def _drive_device_authorization_page(
    page,
    email: str | None,
    password: str | None,
    recovery_email: str | None,
    user_code: str,
    email_code: str | None,
    prefer_google_login: bool = False,
) -> str:
    return str(
        page.run_js(
            r"""
const email = arguments[0] || '';
const password = arguments[1] || '';
const recoveryEmail = arguments[2] || '';
const userCode = arguments[3] || '';
const emailCode = arguments[4] || '';
const preferGoogleLogin = Boolean(arguments[5]);

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setValue(input, value) {
    if (!input || !value) return false;
    input.focus();
    input.click();
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (setter) {
        setter.call(input, '');
        setter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return String(input.value || '').trim() === String(value || '').trim();
}

function visibleInputs(selector) {
    return Array.from(document.querySelectorAll(selector)).filter((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    });
}

function normalizedText(node) {
    return [
        node.innerText || '',
        node.textContent || '',
        node.value || '',
        node.getAttribute?.('aria-label') || '',
        node.getAttribute?.('title') || '',
        node.getAttribute?.('data-testid') || '',
        node.getAttribute?.('data-test-id') || '',
        node.getAttribute?.('name') || '',
        node.getAttribute?.('id') || '',
    ].join(' ').replace(/\s+/g, ' ').trim();
}

function compactText(node) {
    return normalizedText(node).replace(/\s+/g, '').toLowerCase();
}

function canClick(key, minMs = 2500) {
    const now = Date.now();
    window.__grokOAuthLastClicks = window.__grokOAuthLastClicks || {};
    if (window.__grokOAuthLastClicks[key] && now - window.__grokOAuthLastClicks[key] < minMs) {
        return false;
    }
    window.__grokOAuthLastClicks[key] = now;
    return true;
}

function isBadButton(text) {
    const compact = String(text || '').replace(/\s+/g, '').toLowerCase();
    return !compact ||
        compact.includes('cancel') ||
        compact.includes('deny') ||
        compact.includes('back') ||
        compact.includes('notnow') ||
        compact.includes('manageaccount') ||
        compact.includes('useanotheraccount') ||
        compact.includes('取消') ||
        compact.includes('拒绝') ||
        compact.includes('返回') ||
        compact.includes('暂不') ||
        compact.includes('账户管理') ||
        compact.includes('账号管理');
}

function clickButton(key, keywords, anchorInput = null, fallbackSingle = false) {
    if (!canClick(key)) return '';
    const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], a[role="button"], [role="button"]'));
    const visible = buttons.filter((btn) => isVisible(btn) && !btn.disabled && btn.getAttribute('aria-disabled') !== 'true');
    const anchorRect = anchorInput?.getBoundingClientRect?.() || null;
    const scored = [];

    for (const btn of visible) {
        const text = normalizedText(btn);
        const compact = text.replace(/\s+/g, '').toLowerCase();
        if (isBadButton(text)) continue;
        const exactIndex = keywords.findIndex((keyword) => compact === keyword);
        const containsIndex = keywords.findIndex((keyword) => compact.includes(keyword));
        if (exactIndex < 0 && containsIndex < 0) continue;

        let score = exactIndex >= 0 ? 1000 - exactIndex : 500 - containsIndex;
        if (anchorRect) {
            const rect = btn.getBoundingClientRect();
            const anchorCenterX = anchorRect.left + anchorRect.width / 2;
            const buttonCenterX = rect.left + rect.width / 2;
            const belowInput = rect.top >= anchorRect.bottom - 8;
            const sameColumn = Math.abs(buttonCenterX - anchorCenterX) < Math.max(anchorRect.width, rect.width) + 160;
            if (belowInput && sameColumn) score += 500;
            if (rect.bottom < anchorRect.top) score -= 250;
        }
        scored.push({ btn, text, score });
    }

    scored.sort((a, b) => b.score - a.score);
    const target = scored[0]?.btn || (fallbackSingle && visible.length === 1 ? visible[0] : null);
    if (!target) return '';
    target.focus?.();
    target.click();
    return normalizedText(target);
}

function turnstileState() {
    const responseInput = document.querySelector('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]');
    const iframe = Array.from(document.querySelectorAll('iframe')).find((node) => {
        const src = String(node.src || '').toLowerCase();
        const title = String(node.title || '').toLowerCase();
        return src.includes('challenges.cloudflare.com') ||
            src.includes('turnstile') ||
            title.includes('cloudflare') ||
            title.includes('challenge') ||
            title.includes('turnstile');
    }) || null;
    if (responseInput && String(responseInput.value || '').trim()) return 'solved';
    if (iframe && isVisible(iframe)) return 'pending';
    if (responseInput) return 'pending';
    return 'absent';
}

const host = location.hostname.toLowerCase();
const path = location.pathname.toLowerCase();
const pageUrl = location.href.toLowerCase();
const bodyText = (document.body?.innerText || document.body?.textContent || '').toLowerCase();
const bodyCompact = bodyText.replace(/\s+/g, '');

if (
    bodyText.includes("couldn't sign you in") ||
    bodyText.includes("this browser or app may not be secure") ||
    bodyText.includes('sorry, you have been blocked') ||
    bodyText.includes('you have been blocked') ||
    bodyText.includes('you are unable to access') ||
    bodyText.includes('unable to access grok.com') ||
    (bodyText.includes('attention required') && bodyText.includes('cloudflare')) ||
    bodyText.includes('无法登录') ||
    bodyText.includes('无法验证') ||
    bodyText.includes('异常活动')
) {
    return 'blocked';
}

if (
    bodyText.includes('successfully authorized') ||
    bodyText.includes('authorization complete') ||
    bodyText.includes('you may close') ||
    bodyCompact.includes('授权成功') ||
    bodyCompact.includes('已授权') ||
    bodyCompact.includes('可以关闭')
) {
    return 'authorized-page';
}

const isXaiHost = (
    host === 'x.ai' ||
    host.endsWith('.x.ai') ||
    host === 'grok.com' ||
    host.endsWith('.grok.com')
);
if (isXaiHost && path.includes('/oauth2/device/approve') && bodyCompact.includes('invalidaction')) {
    return 'device-invalid-action';
}
if (isXaiHost && (
    path.includes('/oauth2/device/consent') ||
    path.includes('/oauth/device/consent')
)) {
    return 'device-consent-ready';
}

if (host.includes('accounts.google.') || host === 'google.com' || host.endsWith('.google.com')) {
    const isAccountChooserPage = (
        path.includes('/signin/accountchooser') ||
        path.includes('/v3/signin/accountchooser') ||
        bodyText.includes('choose an account') ||
        bodyCompact.includes('选择账号') ||
        bodyCompact.includes('选择帐号')
    );
    const isIdentifierPage = path.includes('/signin/identifier') || path.includes('/v3/signin/identifier');
    const isPasswordPage = (
        path.includes('/signin/challenge') ||
        path.includes('/v3/signin/challenge') ||
        path.includes('/challenge/pwd') ||
        path.includes('/challenge/password')
    );
    const isWorkspaceSpeedbumpPage = (
        path.includes('/signin/speedbump') ||
        path.includes('/v3/signin/speedbump') ||
        (
            (
                bodyText.includes('your school') ||
                bodyText.includes('your organization') ||
                bodyCompact.includes('所在学校的内部规定') ||
                bodyCompact.includes('所在组织的内部规定') ||
                bodyCompact.includes('学校或家长') ||
                bodyCompact.includes('组织的政策')
            ) && (
                bodyCompact.includes('我了解') ||
                bodyCompact.includes('iunderstand') ||
                bodyCompact.includes('gotit')
            )
        )
    );

    if (isWorkspaceSpeedbumpPage) {
        const label = clickButton('google-workspace-speedbump', [
            '我了解',
            'iunderstand',
            'understand',
            'gotit',
            'acknowledge',
            'accept',
            'continue',
            'ok',
            '知道了',
            '明白了',
            '接受',
            '继续'
        ]);
        return label ? `google-speedbump-click:${label}` : 'idle';
    }
    const isConsentPage = !isAccountChooserPage && !isIdentifierPage && !isPasswordPage && (
        path.includes('/signin/oauth') ||
        path.includes('/oauth/consent') ||
        bodyText.includes('wants to access your google account') ||
        bodyText.includes('wants access to your google account') ||
        bodyText.includes('review the permissions') ||
        bodyText.includes('make sure you trust') ||
        bodyCompact.includes('想要访问您的google账号') ||
        bodyCompact.includes('查看权限')
    );

    if (isConsentPage) {
        const label = clickButton('google-consent', ['continue', 'allow', 'agree', '继续', '同意', '允许', '授权', '我同意']);
        return label ? `google-consent-click:${label}` : 'idle';
    }

    const recoveryInput = visibleInputs('input[name="knowledgePreregisteredEmailResponse"], input[type="email"], input[type="text"]').find((node) => {
        const name = String(node.name || node.id || node.autocomplete || '').toLowerCase();
        return name.includes('recovery') || name.includes('knowledge') || bodyText.includes('recovery email') || bodyText.includes('辅助邮箱');
    });
    if (recoveryInput && recoveryEmail) {
        const current = String(recoveryInput.value || '').trim();
        if (current.toLowerCase() !== recoveryEmail.toLowerCase()) {
            return setValue(recoveryInput, recoveryEmail) ? 'google-recovery-filled' : 'idle';
        }
        const label = clickButton('google-recovery-next', ['next', '下一步'], recoveryInput);
        return label ? `google-recovery-click:${label}` : 'google-recovery-filled';
    }

    const identifierInput = visibleInputs('input[type="email"], input[name="identifier"], input#identifierId').find((node) => {
        const marker = String(node.name || node.id || node.autocomplete || '').toLowerCase();
        return !marker.includes('recovery') && !marker.includes('knowledge');
    });
    if (identifierInput && email) {
        const current = String(identifierInput.value || '').trim();
        if (current.toLowerCase() !== email.toLowerCase()) {
            return setValue(identifierInput, email) ? 'google-email-filled' : 'idle';
        }
        const label = clickButton('google-email-next', ['next', '下一步'], identifierInput);
        return label ? `google-email-click:${label}` : 'google-email-filled';
    }

    const passwordInput = visibleInputs('input[type="password"], input[name="Passwd"]').at(0);
    if (passwordInput) {
        if (!password) return 'needs-google-password';
        if (String(passwordInput.value || '') !== password) {
            return setValue(passwordInput, password) ? 'google-password-filled' : 'idle';
        }
        const label = clickButton('google-password-next', ['next', '下一步'], passwordInput);
        return label ? `google-password-click:${label}` : 'google-password-filled';
    }

    if (isAccountChooserPage) {
        if (!canClick('google-account-choice', 3000)) return 'idle';
        const choices = Array.from(document.querySelectorAll('[data-identifier], [role="link"], [role="button"], button')).filter(isVisible);
        const accountChoice = choices.find((node) => {
            const text = [
                node.getAttribute?.('data-identifier') || '',
                node.innerText || '',
                node.textContent || '',
            ].join(' ').toLowerCase();
            return email && text.includes(email.toLowerCase());
        });
        const useAnotherAccount = choices.find((node) => {
            const text = compactText(node);
            return text.includes('useanotheraccount') || text.includes('使用其他账号') || text.includes('使用其他帐号');
        });
        const target = accountChoice || useAnotherAccount;
        if (target) {
            target.click();
            return 'google-account-choice';
        }
        return 'idle';
    }

    return 'idle';
}

const userCodeInput = visibleInputs('input[name*="user_code" i], input[id*="user_code" i], input[name*="code" i], input[id*="code" i], input[type="text"]').find((node) => {
    const marker = String(node.name || node.id || node.autocomplete || '').toLowerCase();
    return marker.includes('user') ||
        bodyText.includes('device code') ||
        bodyText.includes('user code') ||
        bodyCompact.includes('设备代码') ||
        bodyCompact.includes('设备码');
});
if (userCodeInput && userCode) {
    const compactCurrent = String(userCodeInput.value || '').replace(/\s|-/g, '').toLowerCase();
    const compactCode = String(userCode || '').replace(/\s|-/g, '').toLowerCase();
    if (compactCurrent !== compactCode) {
        return setValue(userCodeInput, userCode) ? 'device-code-filled' : 'idle';
    }
    // The Python side performs the submit with a real CDP mouse event. This
    // avoids the same React/submitter issue as the final Build consent.
    return 'device-code-ready';
}

const emailInput = visibleInputs('input[type="email"], input[name="email"], input[autocomplete="email"], input[data-testid="email"]').at(0);
if (!emailInput) {
    if (preferGoogleLogin) {
        const googleLabel = clickButton('xai-google-login', ['continuewithgoogle', 'signwithgoogle', 'signinwithgoogle', 'google', '使用google', '继续使用google', '谷歌']);
        if (googleLabel) return `google-login-click:${googleLabel}`;
    }

    const emailLabel = clickButton('xai-email-login-mode', ['使用邮箱登录', '邮箱登录', '使用邮箱注册', '邮箱注册', 'email', 'continuewithemail']);
    if (emailLabel) return `email-login-click:${emailLabel}`;
}
if (emailInput && email) {
    const current = String(emailInput.value || '').trim();
    if (current.toLowerCase() !== email.toLowerCase()) {
        return setValue(emailInput, email) ? 'email-filled' : 'idle';
    }
    const label = clickButton('xai-email-next', ['继续', '下一步', '确认', '登录', 'continue', 'next', 'sign in', 'login'], emailInput);
    return label ? `email-click:${label}` : 'email-filled';
}

const passwordInput = visibleInputs('input[type="password"], input[name="password"], input[data-testid="password"]').at(0);
if (passwordInput && password) {
    if (String(passwordInput.value || '') !== password) {
        return setValue(passwordInput, password) ? 'password-filled' : 'idle';
    }
    if (turnstileState() === 'pending') {
        return 'needs-human-turnstile';
    }
    const label = clickButton('xai-password-next', ['登录', '继续', '下一步', '确认', 'continue', 'next', 'sign in', 'login'], passwordInput);
    return label ? `password-click:${label}` : 'password-filled';
}

const emailCodeInputs = visibleInputs('input[inputmode="numeric"], input[inputmode="text"], input[type="text"], input[autocomplete="one-time-code"], input[name*="code"], input[data-testid*="code"]').filter((node) => {
    const type = String(node.type || '').toLowerCase();
    const name = String(node.name || '').toLowerCase();
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return type !== 'email' &&
        type !== 'password' &&
        name !== 'email' &&
        !name.includes('user_code') &&
        autocomplete !== 'email' &&
        autocomplete !== 'current-password' &&
        autocomplete !== 'new-password';
});
if (emailCodeInputs.length > 0 && !emailCode) {
    return 'needs-email-code';
}
if (emailCodeInputs.length > 0 && emailCode) {
    if (emailCodeInputs.length === 1) {
        setValue(emailCodeInputs[0], emailCode);
    } else {
        const chars = String(emailCode).replace(/-/g, '').split('');
        emailCodeInputs.slice(0, chars.length).forEach((input, index) => setValue(input, chars[index]));
    }
    const label = clickButton('xai-email-code-next', ['确认', '验证', '提交', '继续', '下一步', 'confirm', 'verify', 'submit', 'continue', 'next'], emailCodeInputs[0]);
    return label ? `code-click:${label}` : 'code-filled';
}

const likelyOauthPage = isXaiHost && (
    pageUrl.includes('/oauth') ||
    pageUrl.includes('oauth2-provider') ||
    pageUrl.includes('/device') ||
    bodyText.includes('device') ||
    bodyText.includes('grok cli') ||
    bodyCompact.includes('授权') ||
    bodyCompact.includes('允许访问')
);
const visibleControls = Array.from(document.querySelectorAll(
    'button, input[type="submit"], input[type="button"], a, [role="button"]'
)).filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
const hasApproveForm = visibleControls.some((node) => {
    const action = String(node.form?.action || node.getAttribute('href') || '').toLowerCase();
    return action.includes('/oauth2/device/approve');
});
const hasBuildControl = visibleControls.some((node) => {
    const text = compactText(node);
    return [
        'build', 'continuetobuild', 'connectbuild', 'usebuild', 'getstarted',
        'startbuilding', 'openbuild', 'gotobuild', 'buildconsole',
        '开始使用', '开始构建', '连接build', '使用build'
    ].some((word) => text === word || text.includes(word));
});
if (likelyOauthPage) {
    if (turnstileState() === 'pending') {
        return 'needs-human-turnstile';
    }
    // xAI's consent endpoint needs the actual submitter from a real browser
    // mouse event. Leave the candidate untouched and let Python dispatch the
    // CDP click instead of using HTMLElement.click().
    return 'device-consent-ready';
}
// Some xAI deployments render the Build consent as a client-side route with
// no /oauth or /device path. A real approve form or a strong Build label is
// enough to put it through the same CDP clicker.
if (isXaiHost && (hasApproveForm || (hasBuildControl && !emailInput && !passwordInput))) {
    if (turnstileState() === 'pending') {
        return 'needs-human-turnstile';
    }
    return 'device-consent-ready';
}

return 'idle';
            """,
            email or "",
            password or "",
            recovery_email or "",
            user_code or "",
            email_code or "",
            prefer_google_login,
        )
    )


def _click_device_consent_button(page, purpose: str = "consent") -> tuple[bool, str]:
    """用 Chrome 真实鼠标事件点击 xAI consent 的允许按钮。

    这里不能调用 form.submit()/requestSubmit()，也不能通过 JS 执行 element.click()。
    xAI 的 approve endpoint 依赖被点击 submitter 自身携带的 action；缺失时服务端
    会返回 ``Invalid action``。
    """
    try:
        candidate = page.run_js(
            r"""
const purpose = String(arguments[0] || 'consent');
const marker = 'data-grok-oauth-consent-target';
for (const node of document.querySelectorAll(`[${marker}]`)) {
    node.removeAttribute(marker);
}

function visible(node) {
    if (!node || node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' &&
        style.visibility !== 'hidden' && style.opacity !== '0';
}

function words(node) {
    return [
        node.innerText || '',
        node.textContent || '',
        node.value || '',
        node.name || '',
        node.getAttribute('aria-label') || '',
        node.getAttribute('title') || '',
        node.getAttribute('data-action') || '',
        node.getAttribute('data-testid') || '',
        node.getAttribute('data-test-id') || '',
        node.id || '',
    ].join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
}

const consentWords = [
    'build', 'continue to build', 'connect build', 'use build', 'get started',
    'start building', 'open build', 'go to build', 'build console',
    'allow', 'authorize', 'approve', 'accept', 'continue', 'confirm',
    '允许', '授权', '同意', '批准', '开始使用', '开始构建', '连接build', '使用build'
];
const deviceCodeWords = [
    'continue', 'next', 'submit', 'confirm', 'verify', 'activate',
    '继续', '下一步', '提交', '确认', '验证', '激活'
];
const allowWords = purpose === 'device-code' ? deviceCodeWords : consentWords;
const denyWords = [
    'cancel', 'deny', 'decline', 'reject', 'back', 'not now',
    'sign out', 'switch account', 'use another account',
    '取消', '拒绝', '返回', '暂不', '退出登录', '切换账号', '使用其他账号'
];
const controls = Array.from(document.querySelectorAll(
    'button, input[type="submit"], input[type="button"], a, [role="button"]'
)).filter(visible);
const scored = controls.map((node, index) => {
    const text = words(node);
    const formAction = String(node.form?.action || node.getAttribute('href') || '').toLowerCase();
    let score = 0;
    if (denyWords.some((word) => text.includes(word))) score -= 10000;
    const exact = allowWords.findIndex((word) => text === word);
    const contains = allowWords.findIndex((word) => text.includes(word));
    if (exact >= 0) score += 2000 - exact;
    else if (contains >= 0) score += 1000 - contains;
    if (String(node.type || '').toLowerCase() === 'submit') score += 100;
    if (formAction.includes('/oauth2/device/approve')) score += 500;
    if (purpose === 'device-code' && text.includes('build')) score -= 1500;
    return { node, index, text, formAction, score };
}).filter((item) => item.score > 0).sort((left, right) => right.score - left.score);

// 页面常见结构是“取消 + 允许”两个 submit button。若文案变化，只在能明确排除
// 拒绝按钮且表单目标确实是 device approve 时选择剩下的唯一按钮。
let selected = scored[0] || null;
if (!selected) {
    const approveControls = controls.filter((node) => {
        const text = words(node);
        return String(node.form?.action || node.getAttribute('href') || '').toLowerCase().includes('/oauth2/device/approve') &&
            !denyWords.some((word) => text.includes(word));
    });
    if (approveControls.length === 1) {
        const node = approveControls[0];
        selected = {
            node,
            index: controls.indexOf(node),
            text: words(node),
            formAction: String(node.form?.action || node.getAttribute('href') || '').toLowerCase(),
            score: 1,
        };
    }
}

if (!selected) {
    return { found: false, count: controls.length };
}
selected.node.setAttribute(marker, 'true');
return {
    found: true,
    tag: selected.node.tagName.toLowerCase(),
    type: String(selected.node.type || ''),
    name: String(selected.node.name || ''),
    value: String(selected.node.value || ''),
    text: selected.text.slice(0, 80),
    formAction: selected.formAction,
};
            """,
            purpose,
        )
        if not isinstance(candidate, dict) or not candidate.get("found"):
            count = candidate.get("count", 0) if isinstance(candidate, dict) else 0
            return False, f"可见提交控件数={count}"

        target = page.ele('css:[data-grok-oauth-consent-target="true"]', timeout=2)
        if not target:
            return False, "按钮在标记后已被页面重新渲染"

        # DrissionPage 默认 by_js=False，会通过 CDP Input.dispatchMouseEvent 发送真实
        # mousePressed/mouseReleased，浏览器因此会把该 button 作为 form submitter。
        clicked = target.click(by_js=False, timeout=3)
        if not clicked:
            return False, "Chrome 未能点击已识别的按钮"

        label = str(candidate.get("text") or candidate.get("value") or "允许")
        submitter = str(candidate.get("name") or "-")
        submitter_value = str(candidate.get("value") or "-")
        return True, f"{label}; submitter={submitter}:{submitter_value}"
    except Exception as error:
        return False, f"{error.__class__.__name__}: {error}"


def _try_click_turnstile_checkbox(page) -> bool:
    """尝试自动点击 Cloudflare Turnstile 复选框。"""
    try:
        challenge_solution = page.ele("@name=cf-turnstile-response")
        challenge_wrapper = challenge_solution.parent()
        challenge_iframe = challenge_wrapper.shadow_root.ele("tag:iframe")
    except Exception:
        return False

    try:
        challenge_iframe.run_js(
            """
function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}
let screenX = getRandomInt(800, 1200);
let screenY = getRandomInt(400, 700);
for (const [name, value] of [['screenX', screenX], ['screenY', screenY]]) {
    const current = Object.getOwnPropertyDescriptor(MouseEvent.prototype, name);
    if (!current || current.configurable) {
        Object.defineProperty(MouseEvent.prototype, name, {
            configurable: true,
            get: function () { return value; },
        });
    }
}
            """
        )
    except Exception:
        pass

    try:
        challenge_iframe_body = challenge_iframe.ele("tag:body").shadow_root
        challenge_button = challenge_iframe_body.ele("tag:input")
        challenge_button.click()
        return True
    except Exception:
        return False
