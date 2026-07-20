"""使用 xAI Authorization Code  + PKCE 换取 Grok OAuth tokens。

cockpit-tools 的 Grok 获取 refresh_token 走的是标准 OAuth 授权码流程：
打开 xAI authorization_endpoint，监听 127.0.0.1:56121/callback，拿到 code 后
用 code_verifier 换 access_token / refresh_token / id_token。

这里复用注册后已有登录态的浏览器 session，只自动推进邮箱、密码、邮箱验证码和授权确认页；
Cloudflare 真人验证仍交给用户手动完成，避免错误提交导致 Invalid action。
"""

from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import queue
import secrets
import threading
import time
from typing import Callable, TypedDict
from urllib.parse import urlencode, urlparse, parse_qs

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


class CallbackResult(TypedDict, total=False):
    code: str
    state: str
    error: str


OIDC_ISSUER = "https://auth.x.ai"
OIDC_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_SCOPE = "openid profile email offline_access grok-cli:access api:access"

AUTHORIZATION_ENDPOINT = "https://auth.x.ai/oauth/authorize"
TOKEN_ENDPOINT = "https://auth.x.ai/oauth/token"
DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 56121
CALLBACK_PATH = "/callback"


def _discover_oauth_endpoints(timeout: int = 30) -> dict[str, str]:
    """读取 OIDC discovery；失败时回退到 cockpit-tools 使用的已知端点。"""
    fallback = {
        "authorization_endpoint": AUTHORIZATION_ENDPOINT,
        "token_endpoint": TOKEN_ENDPOINT,
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
            "authorization_endpoint": data.get("authorization_endpoint")
            or AUTHORIZATION_ENDPOINT,
            "token_endpoint": data.get("token_endpoint") or TOKEN_ENDPOINT,
        }
    except Exception as e:
        print(f"[OAuth Exchange] 读取 discovery 失败，使用默认端点: {e}")
        return fallback


