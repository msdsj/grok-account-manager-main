"""使用 xAI Device OAuth Flow 换取 Grok OAuth tokens。

cockpit-tools 的 Grok refresh_token 获取流程走的是 xAI device flow，而不是
PKCE loopback callback。这里复用注册完成后的同一个无痕浏览器登录态，打开
device 授权页并自动推进登录。xAI 最终 consent 必须用浏览器真实鼠标事件点击
提交按钮，才能把按钮自身的 action 一起提交；直接调用 form.requestSubmit() 会得到
Invalid action。授权完成后轮询 token endpoint 获取 OAuth tokens。
"""

from __future__ import annotations

import time
from typing import Callable, TypedDict
from urllib.parse import quote, urlparse

import requests

from ..mail.duckmail import get_oai_code


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


def _discover_oauth_endpoints(timeout: int = 30) -> dict[str, str | None]:
    """读取 OIDC discovery；失败时回退到 cockpit-tools 使用的已知端点。"""
    fallback: dict[str, str | None] = {
        "device_authorization_endpoint": DEVICE_AUTHORIZATION_ENDPOINT,
        "token_endpoint": TOKEN_ENDPOINT,
        "userinfo_endpoint": None,
    }
    try:
        response = requests.get(
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


def _request_device_code(
    device_authorization_endpoint: str,
    timeout: int = 30,
) -> dict:
    response = requests.post(
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


def _open_oauth_url(session, url: str):
    """在新 tab 打开 OAuth URL，但不要卡在页面完整加载等待上。"""
    opener = getattr(session, "open_new_tab", None)
    try:
        if callable(opener):
            page = opener()
        else:
            browser = getattr(session, "browser", None)
            page = browser.new_tab() if browser is not None else session.refresh_page()
        print("[OAuth Exchange] 已新开 tab 处理 device 授权")
    except Exception as e:
        print(f"[OAuth Exchange] 新开授权 tab 失败，改用当前 tab: {e}")
        page = session.refresh_page()

    try:
        ok = page.get(url, retry=0, timeout=8)
        if ok:
            return page
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
    return page


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
) -> OAuthTokens | None:
    """使用当前浏览器登录态执行 xAI device flow，返回真正的 OAuth tokens。"""
    print("[OAuth Exchange] 开始使用 xAI Device Flow 换取 OAuth tokens...")

    try:
        endpoints = _discover_oauth_endpoints()
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

            device_data = _request_device_code(device_endpoint)
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

            print(f"[OAuth Exchange] Device Code 已生成 ({attempt}/{max_attempts})，User Code: {user_code}")
            print(f"[OAuth Exchange] 打开授权页面: {verification_url}")
            oauth_page = _open_oauth_url(session, verification_url)

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
                stop_event=stop_event,
            )
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
) -> tuple[str, OAuthTokens | str | None]:
    try:
        response = requests.post(
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

    error_code = str(data.get("error") or "").strip()
    error_description = str(data.get("error_description") or "").strip()
    if error_code == "authorization_pending":
        return "pending", None
    if error_code == "slow_down":
        return "slow_down", None
    if error_code == "access_denied":
        return "fatal", "Grok OAuth 授权已被拒绝"
    if error_code == "expired_token":
        return "fatal", "Grok OAuth 验证码已过期"
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
) -> OAuthTokens | None:
    deadline = time.monotonic() + expires_in
    next_poll_at = time.monotonic()
    email_code: str | None = None
    notices: set[str] = set()
    last_status_at = 0.0
    consecutive_transport_errors = 0
    consent_click_sent = False
    consent_click_at = 0.0

    print("[OAuth Exchange] 已进入 device 授权页驱动；如果停在人机验证页，请手动完成")

    while time.monotonic() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")

        now = time.monotonic()
        if now >= next_poll_at:
            poll_result, payload = _poll_device_token_once(device_code, token_endpoint)
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
            else:
                raise RuntimeError(str(payload))

        if now - last_status_at >= 5:
            remaining = max(0, int(deadline - now))
            print(f"[OAuth Exchange] 等待 device 授权，剩余约 {remaining} 秒；页面: {_page_status(session, oauth_page)}")
            last_status_at = now

        try:
            page = oauth_page or session.refresh_page()
            action = _drive_device_authorization_page(
                page=page,
                email=email,
                password=password,
                recovery_email=recovery_email,
                user_code=user_code,
                email_code=email_code,
            )
        except Exception:
            time.sleep(0.5)
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
            raise RuntimeError("授权页被阻止或出现异常活动提示")

        if action == "device-consent-ready":
            if not consent_click_sent:
                clicked, description = _click_device_consent_button(page)
                if clicked:
                    consent_click_sent = True
                    consent_click_at = time.monotonic()
                    print(f"[OAuth Exchange] 已用真实鼠标事件点击 xAI 授权按钮 ({description})")
                    time.sleep(2)
                    continue
                if "device-consent-button" not in notices:
                    print(f"[OAuth Exchange] 暂未找到可点击的 xAI 授权按钮: {description}")
                    notices.add("device-consent-button")
            elif time.monotonic() - consent_click_at >= 20:
                current_url = _page_status(session, oauth_page)
                if "/oauth2/device/consent" in current_url.lower():
                    raise RuntimeError("点击授权按钮后页面 20 秒仍未跳转")
            time.sleep(1)
            continue

        if action in {"needs-manual-consent", "needs-google-password"}:
            if action not in notices:
                if action == "needs-google-password":
                    print("[OAuth Exchange] Google 授权页要求密码，但账号池没有可用密码，等待人工处理...")
                else:
                    print("[OAuth Exchange] 授权页结构尚未识别，等待页面组件加载...")
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
) -> str:
    return str(
        page.run_js(
            r"""
const email = arguments[0] || '';
const password = arguments[1] || '';
const recoveryEmail = arguments[2] || '';
const userCode = arguments[3] || '';
const emailCode = arguments[4] || '';

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

const isXaiHost = host === 'x.ai' || host.endsWith('.x.ai');
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

    const identifierInput = visibleInputs('input[type="email"], input[name="identifier"], input#identifierId').at(0);
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

    const recoveryInput = visibleInputs('input[type="email"], input[type="text"], input[name="knowledgePreregisteredEmailResponse"]').find((node) => {
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
    const label = clickButton('device-code-next', ['continue', 'next', 'submit', 'confirm', '继续', '下一步', '提交', '确认'], userCodeInput);
    return label ? `device-code-click:${label}` : 'device-code-filled';
}

const emailInput = visibleInputs('input[type="email"], input[name="email"], input[autocomplete="email"], input[data-testid="email"]').at(0);
if (!emailInput) {
    const googleLabel = clickButton('xai-google-login', ['continuewithgoogle', 'signwithgoogle', 'signinwithgoogle', 'google', '使用google', '继续使用google', '谷歌']);
    if (googleLabel) return `google-login-click:${googleLabel}`;

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
if (likelyOauthPage) {
    if (turnstileState() === 'pending') {
        return 'needs-human-turnstile';
    }
    const label = clickButton('xai-oauth-consent', [
        '继续',
        '允许',
        '授权',
        '同意',
        '批准',
        '确认',
        'continue',
        'allow',
        'authorize',
        'approve',
        'agree',
        'confirm'
    ], null, false);
    return label ? `consent-click:${label}` : 'needs-manual-consent';
}

return 'idle';
            """,
            email or "",
            password or "",
            recovery_email or "",
            user_code or "",
            email_code or "",
        )
    )


