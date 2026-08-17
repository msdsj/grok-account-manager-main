"""Grok  (xAI) 注册流程。

页面交互全部走 page.run_js() 注入 JS：x.ai 是 React 受控表单，必须经过
HTMLInputElement.prototype 原生 setter + _valueTracker 重置才能让按钮可点击。
切勿替换为 DrissionPage 的 .input() 高阶 API，会导致提交按钮永远 disabled。
"""

from __future__ import annotations

import os
import random
import secrets
import time
from collections.abc import Callable

from DrissionPage.errors import PageDisconnectedError

from ..core.browser import DrissionBrowserSession, get_grok_clearance, wait_for_cookie
from ..core.network import build_browser_http_session
from ..mail.sources import DuckMailSource, MailboxSource, VerificationMailbox
from .base import RegistrationResult

try:
    from ..grok.client import build_cockpit_grok_credential, fetch_complete_credential
    from ..grok.oauth_exchange import (
        CloudflareBlockedError,
        OAuthTerminalError,
        exchange_sso_for_oauth_tokens,
    )
    GROK_API_AVAILABLE = True
except ImportError:
    GROK_API_AVAILABLE = False


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
BROWSER_STAGE_LABELS = {
    "starting": "准备开始",
    "create_mailbox": "创建或领取邮箱",
    "open_signup": "打开注册页",
    "click_email_signup": "选择邮箱注册",
    "fill_email": "填写邮箱",
    "wait_email_code": "等待邮箱验证码",
    "fill_profile": "填写账号资料",
    "wait_sso_cookie": "确认登录状态",
    "oauth_queue": "等待授权队列",
    "oauth_exchange": "获取登录凭证",
    "fetch_credential": "保存账号凭证",
    "done": "注册完成",
}


class BrowserStepRetryRequested(RuntimeError):
    """Raised when the user asks the visible browser window to restart its round."""


