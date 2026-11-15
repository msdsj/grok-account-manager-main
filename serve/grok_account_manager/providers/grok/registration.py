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

from ...core.browser import DrissionBrowserSession, get_grok_clearance, wait_for_cookie
from ...core.network import build_browser_http_session
from ...mail.sources import DuckMailSource, MailboxSource, VerificationMailbox
from ..base import RegistrationResult

try:
    from .client import build_cockpit_grok_credential, fetch_complete_credential
    from .oauth_exchange import (
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


def _inspect_post_registration_page(
    session: DrissionBrowserSession,
    *,
    timeout: float,
    stop_event=None,
) -> tuple[str, str]:
    """Wait for a readable post-registration page and classify explicit blocks."""
    deadline = time.monotonic() + max(0.5, float(timeout))
    last_error = ""
    while time.monotonic() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        try:
            page = session.refresh_page()
            page_url = str(getattr(page, "url", "") or "").lower()
            page_state = page.run_js(
                "return ((document.title || '') + ' ' + (document.body?.innerText || '')).toLowerCase();"
            )
            if page_state is None or not str(page_state).strip():
                raise RuntimeError("页面状态为空")
            compact = "".join(str(page_state).split())
            if (
                "blocked" in page_url
                or "suspended" in page_url
                or "account-locked" in page_url
                or "sorry,youhavebeenblocked" in compact
                or "youhavebeenblocked" in compact
                or "youareunabletoaccess" in compact
                or "unabletoaccessgrok.com" in compact
                or ("cloudflare" in compact and "blocked" in compact)
                or "requestblocked" in compact
                or "accessdenied" in compact
                or "accountsuspended" in compact
                or "accounthasbeensuspended" in compact
                or "accountlocked" in compact
                or "accounthasbeenlocked" in compact
            ):
                return "blocked", f"页面 {page_url or '未知'}"
            return "clear", f"页面 {page_url or '未知'}"
        except Exception as error:
            last_error = f"{error.__class__.__name__}: {error}"
            time.sleep(0.25)
    return "unknown", last_error or "页面仍在跳转"


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

        # The SSO cookie confirms registration. Before opening an OAuth tab,
        # wait until the current Grok page is readable and has no explicit
        # block message. This is state-driven and does not use a fixed settle
        # delay that can race the redirect.
        # The production BrowserSession always exposes ``refresh_page``. A
        # small protocol-compatible fallback keeps provider integrations that
        # only implement the registration calls working; real browsers still
        # take the state check above before OAuth.
        if callable(getattr(session, "refresh_page", None)):
            block_state, block_detail = _inspect_post_registration_page(
                session,
                timeout=30.0,
                stop_event=self.stop_event,
            )
        else:
            block_state, block_detail = "clear", "兼容模式会话未提供页面检查接口"
        if block_state == "blocked":
            raise CloudflareBlockedError(
                f"注册完成后检测到 Cloudflare 封禁（{block_detail}），跳过 Build/OAuth"
            )
        if block_state == "unknown":
            raise RuntimeError(
                f"注册已取得 sso cookie，但注册后的 Grok 页面无法确认状态，已跳过 refresh_token：{block_detail}"
            )

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
                                print(
                                    f"[Grok] 已获取 OAuth refresh_token（账号 {email}，"
                                    "token 内容已隐藏）"
                                )
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
                    # OAuth has the browser cookies it needs in the bound HTTP
                    # session now. Release Chromium before the optional
                    # cooldown so the next round can start without waiting on
                    # a browser process that is no longer useful.
                    if oauth_tokens:
                        stop_browser = getattr(session, "stop", None)
                        if callable(stop_browser):
                            try:
                                stop_browser()
                            except Exception as error:
                                self._log(
                                    "warning",
                                    f"refresh_token 已获取，但关闭本轮浏览器失败：{error}",
                                    stage="oauth_exchange",
                                )
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
        'position:fixed', 'bottom:12px', 'left:12px', 'z-index:2147483646',
        'pointer-events:none',
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
        ? 'border:0;border-radius:6px;background:#0f766e;color:#fff;padding:6px 10px;font:600 12px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;cursor:pointer;pointer-events:auto'
        : 'border:1px solid #cbd5e1;border-radius:6px;background:#fff;color:#334155;padding:6px 10px;font:600 12px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;cursor:pointer;pointer-events:auto';
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

// Google 界面的语言由账号 locale 决定，可能是世界上任意一种语言，
// 因此不能只靠英文文案点按钮。策略是三级：
//   1) 结构定位：Google 主操作按钮带有与语言无关的稳定 id / jsname；
//   2) 多语言文案：覆盖尽可能多的语种（注意 getButtonText 已去掉空格，关键词也要写成无空格）；
//   3) 位置兜底：排除「取消/返回」类后，取最后一个可见可用按钮（Google 把主操作放最右）。
const PRIMARY_ACTION_KEYWORDS = [
    'continue', 'allow', 'agree', 'accept', 'confirm', 'ok', 'yes',
    '继续', '同意', '允许', '授权', '确定', '确认', '接受',
    '繼續', '允許', '授權', '確定', '確認', '接受',
    'tiếptục', 'đồngý', 'chophép', 'chấpnhận', 'xácnhận',
    '続行', '許可', '同意', '承認', '確認',
    '계속', '허용', '동의', '승인', '확인',
    'continuar', 'permitir', 'aceptar', 'deacuerdo',
    'aceitar', 'concordar',
    'continuer', 'autoriser', 'accepter',
    'weiter', 'fortfahren', 'zulassen', 'akzeptieren', 'erlauben', 'bestätigen',
    'continua', 'consenti', 'accetta',
    'продолжить', 'разрешить', 'принять', 'согласиться',
    'продовжити', 'дозволити',
    'متابعة', 'السماح', 'موافق',
    'המשך', 'אפשר', 'אישור',
    'ادامه', 'اجازه',
    'जारीरखें', 'अनुमतिदें', 'सहमत',
    'চালিয়েযান', 'অনুমতিদিন',
    'தொடரவும்', 'அனுமதிக்கவும்',
    'కొనసాగించు', 'అనుమతించు',
    'ಮುಂದುವರಿಸಿ', 'ಅನುಮತಿಸಿ',
    'തുടരുക', 'അനുവദിക്കുക',
    'ચાલુરાખો', 'સાચવો',
    'सुरूठेवा', 'जारीरखें',
    'ਜਾਰੀਰੱਖੋ',
    'ดำเนินการต่อ', 'อนุญาต', 'ยอมรับ',
    'lanjutkan', 'izinkan', 'setujui',
    'teruskan', 'benarkan', 'setuju',
    'magpatuloy', 'payagan',
    'doorgaan', 'toestaan', 'accepteren',
    'fortsätt', 'tillåt',
    'fortsæt', 'tillad',
    'fortsett', 'tillat',
    'jatka', 'salli',
    'kontynuuj', 'zezwól', 'zaakceptuj',
    'pokračovat', 'povolit',
    'nadaljuj', 'dovoli',
    'nastavi', 'dopusti',
    'folytatás', 'engedélyezés',
    'continuă', 'permite',
    'продължи', 'разреши',
    'tęsti', 'leisti',
    'turpināt', 'atļaut',
    'jätka', 'luba',
    'haldaáfram', 'leyfa',
    'გაგრძელება', 'დაშვება',
    'շարունակել', 'թույլատրել',
    'davamet', 'icazəverin',
    'жалғастыру', 'рұқсатету',
    'davometish', 'ruxsatberish',
    'үргэлжлүүлэх', 'зөвшөөрөх',
    'जारीराख्नुहोस्', 'अनुमतिदिनुहोस्',
    'දිගටමකරගෙනයන්න', 'අවසරදෙන්න',
    'បន្ត', 'អនុញ្ញាត',
    'ສືບຕໍ່', 'ອະນຸຍາດ',
    'ဆက်လက်', 'ခွင့်ပြု',
    'ቀጥል', 'ፍቀድ',
    'endelea', 'ruhusu',
    'komeza', 'emerera',
    // 「下一步」类（标识符 / 密码步骤的按钮，同样跟语言走）
    'next', '下一步', '下一頁', '次へ', '다음',
    'tiếptheo', 'siguiente', 'suivant', 'próximo', 'avanti', 'volgende',
    'nästa', 'næste', 'neste', 'seuraava', 'dalej', 'ďalej', 'tovább',
    'далее', 'наступний', 'berikutnya', 'seterusnya', 'ถัดไป', 'lanjut',
    'seuraava', 'следующий', 'पुढे', 'ถัดไป',
];

// 「取消 / 返回 / 拒绝」类文案，位置兜底时要排除，避免点到取消。
const CANCEL_KEYWORDS = [
    'cancel', 'back', 'deny', 'decline', 'nothanks', 'reject',
    '取消', '返回', '拒绝',
    'huỷ', 'hủy', 'quaylại', 'từchối',
    'キャンセル', '戻る', '拒否',
    '취소', '뒤로', '거부',
    'annuler', 'retour', 'refuser',
    'abbrechen', 'zurück', 'ablehnen',
    'cancelar', 'volver', 'rechazar',
    'annulla', 'indietro', 'rifiuta',
    'отмена', 'назад', 'отклонить',
    'إلغاء', 'رجوع', 'رفض',
    'ביטול', 'חזרה', 'דחייה',
    'انصراف', 'بازگشت',
    'रद्दकरें', 'वापस', 'अस्वीकार',
    'ยกเลิก', 'ย้อนกลับ', 'ปฏิเสธ',
    'batal', 'kembali', 'tolak',
    'anuluj', 'wstecz', 'odrzuć',
    'zrušit', 'zpět', 'odmítnout',
    'prekliči', 'nazaj',
    'odustani', 'natrag',
    'mégse', 'vissza',
    'anulează', 'înapoi',
    'atcelt', 'atpakaļ',
    'tühista', 'tagasi',
    'hættavið', 'tilbaka',
    'გაუქმება', 'უკან',
];

function isCancelLike(node) {
    const text = getButtonText(node);
    return CANCEL_KEYWORDS.some((keyword) => text.includes(keyword));
}

function visibleButtons() {
    return Array.from(document.querySelectorAll(
        'button, [role="button"], input[type="button"], input[type="submit"]'
    )).filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
}

function activate(node) {
    if (!node) {
        return false;
    }
    // #identifierNext / #passwordNext 这类外层是 div，真正可点的是里面的 button。
    const inner = node.querySelector && node.querySelector('button, [role="button"], input[type="submit"]');
    const target = inner && isVisible(inner) ? inner : node;
    target.focus();
    target.click();
    return true;
}

// 语言无关的主操作按钮定位：结构 → 多语言文案 → 位置兜底。
function clickPrimaryAction() {
    const structuralSelectors = [
        '#identifierNext',
        '#passwordNext',
        '#submit_approve_access',
        'button[jsname="V67aGc"]',
        'form[action*="approve"] button[type="submit"]',
        'form[action*="consent"] button[type="submit"]',
        'button[data-mdc-dialog-action="accept"]',
        'button[data-mdc-dialog-action="yes"]',
    ];
    for (const selector of structuralSelectors) {
        const node = Array.from(document.querySelectorAll(selector)).find(isVisible);
        if (node) {
            return activate(node);
        }
    }

    const candidates = visibleButtons();
    if (!candidates.length) {
        return false;
    }

    const byKeyword = candidates.find((node) => {
        const text = getButtonText(node);
        return PRIMARY_ACTION_KEYWORDS.some((keyword) => text.includes(keyword));
    });
    if (byKeyword) {
        return activate(byKeyword);
    }

    // 位置兜底：先排除「取消」类；Google 把主操作放在最右（DOM 里最后）。
    const pool = candidates.filter((node) => !isCancelLike(node));
    if (pool.length === 1) {
        return activate(pool[0]);
    }
    if (pool.length > 1) {
        return activate(pool[pool.length - 1]);
    }
    return false;
}

function clickNext() {
    return clickPrimaryAction();
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
    ], false) || clickPrimaryAction()) {
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
        // 语言无关的结构信号：Google 授权页的主操作按钮带稳定标记
        Boolean(document.querySelector('#submit_approve_access, button[jsname="V67aGc"]')) ||
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
    // 授权页的「继续 / 允许」按钮文案取决于 Google 账号的界面语言
    // （实测越南语是 "Tiếp tục"），这里统一走语言无关的主操作定位。
    if (clickPrimaryAction()) {
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


def _optional_element(owner, locator):
    """Find a late-rendered element without turning a poll into a long wait."""
    try:
        return owner.ele(locator, timeout=0.2)
    except TypeError:
        # Small protocol-compatible test doubles may only accept the locator.
        return owner.ele(locator)
    except Exception:
        return None


_CF_CHALLENGE_HOST = "challenges.cloudflare.com"


def _turnstile_backend_ids_via_cdp(page) -> list[int]:
    """Return backend node ids for Turnstile iframes, including shadow DOM nodes."""
    document = page.run_cdp("DOM.getDocument", depth=-1, pierce=True)
    backend_ids: list[int] = []

    def collect(node):
        if not isinstance(node, dict):
            return
        if str(node.get("nodeName", "")).upper() == "IFRAME":
            # attributes is a flat [name, value, name, value, ...] list
            attributes = node.get("attributes") or []
            for index in range(0, len(attributes) - 1, 2):
                if attributes[index] == "src" and _CF_CHALLENGE_HOST in str(attributes[index + 1]):
                    backend_id = node.get("backendNodeId")
                    if backend_id:
                        backend_ids.append(backend_id)
                    break
        for key in ("children", "shadowRoots", "pseudoElements"):
            for child in node.get(key) or ():
                collect(child)
        collect(node.get("contentDocument"))

    collect(document.get("root") or {})
    return backend_ids


def _scroll_turnstile_into_view(page) -> bool:
    """Scroll the challenge widget into the active viewport before coordinates are read."""
    try:
        for backend_id in _turnstile_backend_ids_via_cdp(page):
            try:
                # This also reaches a widget inside a closed shadow root, which
                # cannot be found by document.querySelector().
                page.run_cdp("DOM.scrollIntoViewIfNeeded", backendNodeId=backend_id)
                return True
            except Exception:
                continue
    except Exception:
        pass

    try:
        return bool(page.run_js(
            """
const iframe = document.querySelector(
    '.cf-turnstile iframe, iframe[src*="challenges.cloudflare.com"], '
    + '#turnstile-wrapper iframe, #turnstileForm iframe'
);
const input = document.querySelector('input[name="cf-turnstile-response"]');
const target = iframe || input?.closest('.cf-turnstile') || input?.parentElement;
if (!target || typeof target.scrollIntoView !== 'function') return false;
target.scrollIntoView({ block: 'center', inline: 'center' });
return true;
            """
        ))
    except Exception:
        return False


def _box_model_rect(page, backend_id):
    """Viewport-space rect for a CDP node, or None when it has no layout box."""
    try:
        model = page.run_cdp("DOM.getBoxModel", backendNodeId=backend_id)["model"]
    except Exception:
        return None
    border = model.get("border") or []
    if len(border) < 6:
        return None
    width = border[2] - border[0]
    height = border[5] - border[1]
    if width <= 0 or height <= 0:
        return None
    return {"x": border[0], "y": border[1], "w": width, "h": height}


def _turnstile_rect_via_cdp(page):
    """Locate the Turnstile iframe with a pierced CDP DOM walk.

    ``DOM.getDocument`` with ``pierce=True`` inlines shadow roots and iframe
    content documents, so this reaches the challenge iframe even when it sits
    behind a *closed* shadow root that ``document.querySelector`` cannot see.
    That makes it the only locator that does not depend on guessing how x.ai
    happens to nest the widget.
    """
    for backend_id in _turnstile_backend_ids_via_cdp(page):
        rect = _box_model_rect(page, backend_id)
        if rect:
            return rect
    return None


def _turnstile_iframe(page):
    """Return the Turnstile iframe element by navigating the outer shadow DOM.

    The page structure is:
      input[name="cf-turnstile-response"]  (hidden, outer page)
        → parent div → shadow root → iframe  (← this is what we return)
          → body → shadow root → input[type="checkbox"]

    DrissionPage's shadow_root property uses CDP to pierce shadow boundaries,
    so it works even when the shadow root is closed (not accessible from JS).
    """
    challenge_solution = _optional_element(page, "@name=cf-turnstile-response")
    if not challenge_solution:
        return None
    try:
        challenge_wrapper = challenge_solution.parent()
        challenge_shadow = challenge_wrapper.shadow_root
        if not challenge_shadow:
            return None
        return _optional_element(challenge_shadow, "tag:iframe")
    except Exception:
        return None


def _turnstile_checkbox(page):
    """Return the checkbox inside Turnstile's nested shadow DOM when ready."""
    try:
        challenge_iframe = _turnstile_iframe(page)
        if not challenge_iframe:
            return None
        iframe_body = _optional_element(challenge_iframe, "tag:body")
        if not iframe_body:
            return None
        iframe_shadow = iframe_body.shadow_root
        if not iframe_shadow:
            return None
        return _optional_element(iframe_shadow, "tag:input")
    except Exception:
        return None


def _human_mouse_move(page, target_x, target_y, steps=6):
    """Dispatch a short jittered mouseMoved trail that ends on the target.

    Turnstile scores the pointer trajectory arriving at a click, so a pointer
    that teleports onto the checkbox is itself a bot signal.  cf-stealth drives
    this through Playwright's mouse API; the CDP equivalent is a sequence of
    Input.dispatchMouseEvent mouseMoved events before the press.
    """
    start_x = max(1.0, target_x - random.randint(60, 160))
    start_y = max(1.0, target_y - random.randint(40, 120))
    for index in range(1, steps + 1):
        progress = index / steps
        # Land the final move exactly on target; jitter the approach only.
        jitter_x = 0.0 if index == steps else random.uniform(-6, 6)
        jitter_y = 0.0 if index == steps else random.uniform(-5, 5)
        try:
            page.run_cdp(
                "Input.dispatchMouseEvent",
                type="mouseMoved",
                x=start_x + (target_x - start_x) * progress + jitter_x,
                y=start_y + (target_y - start_y) * progress + jitter_y,
                buttons=0,
            )
        except Exception:
            return
        time.sleep(random.uniform(0.02, 0.09))


def _cdp_click_turnstile(page) -> bool:
    """Simulate a hardware-level CDP mouse click on the Turnstile iframe checkbox.

    Mirrors the CFTurnstileAutoClick approach: find the iframe's viewport rect,
    then inject mousePressed/mouseReleased via CDP so the click lands on the
    checkbox rather than the iframe edge.  The turnstile_patch extension already
    randomises screenX/screenY to cover the CDP screenX==x Chromium bug that
    Cloudflare flags.

    Locators are tried widest-reach first:
      1. Pierced CDP DOM walk — sees through closed shadow roots.
      2. DrissionPage shadow-DOM navigation — needs the wrapper/shadow nesting
         to match, but returns a live element.
      3. JS getBoundingClientRect — only works when the iframe is queryable
         from the page context (open shadow root or light DOM).
    """
    if _scroll_turnstile_into_view(page):
        # Let the browser apply the scroll before reading the iframe box. The
        # box can otherwise still contain the pre-scroll viewport coordinates.
        time.sleep(0.12)

    rect = None
    origin = ""

    # Strategy 1: pierced CDP DOM walk — independent of how the widget nests
    try:
        rect = _turnstile_rect_via_cdp(page)
        if rect:
            origin = "CDP 穿透"
    except Exception as err:
        print(f"[Turnstile] CDP 定位失败，尝试 shadow DOM: {err}")

    # Strategy 2: DrissionPage shadow-DOM navigation — works for closed shadow roots
    if not rect:
        try:
            iframe_el = _turnstile_iframe(page)
            if iframe_el:
                loc = iframe_el.rect.viewport_location   # (x, y) top-left in viewport
                size = iframe_el.rect.size               # (width, height)
                if size[0] > 0 and size[1] > 0:
                    rect = {"x": loc[0], "y": loc[1], "w": size[0], "h": size[1]}
                    origin = "shadow DOM"
        except Exception as err:
            print(f"[Turnstile] shadow DOM 定位失败，尝试 JS 方式: {err}")

    # Strategy 3: JS getBoundingClientRect — works when shadow root is open/accessible
    if not rect:
        try:
            js_rect = page.run_js(
                """
const selectors = [
    '.cf-turnstile iframe',
    'iframe[src*="challenges.cloudflare.com"]',
    '#turnstile-wrapper iframe',
    '#turnstileForm iframe',
];
let iframe = null;
for (const sel of selectors) {
    iframe = document.querySelector(sel);
    if (iframe) break;
}
if (!iframe) {
    const inp = document.querySelector('input[name="cf-turnstile-response"]');
    if (inp && inp.parentElement && inp.parentElement.shadowRoot) {
        iframe = inp.parentElement.shadowRoot.querySelector('iframe');
    }
}
if (!iframe) return null;
const r = iframe.getBoundingClientRect();
if (r.width === 0 || r.height === 0) return null;
return {x: r.x, y: r.y, w: r.width, h: r.height};
                """
            )
            if js_rect:
                rect = js_rect
                origin = "JS"
        except Exception as err:
            print(f"[Turnstile] JS 定位也失败: {err}")

    if not rect:
        return False

    # The checkbox sits near the widget's left edge (~30px in on a 300x65 widget),
    # vertically centred.  Narrow/compact renderings fall back to the rect centre.
    offset_x = 30.0 if rect["w"] >= 90 else rect["w"] / 2
    offset_y = rect["h"] / 2
    x = rect["x"] + offset_x + random.uniform(-4, 6)
    y = rect["y"] + offset_y + random.uniform(-4, 4)
    # Keep the point inside the iframe no matter how the widget rendered.
    x = max(rect["x"] + 2, min(x, rect["x"] + rect["w"] - 2))
    y = max(rect["y"] + 2, min(y, rect["y"] + rect["h"] - 2))

    print(
        f"[Turnstile] {origin}定位到 iframe "
        f"({rect['x']:.0f}, {rect['y']:.0f}) {rect['w']:.0f}x{rect['h']:.0f}，"
        f"点击 ({x:.0f}, {y:.0f})"
    )

    try:
        # Approach the checkbox before pressing: Turnstile scores the pointer path.
        _human_mouse_move(page, x, y)
        time.sleep(random.uniform(0.05, 0.18))
        page.run_cdp(
            "Input.dispatchMouseEvent",
            type="mousePressed", x=x, y=y,
            button="left", clickCount=1, modifiers=0, buttons=1,
        )
        time.sleep(random.uniform(0.05, 0.14))  # human press duration, not 0ms
        page.run_cdp(
            "Input.dispatchMouseEvent",
            type="mouseReleased", x=x, y=y,
            button="left", clickCount=1, modifiers=0, buttons=0,
        )
        return True
    except Exception as err:
        print(f"[Turnstile] CDP 点击异常，回退至元素点击: {err}")
        return False


def _get_turnstile_token(
    page,
    stop_event=None,
    *,
    timeout: float = 240.0,
    deadline: float | None = None,
):
    """Wait for the slow Turnstile widget, click it once it is ready, and read its token.

    Click strategy (tried in order each attempt):
      1. CDP mouse event via _cdp_click_turnstile — hardware-level, matches what
         CFTurnstileAutoClick does; the turnstile_patch extension covers the
         Chromium screenX/screenY bug so Cloudflare sees a realistic event.
      2. Shadow-DOM element click via _turnstile_checkbox — fallback for
         environments where run_cdp is unavailable.
    """
    deadline = deadline if deadline is not None else time.monotonic() + max(1.0, float(timeout))
    click_attempts = 0
    # The iframe can be present before it is ready to receive the first
    # pointer event. Keep the previous bounded retry behavior so a late mount
    # does not look like a missing click, while still avoiding an unbounded
    # click loop.
    max_click_attempts = 5
    last_notice = ""

    while time.monotonic() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        try:
            token = page.run_js("""
try {
    var t = typeof turnstile !== 'undefined' ? turnstile.getResponse() : null;
    if (t) return t;
} catch(e) {}
var inp = document.querySelector('input[name="cf-turnstile-response"]');
if (inp && inp.value) return inp.value;
return '';
""")
            if token:
                print(f"[Turnstile] ✓ 获取到 token（长度 {len(str(token))}）")
                return str(token)

            if click_attempts < max_click_attempts:
                # Prefer CDP hardware click; shadow-DOM element click as fallback
                cdp_ok = _cdp_click_turnstile(page)
                if cdp_ok:
                    print(
                        f"[Turnstile] ✓ CDP 鼠标事件已发送（第 {click_attempts + 1} 次），"
                        "等待 Cloudflare 返回验证结果…"
                    )
                    click_attempts += 1
                else:
                    checkbox = _turnstile_checkbox(page)
                    if checkbox:
                        print(
                            f"[Turnstile] ✓ 控件已加载，使用元素点击 checkbox（第 {click_attempts + 1} 次）"
                        )
                        checkbox.click()
                        click_attempts += 1
                        print("[Turnstile] 已点击 checkbox，等待 Cloudflare 返回验证结果…")
                    else:
                        notice = "[Turnstile] 控件尚未加载，继续等待…"
                        if notice != last_notice:
                            print(notice)
                            last_notice = notice
            else:
                notice = "[Turnstile] 已达最大点击次数，等待控件自动完成验证…"
                if notice != last_notice:
                    print(notice)
                    last_notice = notice
        except Exception as error:
            if _is_transient_page_error(error):
                # The caller must reacquire the active tab. Keeping a stale
                # page object here can wait until the deadline without ever
                # seeing the response written by the new document.
                raise
            print(f"[Turnstile] 等待控件时出现可重试错误: {error}")

        time.sleep(1.0)

    raise RuntimeError("Cloudflare 真人验证超时，未能完成验证")


_TURNSTILE_STATE_JS = """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
const challengeWidget = document.querySelector(
    '.cf-turnstile, iframe[src*="challenges.cloudflare.com"]'
);
if (!challengeInput && !challengeWidget) return 'absent';
const value = challengeInput ? String(challengeInput.value || '').trim() : '';
return value ? 'ready' : 'pending';
"""


def _detect_turnstile_state(page, wait_seconds=0.0, stop_event=None) -> str:
    """Return 'ready' / 'pending' / 'absent', polling for the late-mounting widget.

    x.ai mounts the challenge a few seconds after the name/password inputs, so a
    single early probe reports 'absent' for a form that does require verification.
    Poll until the deadline, and treat a challenge iframe found through the
    pierced CDP walk as 'pending' even when the light-DOM query misses it — the
    widget can be nested inside a closed shadow root.
    """
    deadline = time.monotonic() + max(0.0, wait_seconds)
    state = "absent"
    while True:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        state = page.run_js(_TURNSTILE_STATE_JS)
        if state != "absent":
            return state
        try:
            if _turnstile_rect_via_cdp(page):
                return "pending"
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return state
        time.sleep(0.5)


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
    timeout: int = 300,
    stop_event=None,
    action_handler=None,
) -> dict[str, str]:
    """填写注册资料并提交。

    两阶段状态机：
      阶段 1 — 填写姓名/密码，成功后设 form_filled=True，不再重填。
      阶段 2 — 处理 Turnstile 验证，获取 token，提交表单。
    form_filled=True 之后循环只做 Turnstile + 提交，不再 refresh_page 重填，
    避免每次循环重填表单导致 token 失效的死循环。
    """
    profile = _build_profile()
    given_name = profile["given_name"]
    family_name = profile["family_name"]
    password = profile["password"]

    # Use one monotonic deadline for field rendering, challenge rendering,
    # token propagation, and the final submit. This prevents nested 180s
    # waits from expiring at different points in the same browser round.
    deadline = time.monotonic() + max(1.0, float(timeout))
    turnstile_token = ""
    form_filled = False

    while time.monotonic() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")

        try:
            page = session.refresh_page()
        except Exception as error:
            if _is_transient_page_error(error):
                form_filled = False
                turnstile_token = ""
                time.sleep(0.8)
                continue
            raise
        if action_handler is not None:
            action_handler(page)
        if _dismiss_cookie_banner(page):
            time.sleep(0.3)
            continue

        # ── 阶段 1：填写表单（form_filled=True 后跳过）──────────────────────
        if not form_filled:
            try:
                filled = page.run_js(
                    """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    // 不 dispatch blur：React 受控组件收到 blur 后会重置 password 字段的值
    return String(input.value || '') === String(value || '');
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return 'not-ready:' + (!givenInput ? 'no-given' : '') + (!familyInput ? 'no-family' : '') + (!passwordInput ? 'no-password' : '');
}

const givenOk = setInputValue(givenInput, givenName);
const familyOk = setInputValue(familyInput, familyName);
const passwordOk = setInputValue(passwordInput, password);

if (!givenOk || !familyOk) return 'filled-failed:given=' + givenOk + ',family=' + familyOk + ',pw=' + passwordOk;
// 密码字段即使校验失败（React 受控组件问题）也继续，肉眼可见已填上
return 'filled';
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

            print(f"[Debug] 填写结果: filled={filled!r}")
            # not-ready 及其带冒号的详细变体（如 not-ready:no-given）都表示页面未就绪
            if str(filled).startswith('not-ready'):
                time.sleep(0.5)
                continue

            # filled / filled-failed:... 都表示找到了输入框并完成写入
            # 无论密码校验是否通过（React 受控组件问题），都继续进入 Turnstile 检测
            print(f"[*] 表单已填写（{filled}）：{given_name} {family_name}")
            form_filled = True

        # ── 阶段 2：Turnstile 验证 ──────────────────────────────────────────
        try:
            # 控件比输入框晚几秒挂载，首轮多等一会儿再判定 absent
            turnstile_state = _detect_turnstile_state(
                page,
                wait_seconds=min(8.0, max(0.0, deadline - time.monotonic()))
                if not turnstile_token
                else 0.0,
                stop_event=stop_event,
            )
        except Exception as error:
            if _is_transient_page_error(error):
                form_filled = False
                turnstile_token = ""
                time.sleep(0.8)
                continue
            raise

        if turnstile_state == "absent":
            print("[Turnstile] 页面无 Turnstile 元素，直接提交。")
        elif turnstile_state == "ready":
            print("[Turnstile] token 已就绪，直接提交。")
        elif turnstile_state == "pending" and not turnstile_token:
            print("[Turnstile] 检测到「请验证您是真人」，开始自动点击……")
            try:
                turnstile_token = _get_turnstile_token(
                    page,
                    stop_event=stop_event,
                    deadline=deadline,
                )
            except Exception as error:
                if _is_transient_page_error(error):
                    print("[Turnstile] 当前页面已切换，重新读取验证状态。")
                    form_filled = False
                    turnstile_token = ""
                    time.sleep(0.8)
                    continue
                raise
            if turnstile_token:
                print(f"[Turnstile] ✓ 获取到 token（长度 {len(turnstile_token)}），保留当前页面状态。")
                try:
                    synced = page.run_js(
                        """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) return true;
return Boolean(String(challengeInput.value || '').trim());
                        """,
                    )
                except Exception as error:
                    if _is_transient_page_error(error):
                        form_filled = False
                        turnstile_token = ""
                        time.sleep(0.8)
                        continue
                    raise
                if synced:
                    print("[Turnstile] ✓ token 已由控件写入，准备提交。")
                else:
                    print("[Turnstile] ✗ token 写入失败，下一轮重试。")
                    turnstile_token = ""
                    time.sleep(0.5)
                    continue
            else:
                print("[Turnstile] ✗ 未能获取 token，下一轮重试。")
                time.sleep(0.5)
                continue
        elif turnstile_state == "pending" and turnstile_token:
            print(f"[Turnstile] 已持有 token（长度 {len(turnstile_token)}），直接提交。")

        # ── 阶段 3：点击「完成注册」提交 ────────────────────────────────────
        time.sleep(0.5)
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
                form_filled = False
                turnstile_token = ""
                time.sleep(0.8)
                continue
            raise

        if clicked:
            print(f"[*] 已点击完成注册：{given_name} {family_name}")
            return profile

        time.sleep(0.5)

    raise Exception("未找到最终注册表单或完成注册按钮")