def _click_device_consent_button(page) -> tuple[bool, str]:
    """用 Chrome 真实鼠标事件点击 xAI consent 的允许按钮。

    这里不能调用 form.submit()/requestSubmit()，也不能通过 JS 执行 element.click()。
    xAI 的 approve endpoint 依赖被点击 submitter 自身携带的 action；缺失时服务端
    会返回 ``Invalid action``。
    """
    try:
        candidate = page.run_js(
            r"""
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
    ].join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
}

const allowWords = [
    'allow', 'authorize', 'approve', 'accept', 'continue', 'confirm',
    '允许', '授权', '同意', '批准', '继续', '确认'
];
const denyWords = [
    'cancel', 'deny', 'decline', 'reject', 'back', 'not now',
    '取消', '拒绝', '返回', '暂不'
];
const controls = Array.from(document.querySelectorAll(
    'button, input[type="submit"], input[type="button"]'
)).filter(visible);
const scored = controls.map((node, index) => {
    const text = words(node);
    const formAction = String(node.form?.action || '').toLowerCase();
    let score = 0;
    if (denyWords.some((word) => text.includes(word))) score -= 10000;
    const exact = allowWords.findIndex((word) => text === word);
    const contains = allowWords.findIndex((word) => text.includes(word));
    if (exact >= 0) score += 2000 - exact;
    else if (contains >= 0) score += 1000 - contains;
    if (String(node.type || '').toLowerCase() === 'submit') score += 100;
    if (formAction.includes('/oauth2/device/approve')) score += 500;
    return { node, index, text, formAction, score };
}).filter((item) => item.score > 0).sort((left, right) => right.score - left.score);

// 页面常见结构是“取消 + 允许”两个 submit button。若文案变化，只在能明确排除
// 拒绝按钮且表单目标确实是 device approve 时选择剩下的唯一按钮。
let selected = scored[0] || null;
if (!selected) {
    const approveControls = controls.filter((node) => {
        const text = words(node);
        return String(node.form?.action || '').toLowerCase().includes('/oauth2/device/approve') &&
            !denyWords.some((word) => text.includes(word));
    });
    if (approveControls.length === 1) {
        const node = approveControls[0];
        selected = {
            node,
            index: controls.indexOf(node),
            text: words(node),
            formAction: String(node.form?.action || '').toLowerCase(),
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
            """
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
        challenge_iframe.run_js(
            """
function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}
let screenX = getRandomInt(800, 1200);
let screenY = getRandomInt(400, 600);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
            """
        )
        challenge_iframe_body = challenge_iframe.ele("tag:body").shadow_root
        challenge_button = challenge_iframe_body.ele("tag:input")
        challenge_button.click()
        return True
    except Exception:
        return False