class GrokProvider:
    name = "grok"
    signup_url = SIGNUP_URL
    chrome_lang = "zh-CN"
    success_cookie_name = "sso"
    fetch_full_credential = True  # 是否获取完整凭证
    enable_oauth_exchange = False  # cockpit-tools 的 Grok OAuth 需要人工授权，默认不阻塞注册
    stop_event = None  # threading.Event，用于响应停止信号
    mail_source: MailboxSource | None = None
    event_callback = None
    result_callback = None
    retry_browser_callback: Callable[[DrissionBrowserSession], None] | None = None
    oauth_semaphore = None
    oauth_cooldown_range = (2.0, 5.0)
    max_registration_attempts = 3
    browser_window_label = ""

    def run_round(self, session: DrissionBrowserSession) -> RegistrationResult:
        raw_attempts = os.environ.get(
            "GROK_ACCOUNT_MANAGER_MAX_REGISTRATION_ATTEMPTS",
            str(self.max_registration_attempts),
        )
        try:
            max_attempts = max(1, min(4, int(raw_attempts)))
        except (TypeError, ValueError):
            max_attempts = self.max_registration_attempts

        for attempt in range(1, max_attempts + 1):
            try:
                try:
                    return self._run_round_once(session)
                finally:
                    self._close_http_session()
            except Exception as error:
                if isinstance(error, CloudflareBlockedError):
                    stopper = getattr(self.stop_event, "set", None)
                    if callable(stopper):
                        stopper()
                    raise
                stopped = bool(self.stop_event and self.stop_event.is_set())
                pool_exhausted = "邮箱池已耗尽" in str(error)
                if stopped or pool_exhausted or self.registration_succeeded or attempt >= max_attempts:
                    raise
                delay = random.uniform(1.0, 3.0)
                self._log(
                    "warning",
                    f"注册页面流程失败，{delay:.1f}s 后更换浏览器和邮箱重试 "
                    f"({attempt}/{max_attempts - 1})：{error}",
                    stage="registration_retry",
                )
                self._interruptible_sleep(delay)
                retry_callback = self.retry_browser_callback
                if callable(retry_callback):
                    retry_callback(session)
                else:
                    session.restart()

        raise RuntimeError("注册重试结束但没有返回结果")

    def _run_round_once(self, session: DrissionBrowserSession) -> RegistrationResult:
        self._browser_session = session
        self._http_session = None
        self.current_email = ""
        self.registration_succeeded = False
        self.oauth_exchange_error = ""
        self._set_stage("starting", "准备开始注册")
        if self.stop_event and self.stop_event.is_set():
            raise RuntimeError("任务已在开始前停止")

        mail_source = self.mail_source or DuckMailSource()
        self._set_stage("create_mailbox", "正在创建/领取邮箱")
        try:
            mailbox = mail_source.create_mailbox(log_callback=self._log)
        except TypeError:
            mailbox = mail_source.create_mailbox()
        self.current_email = str(getattr(mailbox, "email", "") or "")
        registration_mode = getattr(mail_source, "registration_mode", "email")
        self._log("info", f"本轮使用邮箱：{self.current_email}")

        self._set_stage("open_signup", "正在打开 Grok 注册页")
        session.open_url(self.signup_url)
        self._render_browser_step_overlay("open_signup", "浏览器已打开，正在进入注册页")

        if registration_mode == "google":
            self._set_stage("google_register", "正在使用 Google 账号注册")
            email = _register_with_google_account(
                session,
                mailbox,
                self.stop_event,
                action_handler=self._handle_browser_overlay_action,
            )
            self.current_email = email
            profile = {
                "email": email,
                "auth_provider": "google",
            }
        else:
            self._set_stage("click_email_signup", "正在点击邮箱注册入口")
            _click_email_signup_button(
                session,
                stop_event=self.stop_event,
                action_handler=self._handle_browser_overlay_action,
            )

            if self.stop_event and self.stop_event.is_set():
                raise RuntimeError("任务已停止")

            self._set_stage("fill_email", "正在填写邮箱并发送验证码")
            email = _fill_email_and_submit(
                session,
                mailbox.email,
                stop_event=self.stop_event,
                action_handler=self._handle_browser_overlay_action,
            )
            self.current_email = email
            self._log("info", f"Grok 已向 {email} 发送验证码，开始读取邮箱")

            if self.stop_event and self.stop_event.is_set():
                raise RuntimeError("任务已停止")

            self._set_stage("wait_email_code", "正在等待邮箱验证码")
            _fill_code_and_submit(
                session,
                email,
                mailbox,
                self.stop_event,
                action_handler=self._handle_browser_overlay_action,
            )
            self._log("success", "验证码已写入页面，准备填写资料")

            if self.stop_event and self.stop_event.is_set():
                raise RuntimeError("任务已停止")

            self._set_stage("fill_profile", "正在填写账号资料")
            profile = _fill_profile_and_submit(
                session,
                stop_event=self.stop_event,
                action_handler=self._handle_browser_overlay_action,
            )

        if self.stop_event and self.stop_event.is_set():
            raise RuntimeError("任务已停止")

        self._set_stage("wait_sso_cookie", "正在等待登录凭证 cookie")
        sso_value = _wait_for_sso_cookie_with_resubmit(
            session,
            self.success_cookie_name,
            stop_event=self.stop_event,
            action_handler=self._handle_browser_overlay_action,
        )
        grok_clearance = get_grok_clearance(session)

        result: RegistrationResult = {
            "email": email,
            "credential": sso_value,
            "profile": profile,
            "oauth_status": "pending" if self.enable_oauth_exchange else "not_requested",
        }

        # Registration and OAuth are two distinct outcomes. Emit an in-memory
        # checkpoint for progress reporting; the job manager decides whether it
        # may be persisted. When OAuth is required, only an RT-complete result
        # is allowed onto disk.
        self.registration_succeeded = True
        self.checkpoint_result = dict(result)
        self._emit_result_checkpoint(result)

        if (
            GROK_API_AVAILABLE
            and (self.fetch_full_credential or self.enable_oauth_exchange)
        ):
            self._http_session = build_browser_http_session(session)

        oauth_tokens = None
        oauth_error = ""
        if self.enable_oauth_exchange:
            if not (self.fetch_full_credential and GROK_API_AVAILABLE):
                oauth_error = "OAuth 凭证模块不可用"
            else:
                self._set_stage("oauth_queue", "等待 refresh_token 授权名额")
                acquired_oauth_slot = self._acquire_oauth_slot()
                try:
                    self._set_stage("oauth_exchange", "正在换取 OAuth refresh_token")
                    oauth_password = profile.get("password")
                    oauth_recovery_email = None
                    if registration_mode == "google":
                        oauth_password = str(getattr(mailbox, "password", "") or "").strip() or None
                        oauth_recovery_email = str(getattr(mailbox, "recovery_email", "") or "").strip() or None

                    oauth_error = "未知错误"
                    max_oauth_attempts = 2
                    for attempt in range(1, max_oauth_attempts + 1):
                        if self.stop_event and self.stop_event.is_set():
                            raise RuntimeError("任务已停止")
                        try:
                            print(
                                "[Grok] 尝试通过 xAI Device Flow 换取 OAuth tokens... "
                                f"({attempt}/{max_oauth_attempts})"
                            )
                            candidate_tokens = exchange_sso_for_oauth_tokens(
                                session,
                                email=email,
                                password=oauth_password,
                                sso_token=sso_value,
                                code_getter=lambda: mailbox.wait_for_code(
                                    timeout=180,
                                    stop_event=self.stop_event,
                                ),
                                recovery_email=oauth_recovery_email,
                                prefer_google_login=registration_mode == "google",
                                stop_event=self.stop_event,
                                http_session=self._http_session,
                            )
                            if (
                                candidate_tokens
                                and candidate_tokens.get("access_token")
                                and candidate_tokens.get("refresh_token")
                            ):
                                oauth_tokens = candidate_tokens
                                oauth_error = ""
                                print("[Grok] 已获取 OAuth refresh_token")
                                break
                            if candidate_tokens and not candidate_tokens.get("access_token"):
                                oauth_error = "OAuth token 响应没有 access_token"
                            elif candidate_tokens and not candidate_tokens.get("refresh_token"):
                                oauth_error = "OAuth token 响应没有 refresh_token"
                            else:
                                oauth_error = "OAuth exchange 未返回 token"
                            print(f"[Grok] {oauth_error}")
                        except CloudflareBlockedError as error:
                            oauth_error = str(error)
                            stopper = getattr(self.stop_event, "set", None)
                            if callable(stopper):
                                stopper()
                            print(f"[Grok] 检测到 Cloudflare 封禁，已请求停止任务: {error}")
                            raise
                        except OAuthTerminalError as error:
                            oauth_error = str(error)
                            print(f"[Grok] OAuth exchange 已被服务端终止: {error}")
                            break
                        except Exception as error:
                            if self.stop_event and self.stop_event.is_set():
                                raise
                            oauth_error = str(error)
                            print(f"[Grok] OAuth exchange 失败: {error}")
                        if attempt < max_oauth_attempts:
                            retry_delay = random.uniform(5.0, 12.0)
                            print(f"[Grok] {retry_delay:.1f}s 后重试 OAuth exchange...")
                            self._interruptible_sleep(retry_delay)
                finally:
                    if acquired_oauth_slot:
                        self._oauth_cooldown()
                        self.oauth_semaphore.release()

            if oauth_tokens:
                result["oauth_status"] = "ready"
                result["oauth_error"] = ""
            else:
                result["oauth_status"] = "failed"
                result["oauth_error"] = oauth_error or "未获取到 refresh_token"
                self.oauth_exchange_error = result["oauth_error"]
                self._log(
                    "warning",
                    f"账号已注册，但 refresh_token 获取失败，本轮凭证不保存：{result['oauth_error']}",
                    stage="oauth_exchange",
                )

        if self.fetch_full_credential and GROK_API_AVAILABLE:
            if self.stop_event and self.stop_event.is_set():
                raise RuntimeError("任务已停止")
            self._set_stage("fetch_credential", "正在生成完整 JSON 凭证")
            try:
                if oauth_tokens:
                    full_credential = fetch_complete_credential(
                        email=email,
                        sso_token=oauth_tokens["access_token"],
                        profile=profile,
                        oauth_tokens=oauth_tokens,
                        http_session=self._http_session,
                    )
                else:
                    full_credential = fetch_complete_credential(
                        email=email,
                        sso_token=sso_value,
                        profile=profile,
                        http_session=self._http_session,
                    )
            except Exception as error:
                result["credential_enrichment_error"] = str(error)
                full_credential = build_cockpit_grok_credential(
                    email=email,
                    access_token=oauth_tokens["access_token"] if oauth_tokens else sso_value,
                    profile=profile,
                    oauth_tokens=oauth_tokens,
                )
                self._log(
                    "warning",
                    f"完整账号信息补全失败，已保存基础凭证：{error}",
                    stage="fetch_credential",
                )

            # grok2api authenticates with the browser SSO cookie, so retain it
            # even when the OAuth exchange also succeeded.
            full_credential["sso_token"] = sso_value
            if grok_clearance:
                full_credential["grok_cf_cookies"] = grok_clearance["cookies"]
                if grok_clearance.get("userAgent"):
                    full_credential["grok_cf_user_agent"] = grok_clearance["userAgent"]
            full_credential["oauth_exchange_status"] = result["oauth_status"]
            full_credential["oauth_exchange_error"] = result.get("oauth_error") or None
            full_credential["credential_enrichment_error"] = result.get("credential_enrichment_error") or None
            result["full_credential"] = full_credential
            print(f"[Grok] 成功生成 JSON 凭证 (user_id: {full_credential.get('user_id', 'N/A')})")

        done_message = "本轮注册完成"
        if result.get("oauth_status") == "failed":
            done_message += "，refresh_token 缺失，凭证不保留"
        self._set_stage("done", done_message)
        return result

    def _close_http_session(self) -> None:
        http_session = getattr(self, "_http_session", None)
        self._http_session = None
        if http_session is None:
            return
        try:
            http_session.close()
        except Exception:
            pass

    def _emit_result_checkpoint(self, result: RegistrationResult) -> None:
        callback = self.result_callback
        if callback is None:
            return
        try:
            callback(dict(result))
        except Exception as error:
            raise RuntimeError(f"账号已注册，但基础凭证保存失败：{error}") from error

    def _acquire_oauth_slot(self) -> bool:
        semaphore = self.oauth_semaphore
        if semaphore is None:
            return False
        announced = False
        while not semaphore.acquire(timeout=0.5):
            if self.stop_event and self.stop_event.is_set():
                raise RuntimeError("任务已停止")
            if not announced:
                self._log("info", "其他窗口正在获取 refresh_token，本窗口排队等待", stage="oauth_queue")
                announced = True
        return True

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.stop_event and self.stop_event.is_set():
                raise RuntimeError("任务已停止")
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def _oauth_cooldown(self) -> None:
        minimum, maximum = self.oauth_cooldown_range
        delay = random.uniform(float(minimum), float(maximum))
        try:
            self._interruptible_sleep(delay)
        except RuntimeError:
            return

    def _set_stage(self, stage: str, message: str | None = None) -> None:
        self.current_stage = stage
        if message:
            self._log("info", f"阶段 {stage}：{message}", stage=stage)
        self._render_browser_step_overlay(stage, message or stage)

    def _render_browser_step_overlay(self, stage: str, message: str) -> None:
        session = getattr(self, "_browser_session", None)
        if session is None:
            return
        try:
            _render_browser_step_overlay(
                session.refresh_page(),
                stage=stage,
                stage_label=BROWSER_STAGE_LABELS.get(stage, stage),
                message=message,
                window_label=str(getattr(self, "browser_window_label", "") or ""),
            )
        except Exception:
            # The overlay is observational. A navigation race must not interrupt registration.
            return

    def _handle_browser_overlay_action(self, page) -> None:
        action = _consume_browser_step_action(page)
        if action == "retry":
            self._log(
                "warning",
                "用户在浏览器窗口请求再次运行当前注册流程",
                stage=self.current_stage,
            )
            raise BrowserStepRetryRequested("用户在浏览器窗口请求再次运行")
        if action == "continue":
            self._log(
                "info",
                "用户在浏览器窗口确认继续执行",
                stage=self.current_stage,
            )

    def _log(self, level: str, message: str, **extra) -> None:
        callback = self.event_callback
        if callback is None:
            return
        try:
            callback(level, message, **extra)
        except Exception:
            pass