def _base64url_no_padding(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = _base64url_no_padding(secrets.token_bytes(96))
    challenge = _base64url_no_padding(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _build_authorize_url(
    authorization_endpoint: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    nonce: str,
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": OIDC_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": OIDC_SCOPE,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "nonce": nonce,
            "plan": "generic",
            "referrer": "cli-proxy-api",
        }
    )
    return f"{authorization_endpoint}?{query}"


class _CallbackServer(ThreadingHTTPServer):
    allow_reuse_address = True


def _start_callback_server(
    preferred_port: int = CALLBACK_PORT,
) -> tuple[_CallbackServer, int, "queue.Queue[CallbackResult]"]:
    result_queue: queue.Queue[CallbackResult] = queue.Queue(maxsize=1)

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return

            params = parse_qs(parsed.query)
            result: CallbackResult = {
                "code": (params.get("code") or [""])[0].strip(),
                "state": (params.get("state") or [""])[0].strip(),
                "error": (params.get("error") or [""])[0].strip(),
            }
            try:
                result_queue.put_nowait(result)
            except queue.Full:
                pass

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if result.get("code") and not result.get("error"):
                self.wfile.write(
                    b"<h1>Login successful</h1><p>You can close this window.</p>"
                )
            else:
                self.wfile.write(
                    b"<h1>Login failed</h1><p>Please check the CLI output.</p>"
                )

        def log_message(self, format: str, *args) -> None:
            return

    last_error: OSError | None = None
    for port in (preferred_port, 0):
        try:
            server = _CallbackServer((CALLBACK_HOST, port), CallbackHandler)
            actual_port = int(server.server_address[1])
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            return server, actual_port, result_queue
        except OSError as e:
            last_error = e

    raise RuntimeError(f"无法启动 xAI OAuth callback server: {last_error}")


def _exchange_code_for_tokens(
    code: str,
    redirect_uri: str,
    code_verifier: str,
    token_endpoint: str,
    timeout: int = 30,
) -> OAuthTokens:
    response = requests.post(
        token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code.strip(),
            "redirect_uri": redirect_uri,
            "client_id": OIDC_CLIENT_ID,
            "code_verifier": code_verifier,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    tokens: OAuthTokens = response.json()
    if not tokens.get("access_token"):
        raise RuntimeError("xAI token response missing access_token")
    tokens["token_endpoint"] = token_endpoint
    tokens["redirect_uri"] = redirect_uri
    if not tokens.get("refresh_token"):
        print("[OAuth Exchange] 警告：token 响应没有 refresh_token")
    return tokens


def _open_oauth_url(session, url: str) -> None:
    """打开 OAuth URL，但不要卡在页面完整加载等待上。"""
    page = session.refresh_page()
    try:
        ok = page.get(url, retry=0, timeout=8)
        if ok:
            return
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


def _page_status(session) -> str:
    try:
        page = session.refresh_page()
        return str(page.url)
    except Exception as e:
        return f"<无法读取页面状态: {e}>"


def exchange_sso_for_oauth_tokens(
    session,
    email: str | None = None,
    password: str | None = None,
    dev_token: str | None = None,
    code_getter: Callable[[], str | None] | None = None,
) -> OAuthTokens | None:
    """使用当前浏览器登录态执行 xAI PKCE OAuth，返回真正的 OAuth tokens。"""
    print("[OAuth Exchange] 开始使用 PKCE loopback 流程换取 OAuth tokens...")

    callback_server: _CallbackServer | None = None
    try:
        endpoints = _discover_oauth_endpoints()
        code_verifier, code_challenge = _generate_pkce_pair()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        callback_server, port, callback_queue = _start_callback_server()
        redirect_uri = f"http://{CALLBACK_HOST}:{port}{CALLBACK_PATH}"

        auth_url = _build_authorize_url(
            authorization_endpoint=endpoints["authorization_endpoint"],
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            state=state,
            nonce=nonce,
        )
        print(f"[OAuth Exchange] 已启动回调监听: {redirect_uri}")
        print(f"[OAuth Exchange] 访问授权页面: {auth_url}")
        _open_oauth_url(session, auth_url)
        print("[OAuth Exchange] 已进入授权页驱动；若停在验证或授权确认页，请按页面提示手动完成")

        result = _drive_pkce_authorization_page(
            session=session,
            callback_queue=callback_queue,
            expected_state=state,
            email=email,
            password=password,
            dev_token=dev_token,
            code_getter=code_getter,
            timeout=300,
        )
        if not result:
            print("[OAuth Exchange] 5 分钟内未收到 OAuth callback")
            return None
        if result.get("error"):
            print(f"[OAuth Exchange] OAuth callback 返回错误: {result['error']}")
            return None
        if result.get("state") != state:
            print("[OAuth Exchange] OAuth callback state 不匹配")
            return None
        code = (result.get("code") or "").strip()
        if not code:
            print("[OAuth Exchange] OAuth callback 缺少 authorization code")
            return None

        tokens = _exchange_code_for_tokens(
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
            token_endpoint=endpoints["token_endpoint"],
        )
        print("[OAuth Exchange] 成功获取 OAuth tokens!")
        return tokens
    except Exception as e:
        print(f"[OAuth Exchange] PKCE OAuth 失败: {e}")
        return None
    finally:
        if callback_server is not None:
            callback_server.shutdown()
            callback_server.server_close()


def _drive_pkce_authorization_page(
    session,
    callback_queue: "queue.Queue[CallbackResult]",
    expected_state: str,
    email: str | None = None,
    password: str | None = None,
    dev_token: str | None = None,
    code_getter: Callable[[], str | None] | None = None,
    timeout: int = 300,
) -> CallbackResult | None:
    """推进登录页并等待 localhost callback。

    最终 OAuth consent 页会尽量自动点“允许”；如果没找到按钮，才会等你手动确认。
    浏览器跳到 127.0.0.1 callback 后，本函数从本地 HTTP server 收到 code 并返回。
    """
    deadline = time.monotonic() + timeout
    email_code: str | None = None
    notices: set[str] = set()
    last_status_at = 0.0

    while time.monotonic() < deadline:
        try:
            result = callback_queue.get_nowait()
            if result.get("state") and result.get("state") != expected_state:
                print("[OAuth Exchange] 忽略 state 不匹配的 callback")
            else:
                return result
        except queue.Empty:
            pass

        now = time.monotonic()
        if now - last_status_at >= 5:
            remaining = max(0, int(deadline - now))
            print(f"[OAuth Exchange] 等待 callback，剩余约 {remaining} 秒；页面: {_page_status(session)}")
            last_status_at = now

        try:
            page = session.refresh_page()
            action = page.run_js(
                r"""
const email = arguments[0] || '';
const password = arguments[1] || '';
const code = arguments[2] || '';

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
    if (setter) setter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return String(input.value || '').trim() === String(value || '').trim();
}

function visibleInputs(selector) {
    return Array.from(document.querySelectorAll(selector)).filter((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    });
}

function normalizedText(node) {
    return (node.innerText || node.textContent || node.value || '').replace(/\s+/g, ' ').trim();
}

function isBadButton(text) {
    const lower = text.toLowerCase();
    return !text ||
        lower.includes('cancel') ||
        lower.includes('deny') ||
        lower.includes('back') ||
        text.includes('取消') ||
        text.includes('拒绝') ||
        text.includes('返回') ||
        text.includes('账户管理') ||
        text.includes('您正在登录');
}

function clickButton(kinds, anchorInput = null) {
    const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], a[role="button"], [role="button"]'));
    const anchorRect = anchorInput?.getBoundingClientRect?.() || null;
    const scored = [];

    for (const btn of buttons) {
        if (!isVisible(btn) || btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
        const text = normalizedText(btn);
        const lower = text.toLowerCase();
        if (isBadButton(text)) continue;
        const exactIndex = kinds.findIndex((kind) => lower === kind);
        const containsIndex = kinds.findIndex((kind) => lower.includes(kind));
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
    const button = scored[0]?.btn || null;
    if (!button) return '';
    button.click();
    return normalizedText(button);
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

const bodyText = (document.body?.innerText || document.body?.textContent || '').toLowerCase();
const pageUrl = String(location.href || '').toLowerCase();
if (pageUrl.startsWith('http://127.0.0.1:') || bodyText.includes('login successful')) {
    return 'callback-page';
}

const emailInput = visibleInputs('input[type="email"], input[name="email"], input[autocomplete="email"], input[data-testid="email"]').at(0);
if (!emailInput) {
    const label = clickButton(['使用邮箱登录', '邮箱登录', '使用邮箱注册', '邮箱注册']);
    if (label) {
        return `email-login-click:${label}`;
    }
}
if (emailInput && email) {
    const emailFilled = String(emailInput.value || '').trim() === email || setValue(emailInput, email);
    if (emailFilled) {
        const label = clickButton(['下一步', '继续', '确认', '登录', 'continue', 'next', 'sign in', 'log in'], emailInput);
        return label ? `email-click:${label}` : 'email-filled';
    }
}

const passwordInput = visibleInputs('input[type="password"], input[name="password"], input[data-testid="password"]').at(0);
if (passwordInput && password) {
    const passwordFilled = String(passwordInput.value || '') === password || setValue(passwordInput, password);
    if (passwordFilled) {
        if (turnstileState() === 'pending') {
            return 'needs-human-turnstile';
        }
        const label = clickButton(['登录', '下一步', '继续', '确认', 'continue', 'next', 'sign in', 'log in'], passwordInput);
        return label ? `password-click:${label}` : 'password-filled';
    }
}

const codeInputs = visibleInputs('input[inputmode="numeric"], input[inputmode="text"], input[type="text"], input[autocomplete="one-time-code"], input[name*="code"], input[data-testid*="code"]').filter((node) => {
    const type = String(node.type || '').toLowerCase();
    const name = String(node.name || '').toLowerCase();
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return type !== 'email' &&
        type !== 'password' &&
        name !== 'email' &&
        autocomplete !== 'email' &&
        autocomplete !== 'current-password' &&
        autocomplete !== 'new-password';
});
if (codeInputs.length > 0 && !code) {
    return 'needs-email-code';
}
if (codeInputs.length > 0 && code) {
    if (codeInputs.length === 1) {
        setValue(codeInputs[0], code);
    } else {
        const chars = String(code).replace(/-/g, '').split('');
        codeInputs.slice(0, chars.length).forEach((input, index) => setValue(input, chars[index]));
    }
    const label = clickButton(['确认', '验证', '提交', '继续', '下一步', 'confirm', 'verify', 'submit', 'continue', 'next'], codeInputs[0]);
    return label ? `code-click:${label}` : 'code-filled';
}

const isConsentPage = (
    pageUrl.includes('/oauth') ||
    bodyText.includes('grok build') ||
    bodyText.includes('cli-proxy-api')
) && (
    bodyText.includes('authorize') ||
    bodyText.includes('allow') ||
    bodyText.includes('approve') ||
    bodyText.includes('授权') ||
    bodyText.includes('允许') ||
    bodyText.includes('批准')
);
if (isConsentPage) {
    const label = clickButton(['允许', '授权', '同意', 'allow', 'approve', 'authorize']);
    return label ? `consent-click:${label}` : 'needs-manual-consent';
}

return 'idle';
                """,
                email or "",
                password or "",
                email_code or "",
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
            continue

        if action == "needs-human-turnstile":
            if "turnstile" not in notices:
                print("[OAuth Exchange] 等待你手动完成 Cloudflare 真人验证...")
                notices.add("turnstile")
            time.sleep(2)
            continue

        if action == "needs-manual-consent":
            if "consent" not in notices:
                print("[OAuth Exchange] 等待你手动点击 xAI OAuth 授权页的“允许”按钮...")
                notices.add("consent")
            time.sleep(2)
            continue

        if action and action not in ("idle", "callback-page"):
            print(f"[OAuth Exchange] 授权页动作: {action}")

        time.sleep(0.5)

    return None