def _dismiss_cookie_banner(page) -> bool:
    """Dismiss a visible consent banner before it can intercept form clicks."""
    try:
        status = str(page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function textOf(node) {
    return [
        node.innerText || '',
        node.textContent || '',
        node.getAttribute('aria-label') || '',
        node.getAttribute('title') || '',
        node.id || '',
        node.className || '',
    ].join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
}

const explicitReject = [
    '#onetrust-reject-all-handler',
    '[data-testid="cookie-reject-all"]',
    '[data-test-id="cookie-reject-all"]',
    '[data-testid="reject-all"]',
    '[data-test-id="reject-all"]',
].map((selector) => document.querySelector(selector)).find(isVisible);

const candidates = Array.from(document.querySelectorAll(
    'button, [role="button"], input[type="button"], input[type="submit"], a'
)).filter(isVisible);
const rejectLabels = [
    'reject all', 'rejectall', 'decline all', 'declineall',
    'essential only', 'necessary only', 'only necessary',
    '拒绝全部', '拒绝所有', '全部拒绝', '仅必要', '只使用必要',
];
const rejectButton = explicitReject || candidates.find((node) => {
    const text = textOf(node);
    return rejectLabels.some((label) => text.includes(label));
});

if (rejectButton) {
    rejectButton.click();
    return 'dismissed';
}

const closeButton = candidates.find((node) => {
    const text = textOf(node);
    const isClose = text === 'close' || text === '关闭' || text.includes('close cookie');
    if (!isClose) return false;
    let container = node;
    for (let depth = 0; container && depth < 5; depth += 1, container = container.parentElement) {
        if (/cookie|consent|privacy|onetrust|trustarc/i.test(textOf(container))) return true;
    }
    return false;
});
if (closeButton) {
    closeButton.click();
    return 'dismissed';
}

return 'absent';
            """
        ) or "")
    except Exception:
        # Consent handling is optional and must not make registration fail.
        return False

    if status == "dismissed":
        print("[*] 已关闭 Cookie 横幅，继续注册流程。")
        return True
    return False


def _render_browser_step_overlay(
    page,
    *,
    stage: str,
    stage_label: str,
    message: str,
    window_label: str = "",
) -> bool:
    """Render a small, non-blocking status control in one registration browser."""
    try:
        return bool(page.run_js(
            r"""
const stage = String(arguments[0] || 'working');
const stageLabel = String(arguments[1] || stage);
const message = String(arguments[2] || '正在处理');
const windowLabel = String(arguments[3] || '注册窗口');
const overlayId = '__grok-registration-step-overlay';

if (!document.documentElement) return false;
let root = document.getElementById(overlayId);
if (!root) {
    root = document.createElement('section');
    root.id = overlayId;
    root.setAttribute('role', 'status');
    root.style.cssText = [
        'position:fixed', 'top:12px', 'right:12px', 'z-index:2147483647',
        'width:min(300px, calc(100vw - 24px))', 'box-sizing:border-box',
        'padding:12px', 'border:1px solid rgba(15,23,42,.18)',
        'border-radius:8px', 'background:#ffffff', 'color:#172033',
        'box-shadow:0 14px 32px rgba(15,23,42,.20)',
        'font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif',
        'letter-spacing:0',
    ].join(';');
    (document.body || document.documentElement).appendChild(root);
}

if (typeof window.__grokRegistrationStepAction !== 'string') {
    window.__grokRegistrationStepAction = '';
}

root.replaceChildren();
const eyebrow = document.createElement('div');
eyebrow.textContent = windowLabel;
eyebrow.style.cssText = 'margin:0 0 3px;color:#64748b;font-size:11px;font-weight:600';
const title = document.createElement('div');
title.textContent = message;
title.style.cssText = 'font-size:14px;font-weight:700;color:#0f172a';
const detail = document.createElement('div');
detail.textContent = `当前步骤：${stageLabel}`;
detail.style.cssText = 'margin-top:3px;color:#526176;font-size:12px';
const feedback = document.createElement('div');
feedback.dataset.role = 'feedback';
feedback.style.cssText = 'min-height:16px;margin-top:7px;color:#526176;font-size:12px';
const actions = document.createElement('div');
actions.style.cssText = 'display:flex;gap:8px;margin-top:7px';

function makeButton(label, action, emphasized) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    button.dataset.action = action;
    button.style.cssText = emphasized
        ? 'border:0;border-radius:6px;background:#0f766e;color:#fff;padding:6px 10px;font:600 12px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;cursor:pointer'
        : 'border:1px solid #cbd5e1;border-radius:6px;background:#fff;color:#334155;padding:6px 10px;font:600 12px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;cursor:pointer';
    return button;
}

actions.append(makeButton('继续执行', 'continue', true));
actions.append(makeButton('再次运行', 'retry', false));
root.append(eyebrow, title, detail, feedback, actions);

root.onclick = (event) => {
    const button = event.target instanceof Element ? event.target.closest('button[data-action]') : null;
    const action = button?.dataset.action;
    if (!action) return;
    window.__grokRegistrationStepAction = action;
    feedback.textContent = action === 'retry' ? '已请求重新运行当前流程。' : '正在继续执行。';
    if (action === 'continue') root.style.display = 'none';
};

return true;
            """,
            stage,
            stage_label,
            message,
            window_label,
        ))
    except Exception:
        return False


def _consume_browser_step_action(page) -> str:
    """Read and clear a browser-overlay action without relying on cross-origin HTTP."""
    try:
        action = str(page.run_js(
            """
const action = String(window.__grokRegistrationStepAction || '');
window.__grokRegistrationStepAction = '';
return action;
            """
        ) or "")
    except Exception:
        return ""
    return action if action in {"continue", "retry"} else ""


def _has_profile_form(page) -> bool:
    """最终注册页只要出现姓名和密码输入框，就认为已经成功进入资料填写阶段。"""
    try:
        return bool(page.run_js(
            """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
        ))
    except Exception:
        return False


def _wait_for_profile_after_code(
    session: DrissionBrowserSession,
    *,
    timeout: float = 10.0,
    stop_event=None,
) -> bool:
    """确认验证码已被服务端接受，避免把仍停在 OTP 页面误报为成功。"""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        page = session.refresh_page()
        if _has_profile_form(page):
            return True
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    return False


def _resubmit_final_profile(page, action_handler=None) -> bool:
    """资料页点击后仍未跳转时补交一次；人机验证未就绪时不点击。"""
    if action_handler is not None:
        action_handler(page)
    _dismiss_cookie_banner(page)
    try:
        return bool(page.run_js(
            r"""
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (challengeInput && !String(challengeInput.value || '').trim()) {
    return false;
}

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    return text === '完成注册' || text.includes('完成注册');
});
if (!submitButton) return false;
submitButton.focus();
submitButton.click();
return true;
            """
        ))
    except Exception:
        return False


def _wait_for_sso_cookie_with_resubmit(
    session: DrissionBrowserSession,
    cookie_name: str,
    *,
    stop_event=None,
    action_handler=None,
) -> str:
    """先给首次提交 30 秒，仍停在资料页时有条件地补交一次。"""
    refresh_page = getattr(session, "refresh_page", None)
    if action_handler is not None and callable(refresh_page):
        action_handler(refresh_page())
    try:
        return wait_for_cookie(
            session,
            cookie_name,
            timeout=30,
            stop_event=stop_event,
        )
    except Exception as first_error:
        if stop_event and stop_event.is_set():
            raise

        try:
            page = session.refresh_page()
        except Exception:
            page = None
        if page is not None:
            if _dismiss_cookie_banner(page):
                time.sleep(0.3)
                page = session.refresh_page()
            if _has_profile_form(page):
                if action_handler is None:
                    resubmitted = _resubmit_final_profile(page)
                else:
                    resubmitted = _resubmit_final_profile(page, action_handler=action_handler)
                if resubmitted:
                    print("[*] 首次提交后仍停在资料页，已补点一次完成注册。")

        try:
            return wait_for_cookie(
                session,
                cookie_name,
                timeout=150,
                stop_event=stop_event,
            )
        except Exception as final_error:
            raise final_error from first_error


def _is_transient_page_error(error: Exception) -> bool:
    text = str(error)
    name = error.__class__.__name__
    return (
        isinstance(error, PageDisconnectedError)
        or "页面已被刷新" in text
        or "页面已刷新" in text
        or "page has been refreshed" in text.lower()
        or "js结果解析错误" in text
        or "javascript result" in text.lower() and "pars" in text.lower()
        or "context" in text.lower() and "destroy" in text.lower()
        or name in {"ContextLostError", "ElementLostError"}
    )


def _click_email_signup_button(
    session: DrissionBrowserSession,
    timeout=10,
    stop_event=None,
    action_handler=None,
):
    """页面打开后，自动点击邮箱注册入口；如果已经进入邮箱页则直接继续。"""
    deadline = time.time() + timeout
    last_state = ""
    cloudflare_seen = False
    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)
        if _dismiss_cookie_banner(page):
            time.sleep(0.3)
            continue
        try:
            state = page.run_js(r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const emailInput = Array.from(document.querySelectorAll(
    'input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly);
if (emailInput) {
    return { status: 'ready', url: location.href };
}

function textOf(node) {
    return [
        node.innerText || '',
        node.textContent || '',
        node.getAttribute('aria-label') || '',
        node.getAttribute('title') || '',
        node.getAttribute('data-testid') || '',
        node.getAttribute('data-test-id') || '',
        node.id || '',
        node.className || '',
    ].join(' ').replace(/\s+/g, '').toLowerCase();
}

const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const buttonTexts = candidates.slice(0, 8).map((node) => (
    node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('title') || ''
).trim()).filter(Boolean);
const title = document.title || '';
const bodyText = (document.body?.innerText || '').replace(/\s+/g, ' ').trim();
const hasChallengeFrame = Boolean(document.querySelector(
    'iframe[src*="challenges.cloudflare.com"], input[name="cf-turnstile-response"], .cf-turnstile'
));
const pageText = [
    title,
    bodyText,
    buttonTexts.join(' '),
].join(' ').replace(/\s+/g, '').toLowerCase();
const blockedPage = (
    pageText.includes('sorryyouhavebeenblocked') ||
    pageText.includes('youhavebeenblocked') ||
    pageText.includes('youareunabletoaccess') ||
    pageText.includes('unabletoaccessgrok.com') ||
    (pageText.includes('attentionrequired') && pageText.includes('cloudflare'))
);
if (blockedPage) {
    return {
        status: 'blocked',
        url: location.href,
        title,
        bodySnippet: bodyText.slice(0, 260),
    };
}
const cloudflareChallenge = (
    pageText.includes('cloudflare') ||
    pageText.includes('clicktoreveal') ||
    pageText.includes('checkingifyouarehuman') ||
    pageText.includes('justamoment') ||
    pageText.includes('验证您是真人') ||
    pageText.includes('正在检查') ||
    hasChallengeFrame
);
if (cloudflareChallenge) {
    return {
        status: 'cloudflare',
        url: location.href,
        title,
        readyState: document.readyState || '',
        buttons: buttonTexts,
        bodySnippet: bodyText.slice(0, 260),
        hasChallengeFrame,
    };
}
const target = candidates.find((node) => {
    const text = textOf(node);
    if (text.includes('google') || text.includes('谷歌')) {
        return false;
    }
    const hasEmail = text.includes('email') || text.includes('邮箱') || text.includes('邮件') || text.includes('電子郵件');
    const hasSignup = text.includes('注册') || text.includes('註冊') || text.includes('signup') || text.includes('signin') || text.includes('login') || text.includes('continue') || text.includes('使用') || text.includes('继续') || text.includes('繼續');
    return hasEmail && hasSignup;
});

if (!target) {
    return {
        status: 'missing',
        url: location.href,
        title,
        readyState: document.readyState || '',
        buttons: buttonTexts,
        bodySnippet: bodyText.slice(0, 260),
        hasChallengeFrame,
    };
}
target.click();
return { status: 'clicked', url: location.href };
        """)
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(0.8)
                continue
            raise

        if isinstance(state, dict):
            status = str(state.get("status") or "")
            last_state = (
                f"title={state.get('title') or ''}; "
                f"url={state.get('url') or ''}; "
                f"readyState={state.get('readyState') or ''}; "
                f"hasChallengeFrame={bool(state.get('hasChallengeFrame'))}; "
                f"buttons={state.get('buttons') or []}; "
                f"body={state.get('bodySnippet') or ''}"
            )
        else:
            status = str(state or "")
            last_state = status

        if status == "ready":
            return True

        if status == "blocked":
            raise CloudflareBlockedError(
                f"注册页检测到 Cloudflare 封禁：{last_state or 'You have been blocked'}"
            )

        if status == "cloudflare":
            cloudflare_seen = True
            last_state = f"Cloudflare 验证页: {last_state}"
            time.sleep(1)
            continue

        if status == "clicked":
            time.sleep(1)
            session.refresh_page()
            return True

        time.sleep(0.5)

    if cloudflare_seen:
        raise Exception(
            f"检测到 Cloudflare 验证页（{last_state or '页面未返回可诊断信息'}）；"
            "当前页面被 x.ai 安全策略拦截，请在可见浏览器中手动处理后再继续。"
        )
    raise Exception(f'未找到邮箱注册入口（{last_state or "页面未返回可诊断信息"}）')


def _click_google_signup_button(
    session: DrissionBrowserSession,
    timeout=15,
    stop_event=None,
    action_handler=None,
):
    """页面打开后，自动点击 Google 账号注册按钮。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)
        if _dismiss_cookie_banner(page):
            time.sleep(0.3)
            continue
        try:
            clicked = page.run_js(r"""
const bodyText = (document.body?.innerText || '').replace(/\s+/g, '').toLowerCase();
if (
    bodyText.includes('sorryyouhavebeenblocked') ||
    bodyText.includes('youhavebeenblocked') ||
    bodyText.includes('youareunabletoaccess') ||
    bodyText.includes('unabletoaccessgrok.com') ||
    (bodyText.includes('attentionrequired') && bodyText.includes('cloudflare'))
) {
    return 'blocked';
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = candidates.find((node) => {
    const text = [
        node.innerText || '',
        node.textContent || '',
        node.getAttribute('aria-label') || '',
        node.getAttribute('title') || '',
    ].join(' ').replace(/\s+/g, '').toLowerCase();
    return text.includes('google') || text.includes('谷歌');
});

if (!target) {
    return false;
}

target.click();
return true;
        """)
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(0.8)
                continue
            raise

        if clicked == "blocked":
            raise CloudflareBlockedError("Google 注册页检测到 Cloudflare 封禁：You have been blocked")

        if clicked:
            time.sleep(1)
            session.refresh_page()
            return True

        time.sleep(0.5)

    raise Exception('未找到"使用 Google 账号注册"按钮')


def _register_with_google_account(
    session: DrissionBrowserSession,
    mailbox: VerificationMailbox,
    stop_event=None,
    timeout: int = 180,
    action_handler=None,
) -> str:
    email = str(getattr(mailbox, "email", "") or "").strip()
    password = str(getattr(mailbox, "password", "") or "").strip()
    recovery_email = str(getattr(mailbox, "recovery_email", "") or "").strip()
    if not email or not password:
        raise RuntimeError("Google 账号注册需要邮箱和密码")

    _click_google_signup_button(
        session,
        stop_event=stop_event,
        action_handler=action_handler,
    )
    page = session.refresh_page()
    deadline = time.time() + timeout
    recovery_done = False
    email_next_clicked = False
    password_next_clicked = False
    account_choice_clicked = False
    consent_clicked = False
    speedbump_clicked = False
    last_google_path = ""

    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")

        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)
        if _has_xai_session_ready(page):
            print(f"[*] Google 账号授权完成: {email}")
            return email

        try:
            # Only the URL is needed here. Returning a JS object while Google is
            # navigating can leave DrissionPage with a remote objectId that it
            # cannot deserialize ("js结果解析错误").
            href = str(getattr(page, "url", "") or "")
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(1)
                continue
            raise

        if "accounts.google." in href or "google.com" in href:
            google_path = href.split("?", 1)[0].lower()
            if google_path != last_google_path:
                if "accountchooser" in google_path:
                    account_choice_clicked = False
                if "identifier" in google_path:
                    email_next_clicked = False
                if "challenge" in google_path or "password" in google_path:
                    password_next_clicked = False
                if "oauth" in google_path or "consent" in google_path:
                    consent_clicked = False
                if "speedbump" in google_path:
                    speedbump_clicked = False
                last_google_path = google_path

            try:
                action = _handle_google_login_step(
                    page,
                    email=email,
                    password=password,
                    recovery_email=recovery_email,
                    recovery_done=recovery_done,
                    email_next_clicked=email_next_clicked,
                    password_next_clicked=password_next_clicked,
                    account_choice_clicked=account_choice_clicked,
                    consent_clicked=consent_clicked,
                    speedbump_clicked=speedbump_clicked,
                )
            except Exception as error:
                if _is_transient_page_error(error):
                    time.sleep(1)
                    continue
                raise
            if action == "email-filled":
                print(f"[*] 已输入 Google 邮箱: {email}")
            elif action == "email":
                email_next_clicked = True
                print(f"[*] 已填写 Google 邮箱: {email}")
            elif action == "password-filled":
                print("[*] 已填写 Google 密码，等待页面确认")
            elif action == "password":
                password_next_clicked = True
                print("[*] 已确认 Google 密码并点击下一步")
            elif action == "recovery":
                recovery_done = True
                print("[*] 已填写 Google 辅助邮箱")
            elif action == "account-choice":
                account_choice_clicked = True
                print("[*] 已选择 Google 账号")
            elif action == "consent":
                consent_clicked = True
                print("[*] 已点击 Google 授权继续")
            elif action == "speedbump":
                speedbump_clicked = True
                print("[*] 已确认 Google Workspace 教育/企业账号提示")
            elif action == "missing-password":
                raise RuntimeError("Google 账号池里的密码为空")
            elif action == "blocked":
                raise CloudflareBlockedError("Google 登录页检测到 Cloudflare 封禁：You have been blocked")
            elif action == "manual":
                print("[*] Google 登录需要人工处理，等待浏览器完成授权")

        time.sleep(1)

    raise Exception("Google 账号注册超时，未等到 x.ai 登录态")


def _has_xai_session_ready(page) -> bool:
    try:
        return bool(page.run_js(
            r"""
const host = location.hostname;
const path = location.pathname.toLowerCase();
const text = (document.body?.innerText || '').replace(/\s+/g, '').toLowerCase();
const onXai = host.endsWith('x.ai') || host.endsWith('grok.com');
const onAuthChoice = path.includes('sign-up') || path.includes('sign-in') || path.includes('login') || path.includes('signup');
const hasChoiceButtons = (
    text.includes('使用邮箱注册') ||
    text.includes('邮箱注册') ||
    text.includes('continuewithemail') ||
    text.includes('signupwithemail') ||
    text.includes('email') ||
    text.includes('google')
);
return onXai && !(onAuthChoice && hasChoiceButtons);
            """
        ))
    except Exception:
        return False


def _handle_google_login_step(
    page,
    email: str,
    password: str,
    recovery_email: str,
    recovery_done: bool,
    email_next_clicked: bool,
    password_next_clicked: bool,
    account_choice_clicked: bool,
    consent_clicked: bool,
    speedbump_clicked: bool,
) -> str:
    return str(page.run_js(
        r"""
const email = arguments[0];
const password = arguments[1];
const recoveryEmail = arguments[2];
const recoveryDone = Boolean(arguments[3]);
const emailNextClicked = Boolean(arguments[4]);
const passwordNextClicked = Boolean(arguments[5]);
const accountChoiceClicked = Boolean(arguments[6]);
const consentClicked = Boolean(arguments[7]);
const speedbumpClicked = Boolean(arguments[8]);
const bodyText = (document.body?.innerText || '').toLowerCase();
const bodyCompact = bodyText.replace(/\s+/g, '');
const pagePath = location.pathname.toLowerCase();

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }
    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return String(input.value || '') === String(value || '');
}

function getButtonText(node) {
    return [
        node.innerText || '',
        node.textContent || '',
        node.value || '',
        node.getAttribute('aria-label') || '',
    ].join(' ').replace(/\s+/g, '').toLowerCase();
}

function clickButtonByKeywords(keywords, fallback = false) {
    const candidates = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]')).filter((node) => {
        return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
    });
    const target = candidates.find((node) => {
        const text = getButtonText(node);
        return keywords.some((keyword) => text === keyword || text.includes(keyword));
    }) || (fallback && candidates.length === 1 ? candidates[0] : null);
    if (!target) {
        return false;
    }
    target.focus();
    target.click();
    return true;
}

function clickNext() {
    return clickButtonByKeywords([
        'next',
        '下一步',
    ], false);
}

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

const isAccountChooserPage = (
    pagePath.includes('/signin/accountchooser') ||
    pagePath.includes('/v3/signin/accountchooser') ||
    bodyText.includes('choose an account') ||
    bodyCompact.includes('选择账号') ||
    bodyCompact.includes('选择帐号') ||
    bodyCompact.includes('选择一个账号') ||
    bodyCompact.includes('选择一个帐号')
);
const isIdentifierPage = (
    pagePath.includes('/signin/identifier') ||
    pagePath.includes('/v3/signin/identifier')
);
const isPasswordPage = (
    pagePath.includes('/signin/challenge') ||
    pagePath.includes('/v3/signin/challenge') ||
    pagePath.includes('/challenge/pwd') ||
    pagePath.includes('/challenge/password')
);
const isWorkspaceSpeedbumpPage = (
    pagePath.includes('/signin/speedbump') ||
    pagePath.includes('/v3/signin/speedbump') ||
    (
        (
            bodyText.includes("your school") ||
            bodyText.includes("your organization") ||
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
    if (speedbumpClicked) {
        return 'idle';
    }
    if (clickButtonByKeywords([
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
        '继续',
    ], false)) {
        return 'speedbump';
    }
    return 'idle';
}
const isConsentPage = !isAccountChooserPage && (
    !isIdentifierPage &&
    !isPasswordPage &&
    (
        pagePath.includes('/signin/oauth') ||
        pagePath.includes('/oauth/consent') ||
        bodyText.includes('wants to access your google account') ||
        bodyText.includes('wants access to your google account') ||
        bodyText.includes('review the permissions') ||
        bodyText.includes('review permissions') ||
        bodyText.includes('make sure you trust') ||
        bodyCompact.includes('想要访问您的google账号') ||
        bodyCompact.includes('想访问您的google账号') ||
        bodyCompact.includes('请求访问您的google账号') ||
        bodyCompact.includes('查看权限') ||
        bodyCompact.includes('请确认您信任')
    )
);
if (isConsentPage) {
    if (consentClicked) {
        return 'idle';
    }
    if (clickButtonByKeywords([
        'continue',
        'allow',
        'agree',
        '继续',
        '同意',
        '允许',
        '授权',
        '我同意',
    ], false)) {
        return 'consent';
    }
    return 'idle';
}

const recoveryInput = Array.from(document.querySelectorAll(
    'input[name="knowledgePreregisteredEmailResponse"], input[type="email"], input[type="text"]'
)).find((node) => {
    const name = String(node.name || node.id || node.autocomplete || '').toLowerCase();
    return isVisible(node) && !node.disabled && !node.readOnly && (
        name.includes('recovery') || name.includes('knowledge') ||
        bodyText.includes('recovery email') || bodyText.includes('辅助邮箱')
    );
});
if (recoveryInput && recoveryEmail && !recoveryDone) {
    if (!setNativeValue(recoveryInput, recoveryEmail)) {
        return 'idle';
    }
    if (!clickNext()) {
        return 'idle';
    }
    return 'recovery';
}

const identifierInput = Array.from(document.querySelectorAll(
    'input[type="email"], input[name="identifier"], input#identifierId'
)).find((node) => {
    const marker = String(node.name || node.id || node.autocomplete || '').toLowerCase();
    return isVisible(node) && !node.disabled && !node.readOnly &&
        !marker.includes('recovery') && !marker.includes('knowledge');
});
if (identifierInput) {
    const currentIdentifier = String(identifierInput.value || '').trim();
    if (currentIdentifier.toLowerCase() !== email.toLowerCase()) {
        if (!setNativeValue(identifierInput, email)) {
            return 'idle';
        }
        return 'email-filled';
    }
    if (emailNextClicked) {
        return 'idle';
    }
    if (clickNext()) {
        return 'email';
    }
    return 'email-filled';
}

const passwordInput = Array.from(document.querySelectorAll(
    'input[type="password"], input[name="Passwd"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly);
if (passwordInput) {
    if (!password) {
        return 'missing-password';
    }
    const currentPassword = String(passwordInput.value || '');
    if (currentPassword !== password) {
        if (!setNativeValue(passwordInput, password)) {
            return 'idle';
        }
        return 'password-filled';
    }
    if (passwordNextClicked) {
        return 'idle';
    }
    passwordInput.focus();
    if (!clickNext()) {
        return 'idle';
    }
    return 'password';
}

if (isAccountChooserPage) {
    if (accountChoiceClicked) {
        return 'idle';
    }
    const accountChoices = Array.from(document.querySelectorAll('[data-identifier], [role="link"], [role="button"], button')).filter(isVisible);
    const accountChoice = accountChoices.find((node) => {
        const text = [
            node.getAttribute('data-identifier') || '',
            node.innerText || '',
            node.textContent || '',
        ].join(' ').toLowerCase();
        return text.includes(email.toLowerCase());
    });
    const useAnotherAccount = accountChoices.find((node) => {
        const text = getButtonText(node);
        return text.includes('useanotheraccount') || text.includes('使用其他账号') || text.includes('使用其他帐号');
    });
    const target = accountChoice || useAnotherAccount;
    if (target) {
        target.click();
        return 'account-choice';
    }
    return 'manual';
}

if (bodyText.includes('verify') || bodyText.includes('验证') || bodyText.includes('2-step') || bodyText.includes('两步')) {
    return 'manual';
}

return 'idle';
        """,
        email,
        password,
        recovery_email,
        recovery_done,
        email_next_clicked,
        password_next_clicked,
        account_choice_clicked,
        consent_clicked,
        speedbump_clicked,
    ))


def _fill_email_and_submit(
    session: DrissionBrowserSession,
    email: str,
    timeout=15,
    stop_event=None,
    action_handler=None,
):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)
        if _dismiss_cookie_banner(page):
            time.sleep(0.3)
            continue
        try:
            filled = page.run_js(
                """
const email = arguments[0];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input) {
    return 'not-ready';
}

input.focus();
input.click();

// 不能只写 `input.value = xxx`，否则 React / 受控表单可能没有同步内部状态。
const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) {
    tracker.setValue('');
}
if (valueSetter) {
    valueSetter.call(input, email);
} else {
    input.value = email;
}

input.dispatchEvent(new InputEvent('beforeinput', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new InputEvent('input', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new Event('change', { bubbles: true }));

if ((input.value || '').trim() !== email || !input.checkValidity()) {
    return false;
}

input.blur();
return 'filled';
                """,
                email,
            )
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(0.8)
                continue
            raise

        if filled == 'not-ready':
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(0.8)
            page = session.refresh_page()
            try:
                clicked = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input || !input.checkValidity() || !(input.value || '').trim()) {
    return false;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    return text === '注册' || text.includes('注册');
});

if (!submitButton || submitButton.disabled) {
    return false;
}

submitButton.click();
return true;
                    """
                )
            except Exception as error:
                if _is_transient_page_error(error):
                    time.sleep(0.8)
                    continue
                raise

            if clicked:
                time.sleep(1)
                session.refresh_page()
                print(f"[*] 已填写邮箱并点击注册: {email}")
                return email

        time.sleep(0.5)

    raise Exception("未找到邮箱输入框或注册按钮")


def _fill_code_and_submit(
    session: DrissionBrowserSession,
    email: str,
    mailbox: VerificationMailbox,
    stop_event=None,
    timeout: int = 180,
    action_handler=None,
) -> str:
    code = mailbox.wait_for_code(timeout=timeout, stop_event=stop_event)
    if not code:
        raise Exception("获取验证码失败")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")

        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)
        if _dismiss_cookie_banner(page):
            time.sleep(0.3)
            continue
        try:
            filled = page.run_js(
                """
const code = String(arguments[0] || '').trim();

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(input, '');
        nativeInputValueSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
}

function dispatchInputEvents(input, value) {
    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const input = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    const declaredLength = Number(node.maxLength);
    const expectedLength = Number.isFinite(declaredLength) && declaredLength > 0
        ? declaredLength
        : (code.length || 6);
    return isVisible(node) && !node.disabled && !node.readOnly && expectedLength > 1;
}) || null;

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) {
        return false;
    }
    const maxLength = Number(node.maxLength || 0);
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || autocomplete === 'one-time-code';
});

if (!input && otpBoxes.length < code.length) {
    return 'not-ready';
}

if (input) {
    input.focus();
    input.click();
    setNativeValue(input, code);
    dispatchInputEvents(input, code);

    const normalizedValue = String(input.value || '').trim();
    const declaredLength = Number(input.maxLength);
    const expectedLength = Number.isFinite(declaredLength) && declaredLength > 0
        ? declaredLength
        : (code.length || 6);
    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;

    if (normalizedValue !== code) {
        // 聚合输入框写入失败，尝试回退到分格输入方式
        if (otpBoxes.length >= code.length) {
            const orderedBoxes = otpBoxes.slice(0, code.length);
            for (let i = 0; i < orderedBoxes.length; i += 1) {
                const box = orderedBoxes[i];
                const char = code[i] || '';
                box.focus();
                box.click();
                setNativeValue(box, char);
                dispatchInputEvents(box, char);
                box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: char }));
                box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: char }));
                box.blur();
            }
            const merged = orderedBoxes.map((node) => String(node.value || '').trim()).join('');
            return merged === code ? 'filled' : 'box-mismatch';
        }
        return 'aggregate-mismatch';
    }

    if (expectedLength > 0 && normalizedValue.length !== expectedLength) {
        return 'aggregate-length-mismatch';
    }

    if (slots.length && filledSlots && filledSlots !== normalizedValue.length) {
        return 'aggregate-slot-mismatch';
    }

    input.blur();
    return 'filled';
}

const orderedBoxes = otpBoxes.slice(0, code.length);
for (let i = 0; i < orderedBoxes.length; i += 1) {
    const box = orderedBoxes[i];
    const char = code[i] || '';
    box.focus();
    box.click();
    setNativeValue(box, char);
    dispatchInputEvents(box, char);
    box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: char }));
    box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: char }));
    box.blur();
}

const merged = orderedBoxes.map((node) => String(node.value || '').trim()).join('');
return merged === code ? 'filled' : 'box-mismatch';
                """,
                code,
            )
        except Exception as error:
            if not _is_transient_page_error(error):
                raise
            # 点击确认邮箱后如果刚好发生跳转，旧页面句柄会断开；此时切到新页继续判断即可。
            page = session.refresh_page()
            if _has_profile_form(page):
                print("[*] 验证码提交后已跳转到最终注册页。")
                return code
            time.sleep(1)
            continue

        if filled == 'not-ready':
            if _has_profile_form(page):
                print("[*] 已直接进入最终注册页，跳过验证码按钮确认。")
                return code
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 验证码输入框已出现，但写入失败: {filled}")
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(1.2)
            page = session.refresh_page()
            try:
                clicked = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const aggregateInput = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    const declaredLength = Number(node.maxLength);
    const expectedLength = Number.isFinite(declaredLength) && declaredLength > 0
        ? declaredLength
        : 6;
    return isVisible(node) && !node.disabled && !node.readOnly && expectedLength > 1;
}) || null;

let value = '';
if (aggregateInput) {
    value = String(aggregateInput.value || '').trim();
    const declaredLength = Number(aggregateInput.maxLength);
    const expectedLength = Number.isFinite(declaredLength) && declaredLength > 0
        ? declaredLength
        : (value.length || 6);
    if (!value || (expectedLength > 0 && value.length !== expectedLength)) {
        return false;
    }

    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    if (slots.length) {
        const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;
        if (filledSlots && filledSlots !== value.length) {
            return false;
        }
    }
} else {
    const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
        if (!isVisible(node) || node.disabled || node.readOnly) {
            return false;
        }
        const maxLength = Number(node.maxLength || 0);
        const autocomplete = String(node.autocomplete || '').toLowerCase();
        return maxLength === 1 || autocomplete === 'one-time-code';
    });
    value = otpBoxes.map((node) => String(node.value || '').trim()).join('');
    if (!value || value.length < 6) {
        return false;
    }
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const confirmButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    return text === '确认邮箱' || text.includes('确认邮箱') || text === '继续' || text.includes('继续') || text === '下一步' || text.includes('下一步');
});

if (!confirmButton) {
    return 'no-button';
}

confirmButton.focus();
confirmButton.click();
return 'clicked';
                    """
                )
            except Exception as error:
                if not _is_transient_page_error(error):
                    raise
                page = session.refresh_page()
                if _has_profile_form(page):
                    print("[*] 确认邮箱后页面跳转成功，已进入最终注册页。")
                    return code
                clicked = 'disconnected'

            if clicked == 'clicked':
                print("[*] 已填写验证码并点击确认邮箱。")
                if _wait_for_profile_after_code(session, stop_event=stop_event):
                    print("[*] 验证码确认完成，最终注册页已就绪。")
                    return code
                raise RuntimeError("验证码提交后未进入最终注册页，验证码可能已过期或被拒绝")

            if clicked == 'no-button':
                if _wait_for_profile_after_code(session, stop_event=stop_event):
                    print("[*] 验证码已自动提交，最终注册页已就绪。")
                    return code
                raise RuntimeError("验证码填写后未找到确认按钮，且页面未进入最终注册步骤")

            if clicked == 'disconnected':
                time.sleep(1)
                continue

        time.sleep(0.5)

    raise Exception("未找到验证码输入框或确认邮箱按钮")


def _get_turnstile_token(page, stop_event=None):
    """在最终注册页解 Turnstile（真人化点击 + iframe 内坐标 spoof）。"""
    try:
        page.run_js("try { turnstile.reset() } catch(e) { }")
    except Exception:
        pass

    for _ in range(15):
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        try:
            turnstile_response = page.run_js("try { return turnstile.getResponse() } catch(e) { return null }")
            if turnstile_response:
                return turnstile_response

            challenge_solution = page.ele("@name=cf-turnstile-response")
            challenge_wrapper = challenge_solution.parent()
            challenge_iframe = challenge_wrapper.shadow_root.ele("tag:iframe")

            try:
                challenge_iframe.run_js("""
window.dtp = 1
function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}

// 旧方案在 4K 屏下不稳定，这里给出更自然的屏幕坐标。
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
                        """)
            except Exception:
                # 坐标增强是可选的，失败时仍应尝试点击已定位的 checkbox。
                pass

            challenge_iframe_body = challenge_iframe.ele("tag:body").shadow_root
            challenge_button = challenge_iframe_body.ele("tag:input")
            challenge_button.click()
        except Exception:
            pass
        time.sleep(1)
    raise Exception("failed to solve turnstile")


def _build_profile() -> dict[str, str]:
    """生成一组可重复使用的注册资料，密码至少包含大小写、数字和特殊字符。"""
    given_names = (
        "Alex", "Avery", "Blake", "Casey", "Drew", "Evan", "Finn", "Gray",
        "Hayden", "Jamie", "Jordan", "Kai", "Logan", "Morgan", "Noah",
        "Parker", "Quinn", "Reese", "Riley", "Taylor",
    )
    family_names = (
        "Adams", "Baker", "Brooks", "Carter", "Clark", "Cooper", "Davis",
        "Evans", "Foster", "Gray", "Hayes", "Hughes", "King", "Lewis",
        "Miller", "Morgan", "Parker", "Reed", "Stone", "Walker",
    )
    given_name = secrets.choice(given_names)
    family_name = secrets.choice(family_names)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return {"given_name": given_name, "family_name": family_name, "password": password}


def _fill_profile_and_submit(
    session: DrissionBrowserSession,
    timeout: int = 180,
    stop_event=None,
    action_handler=None,
) -> dict[str, str]:
    profile = _build_profile()
    given_name = profile["given_name"]
    family_name = profile["family_name"]
    password = profile["password"]

    deadline = time.time() + timeout
    turnstile_token = ""

    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)
        if _dismiss_cookie_banner(page):
            time.sleep(0.3)
            continue
        try:
            filled = page.run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) {
        return false;
    }
    input.focus();
    input.click();

    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }

    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }

    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));

    return String(input.value || '') === String(value || '');
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return 'not-ready';
}

const givenOk = setInputValue(givenInput, givenName);
const familyOk = setInputValue(familyInput, familyName);
const passwordOk = setInputValue(passwordInput, password);

if (!givenOk || !familyOk || !passwordOk) {
    return 'filled-failed';
}

return [
    String(givenInput.value || '').trim() === String(givenName || '').trim(),
    String(familyInput.value || '').trim() === String(familyName || '').trim(),
    String(passwordInput.value || '') === String(password || ''),
].every(Boolean) ? 'filled' : 'verify-failed';
                """,
                given_name,
                family_name,
                password,
            )
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(0.8)
                continue
            raise

        if filled == 'not-ready':
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 最终注册页输入框已出现，但姓名/密码写入失败: {filled}")
            time.sleep(0.5)
            continue

        page = session.refresh_page()
        try:
            values_ok = page.run_js(
                """
const expectedGiven = arguments[0];
const expectedFamily = arguments[1];
const expectedPassword = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return false;
}

return String(givenInput.value || '').trim() === String(expectedGiven || '').trim()
    && String(familyInput.value || '').trim() === String(expectedFamily || '').trim()
    && String(passwordInput.value || '') === String(expectedPassword || '');
                """,
                given_name,
                family_name,
                password,
            )
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(0.8)
                continue
            raise
        if not values_ok:
            print("[Debug] 最终注册页字段值校验失败，继续重试填写。")
            time.sleep(0.5)
            continue

        page = session.refresh_page()
        try:
            turnstile_state = page.run_js(
                """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return 'not-found';
}
const value = String(challengeInput.value || '').trim();
return value ? 'ready' : 'pending';
                """
            )
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(0.8)
                continue
            raise

        if turnstile_state == "pending" and not turnstile_token:
            print("[*] 检测到最终注册页存在 Turnstile，开始使用现有真人化点击逻辑。")
            turnstile_token = _get_turnstile_token(page, stop_event=stop_event)
            if turnstile_token:
                page = session.refresh_page()
                try:
                    synced = page.run_js(
                        """
const token = arguments[0];
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return false;
}
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) {
    nativeSetter.call(challengeInput, token);
} else {
    challengeInput.value = token;
}
challengeInput.dispatchEvent(new Event('input', { bubbles: true }));
challengeInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(challengeInput.value || '').trim() === String(token || '').trim();
                        """,
                        turnstile_token,
                    )
                except Exception as error:
                    if _is_transient_page_error(error):
                        time.sleep(0.8)
                        continue
                    raise
                if synced:
                    print("[*] Turnstile 响应已同步到最终注册表单。")

        time.sleep(1.2)

        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)
        if _dismiss_cookie_banner(page):
            time.sleep(0.3)
            continue
        try:
            clicked = page.run_js(
                r"""
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (challengeInput && !String(challengeInput.value || '').trim()) {
    return false;
}

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter(isVisible);
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    return text === '完成注册' || text.includes('完成注册');
});
if (!submitButton || submitButton.disabled || submitButton.getAttribute('aria-disabled') === 'true') {
    return false;
}
submitButton.scrollIntoView({ block: 'center', inline: 'center' });
submitButton.focus();
submitButton.click();
return true;
                """
            )
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(0.8)
                continue
            raise

        if clicked:
            print(f"[*] 已填写注册资料并点击完成注册: {given_name} {family_name}")
            return profile

        time.sleep(0.5)

    raise Exception("未找到最终注册表单或完成注册按钮")
