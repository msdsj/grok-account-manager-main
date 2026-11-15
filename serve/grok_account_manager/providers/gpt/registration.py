"""ChatGPT (OpenAI) 注册流程。

页面交互走 page.run_js() 注入 JS 实现浏览器自动化，参考 grok.py 的实现模式。
OpenAI 注册需要处理 Sentinel PoW 验证，邮箱验证码，以及 OAuth 流程。

实现借鉴：
- any-auto-register: 完整的 HTTP-based 注册引擎
- chatgpt2api: Sentinel/PoW/Turnstile 工具函数
"""

from __future__ import annotations

import base64
import json
import os
import random
import secrets
import time
import uuid
from collections.abc import Callable
from typing import Any

from DrissionPage.errors import PageDisconnectedError

from ...core.browser import DrissionBrowserSession
from ...mail.sources import MailboxSource, VerificationMailbox, DuckMailSource
from ..base import RegistrationResult
# Google 第三方登录的分步处理器，与 grok 共用同一份实现，
# 避免每个 provider 各写一套 Google 页面选择器。
from ..grok.registration import _handle_google_login_step
from .utils import (
    SentinelTokenGenerator,
    generate_datadog_trace_headers,
    generate_pkce,
    OPENAI_AUTH,
    CHATGPT_APP,
    OPENAI_API_ENDPOINTS,
    DEFAULT_USER_AGENT,
)

# 从 chatgpt_utils 导入的 URL 常量
SIGNUP_URL = f"{CHATGPT_APP}/"

# 浏览器阶段标签
BROWSER_STAGE_LABELS = {
    "starting": "准备开始",
    "create_mailbox": "创建或领取邮箱",
    "open_signup": "打开注册页",
    "fill_email": "填写邮箱地址",
    "wait_email_code": "等待邮箱验证码",
    "fill_otp": "填写验证码",
    "set_password": "设置账号密码",
    "complete": "注册完成",
    "done": "完成",
}


class ChatGPTProvider:
    """ChatGPT 注册提供者"""

    name = "chatgpt"
    signup_url = SIGNUP_URL
    chrome_lang = "en-US"
    success_cookie_name = "__Secure-next-auth.session-token"
    mail_source: MailboxSource | None = None
    event_callback = None
    result_callback = None
    stop_event = None
    window_index: int = 0  # 窗口索引，用于并发时计算窗口位置
    window_count: int = 1  # 总窗口数量
    max_registration_attempts = 3
    browser_window_label = ""

    def run_round(self, session: DrissionBrowserSession) -> RegistrationResult:
        """运行一轮注册"""
        raw_attempts = os.environ.get(
            "CHATGPT_ACCOUNT_MANAGER_MAX_REGISTRATION_ATTEMPTS",
            str(self.max_registration_attempts),
        )
        try:
            max_attempts = max(1, min(4, int(raw_attempts)))
        except (TypeError, ValueError):
            max_attempts = self.max_registration_attempts

        for attempt in range(1, max_attempts + 1):
            try:
                return self._run_round_once(session)
            except Exception as error:
                stopped = bool(self.stop_event and self.stop_event.is_set())
                pool_exhausted = "邮箱池已耗尽" in str(error)
                if stopped or pool_exhausted or attempt >= max_attempts:
                    raise
                delay = random.uniform(1.0, 3.0)
                self._log(
                    "warning",
                    f"注册页面流程失败，{delay:.1f}s 后重试 ({attempt}/{max_attempts - 1})：{error}",
                    stage="registration_retry",
                )
                self._interruptible_sleep(delay)
                session.restart()

        raise RuntimeError("注册重试结束但没有返回结果")

    def _run_round_once(self, session: DrissionBrowserSession) -> RegistrationResult:
        """执行一轮完整注册流程。

        流程步骤：
        1. 创建临时邮箱
        2. 打开 ChatGPT 注册页
        3. 填写邮箱并提交
        4. 等待并填写 OTP 验证码
        5. 设置密码
        6. 等待 session token
        7. 可选：通过 OAuth 获取 refresh_token
        """
        self._browser_session = session
        self.current_email = ""
        self.registration_succeeded = False

        self._set_stage("starting", "准备开始 ChatGPT 注册")
        if self.stop_event and self.stop_event.is_set():
            raise RuntimeError("任务已在开始前停止")

        # 创建邮箱 - 与 Grok 保持一致，支持多种邮箱源
        mail_source = self.mail_source or DuckMailSource()
        self._set_stage("create_mailbox", "正在创建/领取邮箱")
        try:
            mailbox = mail_source.create_mailbox(log_callback=self._log)
        except TypeError:
            mailbox = mail_source.create_mailbox()

        self.current_email = str(getattr(mailbox, "email", "") or "")
        self._log("info", f"本轮使用邮箱：{self.current_email}")
        if not self.current_email:
            raise RuntimeError("邮箱创建失败")

        # 打开注册页
        self._set_stage("open_signup", "正在打开 ChatGPT 注册页")
        try:
            session.open_url(self.signup_url)
            self._render_browser_step_overlay("open_signup", "浏览器已打开，正在进入注册页")
        except Exception as e:
            raise RuntimeError(f"打开注册页失败: {e}")

        # 等待页面加载并点击注册按钮
        try:
            _wait_and_click_signup_button(
                session,
                stop_event=self.stop_event,
                action_handler=self._handle_browser_overlay_action,
            )
            self._log("info", "成功进入注册流程")
        except Exception as e:
            raise RuntimeError(f"点击注册按钮失败: {e}")

        if self.stop_event and self.stop_event.is_set():
            raise RuntimeError("任务已停止")

        # 邮箱源决定注册通道：
        # - "google"（选了「Google 账号」邮箱源）：走 Google 第三方登录，
        #   不再用该邮箱去收验证码。
        # - 其它：邮箱 + 验证码 + 密码。
        registration_mode = str(getattr(mail_source, "registration_mode", "email") or "email")
        self._log(
            "info",
            f"注册通道：{'Google 账号' if registration_mode == 'google' else '邮箱验证码'}",
        )

        if registration_mode == "google":
            g_email = str(getattr(mailbox, "email", "") or "").strip()
            g_password = str(getattr(mailbox, "password", "") or "").strip()
            g_recovery = str(getattr(mailbox, "recovery_email", "") or "").strip()
            if not g_email or not g_password:
                raise RuntimeError(
                    "Google 账号注册需要邮箱和密码（Google 账号池格式：邮箱----密码----辅助邮箱）"
                )

            self._set_stage("google_login", "正在使用 Google 账号登录")
            try:
                _click_google_signin(
                    session,
                    stop_event=self.stop_event,
                    action_handler=self._handle_browser_overlay_action,
                )
                _complete_google_signin(
                    session,
                    email=g_email,
                    password=g_password,
                    recovery_email=g_recovery,
                    stop_event=self.stop_event,
                    action_handler=self._handle_browser_overlay_action,
                )
            except Exception as e:
                raise RuntimeError(f"Google 账号登录失败: {e}") from e

            email = g_email
            self.current_email = email
            self._log("success", "Google 账号授权完成，继续完成注册信息")
        else:
            # 填写邮箱
            self._set_stage("fill_email", "正在填写邮箱地址")
            try:
                email = _fill_email_and_continue(
                    session,
                    mailbox.email,
                    stop_event=self.stop_event,
                    action_handler=self._handle_browser_overlay_action,
                )
                self.current_email = email
                self._log("info", f"ChatGPT 已向 {email} 发送验证码，开始读取邮箱")
            except Exception as e:
                raise RuntimeError(f"填写邮箱失败: {e}")

            if self.stop_event and self.stop_event.is_set():
                raise RuntimeError("任务已停止")

            # 等待并填写验证码
            self._set_stage("wait_email_code", "正在等待邮箱验证码")
            try:
                code = _fill_otp_and_continue(
                    session,
                    email,
                    mailbox,
                    self.stop_event,
                    action_handler=self._handle_browser_overlay_action,
                )
                self._log("success", f"验证码 {code} 已验证，准备设置密码")
            except Exception as e:
                raise RuntimeError(f"验证码验证失败: {e}")

        if self.stop_event and self.stop_event.is_set():
            raise RuntimeError("任务已停止")

        # 填写姓名和年龄（注册最后一步）
        self._set_stage("fill_profile", "正在填写个人信息")
        try:
            _fill_name_and_age(
                session,
                stop_event=self.stop_event,
                action_handler=self._handle_browser_overlay_action,
            )
            self._log("info", "已填写姓名和年龄，等待进入主页...")
        except Exception as e:
            raise RuntimeError(f"填写个人信息失败: {e}")

        if self.stop_event and self.stop_event.is_set():
            raise RuntimeError("任务已停止")

        # 等待页面跳转到 ChatGPT 主页（确认注册成功）
        self._set_stage("wait_homepage", "等待进入 ChatGPT 主页")
        try:
            _wait_for_chatgpt_homepage(
                session,
                timeout=30,
                stop_event=self.stop_event,
                action_handler=self._handle_browser_overlay_action,
            )
            self._log("success", "已成功进入 ChatGPT 主页，注册完成")
        except Exception as e:
            raise RuntimeError(f"等待主页失败: {e}")

        if self.stop_event and self.stop_event.is_set():
            raise RuntimeError("任务已停止")

        # 获取完整的 session 信息（直接打开新标签页）
        self._set_stage("fetch_session", "正在获取完整会话信息")
        try:
            print("[DEBUG] 准备调用 _fetch_session_data...")
            session_data = _fetch_session_data(
                session,
                stop_event=self.stop_event,
                action_handler=self._handle_browser_overlay_action,
            )
            print(f"[DEBUG] _fetch_session_data 返回值类型: {type(session_data)}")
            print(f"[DEBUG] session_data 是否为 None: {session_data is None}")

            if session_data:
                print(f"[DEBUG] session_data 包含 {len(session_data)} 个字段")
                print(f"[DEBUG] session_data 的键: {list(session_data.keys())[:5]}")  # 只显示前5个键

            self._log("success", f"成功获取完整会话信息")

            # 从 session_data 中提取 session token（如果有的话）
            session_token = None
            if session_data and isinstance(session_data, dict):
                # 尝试从多个可能的字段提取 token
                session_token = (
                    session_data.get("accessToken") or
                    session_data.get("session_token") or
                    session_data.get("token") or
                    ""
                )
                print(f"[DEBUG] 提取到的 session_token 长度: {len(session_token) if session_token else 0}")
        except Exception as e:
            print(f"[ERROR] 获取会话信息异常: {e}")
            import traceback
            traceback.print_exc()
            self._log("warning", f"获取会话信息失败: {e}")
            session_data = None
            session_token = None

        print(f"[DEBUG] 准备构建 result 对象...")
        print(f"[DEBUG] email: {email}")
        print(f"[DEBUG] session_token: {session_token[:20] if session_token else 'None'}...")
        print(f"[DEBUG] session_data 类型: {type(session_data)}")

        result: RegistrationResult = {
            "email": email,
            "credential": session_token or "pending",
            "profile": {
                "email": email,
                "password": None,  # OpenAI 新流程不需要密码
                "auth_provider": "email",
                "session_data": session_data,  # 保存完整的 session 数据
            },
            "oauth_status": "ready",  # 标记为 ready，因为已经获取到完整 session 数据
        }

        print(f"[DEBUG] result 对象已构建，包含字段: {list(result.keys())}")
        print(f"[DEBUG] 设置 registration_succeeded = True")
        self.registration_succeeded = True

        print(f"[DEBUG] 设置阶段为 done")
        self._set_stage("done", "本轮注册完成")

        print(f"[DEBUG] 准备返回 result")
        return result

    def _interruptible_sleep(self, seconds: float) -> None:
        """可中断的睡眠"""
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.stop_event and self.stop_event.is_set():
                raise RuntimeError("任务已停止")
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def _set_stage(self, stage: str, message: str | None = None) -> None:
        """设置当前阶段"""
        self.current_stage = stage
        if message:
            self._log("info", f"阶段 {stage}：{message}", stage=stage)
        self._render_browser_step_overlay(stage, message or stage)

    def _render_browser_step_overlay(self, stage: str, message: str) -> None:
        """渲染浏览器步骤覆盖层"""
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
            return

    def _handle_browser_overlay_action(self, page) -> None:
        """处理浏览器覆盖层动作"""
        action = _consume_browser_step_action(page)
        if action == "retry":
            self._log(
                "warning",
                "用户在浏览器窗口请求再次运行当前注册流程",
                stage=self.current_stage,
            )
            raise RuntimeError("用户在浏览器窗口请求再次运行")
        if action == "continue":
            self._log(
                "info",
                "用户在浏览器窗口确认继续执行",
                stage=self.current_stage,
            )

    def _log(self, level: str, message: str, **extra) -> None:
        """记录日志"""
        callback = self.event_callback
        if callback is None:
            return
        try:
            callback(level, message, **extra)
        except Exception:
            pass


def _render_browser_step_overlay(
    page,
    *,
    stage: str,
    stage_label: str,
    message: str,
    window_label: str = "",
) -> bool:
    """渲染浏览器步骤覆盖层"""
    try:
        return bool(page.run_js(
            r"""
const stage = String(arguments[0] || 'working');
const stageLabel = String(arguments[1] || stage);
const message = String(arguments[2] || '正在处理');
const windowLabel = String(arguments[3] || '注册窗口');
const overlayId = '__chatgpt-registration-step-overlay';

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

if (typeof window.__chatgptRegistrationStepAction !== 'string') {
    window.__chatgptRegistrationStepAction = '';
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
    window.__chatgptRegistrationStepAction = action;
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
    """读取并清除浏览器覆盖层动作"""
    try:
        action = str(page.run_js(
            """
const action = String(window.__chatgptRegistrationStepAction || '');
window.__chatgptRegistrationStepAction = '';
return action;
            """
        ) or "")
    except Exception:
        return ""
    return action if action in {"continue", "retry"} else ""


def _is_transient_page_error(error: Exception) -> bool:
    """判断是否为暂时性页面错误"""
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


def _wait_and_click_signup_button(
    session: DrissionBrowserSession,
    timeout=60,
    stop_event=None,
    action_handler=None,
):
    """等待并点击注册按钮和 Continue with email 按钮。

    并发多开时页面加载更慢，这里把超时放宽到 60 秒；点击也改成更稳健的实现
    （见下方 JS 注释），避免点到外层容器或页面还没稳定就点空。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")

        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)

        try:
            state = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

// 步骤提示卡片是我们自己注入的，永远不参与匹配。
function isOwnOverlay(node) {
    return !!(node.closest && node.closest('#__chatgpt-registration-step-overlay'));
}

function textOf(node) {
    return String(node.innerText || node.textContent || '').toLowerCase().replace(/\s+/g, ' ').trim();
}

// 在所有候选里挑「最贴合」的那个：优先真正的 button/a/[role=button]，
// 再优先文本最短的——外层容器会把整段文案都包进去，越长越可能是包裹层，
// 直接 .click() 打在外层容器上不会触发真正的注册动作。
function pick(nodes, needles) {
    const matched = [];
    for (const node of nodes) {
        if (isOwnOverlay(node) || !isVisible(node)) continue;
        const text = textOf(node);
        if (!text) continue;
        if (!needles.every((needle) => text.includes(needle))) continue;
        const tag = node.tagName.toLowerCase();
        const role = String(node.getAttribute('role') || '').toLowerCase();
        const isRealControl = tag === 'button' || tag === 'a' || role === 'button' || role === 'link';
        matched.push({ node, isRealControl, length: text.length });
    }
    if (!matched.length) return null;
    matched.sort((a, b) => {
        if (a.isRealControl !== b.isRealControl) return a.isRealControl ? -1 : 1;
        return a.length - b.length;
    });
    return matched[0].node;
}

// 用完整的指针事件序列点击，比裸 .click() 更接近真实用户操作，
// 能触发那些监听 mousedown / pointerdown 的下拉与弹层组件。
function robustClick(node) {
    if (!node) return false;
    try { node.scrollIntoView({ block: 'center', inline: 'center' }); } catch (error) {}
    let x = 0;
    let y = 0;
    try {
        const rect = node.getBoundingClientRect();
        x = rect.left + rect.width / 2;
        y = rect.top + rect.height / 2;
    } catch (error) {}
    const options = {
        bubbles: true, cancelable: true, composed: true, view: window,
        clientX: x, clientY: y, button: 0,
    };
    const fire = (type, ctor) => {
        try { node.dispatchEvent(new ctor(type, options)); } catch (error) {}
    };
    fire('pointerdown', window.PointerEvent || window.MouseEvent);
    fire('mousedown', window.MouseEvent);
    try { node.focus({ preventScroll: true }); } catch (error) {}
    fire('pointerup', window.PointerEvent || window.MouseEvent);
    fire('mouseup', window.MouseEvent);
    fire('click', window.MouseEvent);
    return true;
}

const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], [role="link"]'));

// 已经在邮箱输入页面（邮箱输入框可见且可交互）
const emailInput = Array.from(document.querySelectorAll(
    'input[type="email"], input[name="email"], input[autocomplete="username"], input#login-email'
)).find(node => isVisible(node) && !node.disabled && !node.readOnly);

if (emailInput) {
    // 只有还存在 "Continue with email" 按钮时才说明仍在选择登录方式的模态框里；
    // 否则已经进入邮箱填写页面。
    const continueWithEmail = pick(buttons, ['continue', 'email']);
    if (!continueWithEmail) {
        return { status: 'ready', url: location.href };
    }
}

// 优先查找 "Continue with email" 按钮（在选择登录方式的模态框中）
const continueWithEmailButton = pick(buttons, ['continue', 'email']);
if (continueWithEmailButton) {
    robustClick(continueWithEmailButton);
    return { status: 'clicked_email', url: location.href };
}

// 查找注册按钮：按更具体的文案依次匹配，避免笼统的 register 先命中别的元素
const signupButton = pick(buttons, ['sign up'])
    || pick(buttons, ['signup'])
    || pick(buttons, ['create account'])
    || pick(buttons, ['get started'])
    || pick(buttons, ['register']);

if (signupButton) {
    robustClick(signupButton);
    return { status: 'clicked_signup', url: location.href };
}

return { status: 'waiting', url: location.href };
            """)
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(0.8)
                continue
            raise

        if isinstance(state, dict):
            status = state.get("status")
            if status == "ready":
                return True
            if status in ("clicked_email", "clicked_signup"):
                time.sleep(1)
                continue

        time.sleep(0.5)

    raise Exception("未找到注册按钮或超时")


def _fill_email_and_continue(
    session: DrissionBrowserSession,
    email: str,
    timeout=30,
    stop_event=None,
    action_handler=None,
) -> str:
    """填写邮箱并继续"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")

        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)

        try:
            filled = page.run_js(
                """
const email = arguments[0];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll(
    'input[type="email"], input[name="email"], input[autocomplete="username"], input#login-email'
)).find(node => isVisible(node) && !node.disabled && !node.readOnly);

if (!input) return 'not-ready';

input.focus();
input.click();

const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) {
    valueSetter.call(input, email);
} else {
    input.value = email;
}

input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
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
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(0.8)
            page = session.refresh_page()
            try:
                clicked = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

// 查找提交按钮，排除第三方登录按钮
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'));
const continueButton = buttons.find(node => {
    if (!isVisible(node) || node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
    const text = (node.innerText || node.textContent || '').toLowerCase().trim();

    // 排除第三方登录按钮
    const thirdPartyKeywords = ['phone', 'mobile', 'email', 'google', 'apple', 'microsoft', 'facebook', 'github'];
    if (thirdPartyKeywords.some(keyword => text.includes(keyword))) return false;

    // 查找包含 continue 或 next 的按钮
    return text.includes('continue') || text.includes('next');
});

if (!continueButton) return false;
continueButton.click();
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
                print(f"[*] 已填写邮箱并点击继续: {email}")
                return email

        time.sleep(0.5)

    raise Exception("未找到邮箱输入框或继续按钮")


def _fill_otp_and_continue(
    session: DrissionBrowserSession,
    email: str,
    mailbox: VerificationMailbox,
    stop_event=None,
    timeout: int = 180,
    action_handler=None,
) -> str:
    """填写 OTP 验证码并继续"""
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

        try:
            filled = page.run_js(
                """
const code = String(arguments[0] || '').trim();

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
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
}

const input = Array.from(document.querySelectorAll(
    "input[inputmode='numeric'], input[autocomplete='one-time-code'], input[type='tel']"
)).find(node => isVisible(node) && !node.disabled && !node.readOnly);

if (!input) return 'not-ready';

input.focus();
input.click();
setNativeValue(input, code);
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: code, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: code, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));

if (String(input.value || '').trim() !== code) {
    return 'mismatch';
}

input.blur();
return 'filled';
                """,
                code,
            )
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(1)
                continue
            raise

        if filled == 'not-ready':
            time.sleep(0.5)
            continue

        if filled != 'filled':
            time.sleep(0.5)
            continue

        if filled == 'filled':
            print(f"[*] 已填写验证码: {code}")

            # 填写完验证码后，点击 Continue 按钮
            time.sleep(1)
            try:
                clicked = page.run_js("""
const buttons = Array.from(document.querySelectorAll('button'));
const continueBtn = buttons.find(btn => {
    const text = (btn.innerText || btn.textContent || '').toLowerCase().trim();
    return text === 'continue' && !btn.disabled;
});
if (continueBtn) {
    continueBtn.click();
    return 'clicked';
}
return 'not-found';
                """)
                if clicked == 'clicked':
                    print("[*] 已点击 Continue 按钮")
                    time.sleep(2)
                else:
                    print("[警告] 未找到 Continue 按钮，等待自动跳转")
                    time.sleep(2)
            except Exception as e:
                print(f"[警告] 点击 Continue 按钮失败: {e}")
                time.sleep(2)

            return code

        time.sleep(0.5)

    raise Exception("未找到验证码输入框或验证失败")


def _set_password_and_submit(
    session: DrissionBrowserSession,
    timeout: int = 30,
    stop_event=None,
    action_handler=None,
) -> str:
    """设置密码并提交"""
    password = _generate_password()
    deadline = time.time() + timeout

    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")

        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)

        try:
            filled = page.run_js(
                """
const password = arguments[0];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[type="password"]'))
    .find(node => isVisible(node) && !node.disabled && !node.readOnly);

if (!input) return 'not-ready';

input.focus();
input.click();

const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) {
    valueSetter.call(input, password);
} else {
    input.value = password;
}

input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: password, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: password, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));

if (String(input.value || '') !== password) {
    return 'mismatch';
}

input.blur();
return 'filled';
                """,
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
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(0.8)
            page = session.refresh_page()
            try:
                clicked = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'));
const submitButton = buttons.find(node => {
    if (!isVisible(node) || node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
    const text = (node.innerText || node.textContent || '').toLowerCase().trim();
    return text.includes('continue') || text.includes('sign up') || text.includes('create');
});

if (!submitButton) return false;
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
                print(f"[*] 已设置密码并提交")
                return password

        time.sleep(0.5)

    raise Exception("未找到密码输入框或提交按钮")


def _click_google_signin_js_body() -> str:
    """返回点击「Continue with Google」的 JS。

    用文案匹配而不是 class / data-testid：OpenAI 的 auth 页面会改版，
    且中英文文案不同，按多种文案匹配最稳。
    注意 auth.openai.com 有 Cloudflare 挑战，按钮要等挑战过了才出现，
    所以调用方必须轮询而不是一次性查找。
    """
    return r"""
function visible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const nodes = [...document.querySelectorAll('button, a, div[role="button"]')];
const pattern = /(continue with google|sign ?in with google|log ?in with google|google 继续|使用 google|以 google 继续)/i;
const btn = nodes.find((node) => {
    if (!visible(node)) return false;
    const text = (node.innerText || node.textContent || '').trim();
    return text.length < 60 && pattern.test(text);
});
if (!btn) return 'no-button';
btn.click();
return 'clicked';
"""


def _click_google_signin(
    session: DrissionBrowserSession,
    stop_event=None,
    action_handler=None,
    timeout: int = 120,
) -> None:
    """点击「Continue with Google」。

    auth.openai.com 会先弹 Cloudflare 安全验证，通过后登录按钮才渲染出来，
    因此这里持续轮询直到按钮出现并可点。
    """
    body = _click_google_signin_js_body()
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)
        try:
            last = str(page.run_js(body) or "")
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(1)
                continue
            last = ""
        if last == "clicked":
            print("[*] 已点击 Continue with Google")
            return
        time.sleep(1)
    raise RuntimeError(
        f"未找到或未能点击 Continue with Google（{last or 'unknown'}）；"
        "若页面停在 Cloudflare 验证，请确认浏览器为可见模式后重试"
    )


def _complete_google_signin(
    session: DrissionBrowserSession,
    *,
    email: str,
    password: str,
    recovery_email: str = "",
    stop_event=None,
    action_handler=None,
    timeout: int = 240,
) -> None:
    """在 Google 登录页完成账号登录与授权。

    结束条件是「离开 Google」——后续的姓名/生日填写和主页判定由调用方继续处理。
    """
    deadline = time.time() + timeout
    recovery_done = False
    email_next_clicked = False
    password_next_clicked = False
    account_choice_clicked = False
    consent_clicked = False
    speedbump_clicked = False
    last_google_path = ""
    entered_google = False

    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)
        try:
            href = str(getattr(page, "url", "") or "")
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(1)
                continue
            raise

        on_google = "accounts.google." in href or "google.com" in href
        if not on_google:
            if entered_google:
                print("[*] Google 授权完成，继续后续注册步骤")
                return
            time.sleep(1)
            continue

        entered_google = True
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
            # grok._handle_google_login_step 全部是位置参数。
            action = _handle_google_login_step(
                page,
                email,
                password,
                recovery_email,
                recovery_done,
                email_next_clicked,
                password_next_clicked,
                account_choice_clicked,
                consent_clicked,
                speedbump_clicked,
            )
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(1)
                continue
            raise

        if action == "email":
            email_next_clicked = True
            print(f"[*] 已填写 Google 邮箱: {email}")
        elif action == "password":
            password_next_clicked = True
            print("[*] 已填写 Google 密码并提交")
        elif action == "recovery":
            recovery_done = True
            print("[*] 已填写 Google 辅助邮箱")
        elif action == "account-choice":
            account_choice_clicked = True
            print("[*] 已选择 Google 账号")
        elif action == "consent":
            consent_clicked = True
            print("[*] 已确认 Google 授权")
        elif action == "speedbump":
            speedbump_clicked = True
            print("[*] 已确认 Google 提示页")
        elif action == "missing-password":
            raise RuntimeError("Google 账号池里的密码为空")
        elif action == "blocked":
            raise RuntimeError(
                "Google 登录页触发风控（You have been blocked），请更换出口 IP 或账号"
            )
        elif action == "manual":
            print("[警告] Google 登录需要人工处理，等待浏览器完成授权")

        time.sleep(1)

    raise RuntimeError("Google 账号登录超时")


def _wait_for_session_cookie(
    session: DrissionBrowserSession,
    cookie_name: str,
    timeout: int = 60,
    stop_event=None,
    action_handler=None,
) -> str:
    """等待 session cookie"""
    deadline = time.time() + timeout

    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")

        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)

        try:
            cookies = page.cookies(all_info=True)
            for cookie in cookies:
                if cookie.get("name") == cookie_name:
                    value = cookie.get("value", "")
                    if value:
                        print(f"[*] 已获取 session token")
                        return value
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(1)
                continue

        time.sleep(1)

    raise Exception(f"超时未获取到 {cookie_name} cookie")


def _generate_password(length: int = 12) -> str:
    """生成随机密码，符合 OpenAI 密码要求。

    密码要求：
    - 至少 8 个字符
    - 包含至少一个小写字母
    - 包含至少一个数字
    - 包含至少一个特殊字符（推荐）

    Args:
        length: 密码长度，默认 12

    Returns:
        生成的密码
    """
    charset = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    specials = "!@#$%"

    if length < 10:
        length = 10

    # 确保密码包含所有必需的字符类型
    password_chars = [
        secrets.choice("abcdefghijklmnopqrstuvwxyz"),  # 至少一个小写字母
        secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),  # 至少一个大写字母
        secrets.choice("0123456789"),                   # 至少一个数字
        secrets.choice(specials),                       # 至少一个特殊字符
    ]

    # 填充剩余长度
    for _ in range(length - len(password_chars)):
        password_chars.append(secrets.choice(charset + specials))

    # 随机打乱顺序
    random.shuffle(password_chars)

    return ''.join(password_chars)[:length]


def _fill_name_and_age(
    session: DrissionBrowserSession,
    timeout: int = 60,
    stop_event=None,
    action_handler=None,
) -> None:
    """填写姓名和年龄信息"""
    # 生成随机姓名（大写，中间有空格）
    first_names = ["JAMES", "JOHN", "ROBERT", "MICHAEL", "WILLIAM", "DAVID", "RICHARD", "JOSEPH", "THOMAS", "CHARLES"]
    last_names = ["SMITH", "JOHNSON", "WILLIAMS", "BROWN", "JONES", "GARCIA", "MILLER", "DAVIS", "RODRIGUEZ", "MARTINEZ"]
    full_name = f"{random.choice(first_names)} {random.choice(last_names)}"

    # 生成随机年龄（20-40）
    age = random.randint(20, 40)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")

        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)

        try:
            result = page.run_js(
                r"""
const fullName = String(arguments[0] || '');
const age = String(arguments[1] || '');

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
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
}

// 查找姓名输入框
const nameInputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type])'));
const nameInput = nameInputs.find(input => {
    if (!isVisible(input) || input.disabled || input.readOnly) return false;
    const label = input.getAttribute('aria-label') || input.placeholder || '';
    return label.toLowerCase().includes('name');
});

// 查找年龄输入框
const ageInputs = Array.from(document.querySelectorAll('input[type="text"], input[type="number"], input:not([type])'));
const ageInput = ageInputs.find(input => {
    if (!isVisible(input) || input.disabled || input.readOnly) return false;
    const label = input.getAttribute('aria-label') || input.placeholder || '';
    return label.toLowerCase().includes('age');
});

if (!nameInput || !ageInput) {
    return 'not-ready';
}

// 填写姓名
nameInput.focus();
nameInput.click();
setNativeValue(nameInput, fullName);
nameInput.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: fullName, inputType: 'insertText' }));
nameInput.dispatchEvent(new InputEvent('input', { bubbles: true, data: fullName, inputType: 'insertText' }));
nameInput.dispatchEvent(new Event('change', { bubbles: true }));
nameInput.blur();

// 填写年龄
ageInput.focus();
ageInput.click();
setNativeValue(ageInput, age);
ageInput.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: age, inputType: 'insertText' }));
ageInput.dispatchEvent(new InputEvent('input', { bubbles: true, data: age, inputType: 'insertText' }));
ageInput.dispatchEvent(new Event('change', { bubbles: true }));
ageInput.blur();

// 查找并点击 Continue 按钮
const buttons = Array.from(document.querySelectorAll('button'));
const continueBtn = buttons.find(btn => {
    if (!isVisible(btn) || btn.disabled) return false;
    const text = (btn.innerText || btn.textContent || '').toLowerCase().trim();
    return text === 'continue';
});

if (continueBtn) {
    continueBtn.click();
    return 'submitted';
}

return 'filled';
                """,
                full_name,
                str(age),
            )
        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(1)
                continue
            raise

        if result == 'not-ready':
            time.sleep(0.5)
            continue

        if result in ('filled', 'submitted'):
            print(f"[*] 已填写姓名: {full_name}, 年龄: {age}")
            if result == 'submitted':
                print("[*] 已点击 Continue 按钮，等待页面跳转...")
                time.sleep(5)  # 增加等待时间，让页面有足够时间跳转到密码页
            else:
                time.sleep(2)
            return

        time.sleep(0.5)

    raise Exception("填写姓名和年龄超时")




def _fetch_session_data(
    session: DrissionBrowserSession,
    timeout: int = 30,
    stop_event=None,
    action_handler=None,
) -> dict | None:
    """在新标签页打开 /api/auth/session 获取完整会话信息"""
    session_url = "https://chatgpt.com/api/auth/session"

    try:
        print(f"[*] 正在新标签页打开 {session_url}")

        # 使用 session.open_new_tab 打开新标签页
        session.open_new_tab(session_url)
        time.sleep(3)  # 等待页面加载

        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")

        # 获取新标签页
        page = session.refresh_page()

        # 获取页面文本内容（应该是 JSON）
        try:
            page_text = page.run_js("return document.body.innerText || document.body.textContent || '';")
            if not page_text or not page_text.strip():
                raise Exception("页面内容为空")

            # 尝试解析为 JSON
            session_data = json.loads(page_text)

            print(f"[*] 成功获取 session 数据")
            print(f"[DEBUG] Session 数据包含字段: {list(session_data.keys())}")

            return session_data

        except json.JSONDecodeError as e:
            print(f"[警告] 无法解析 session 数据为 JSON: {e}")
            print(f"[DEBUG] 页面内容: {page_text[:200]}")
            return {"raw_text": page_text}

    except Exception as e:
        print(f"[警告] 获取 session 数据失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def _wait_for_chatgpt_homepage(
    session: DrissionBrowserSession,
    timeout: int = 30,
    stop_event=None,
    action_handler=None,
) -> None:
    """等待页面跳转到 ChatGPT 主页，确认注册成功"""
    deadline = time.time() + timeout

    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")

        page = session.refresh_page()
        if action_handler is not None:
            action_handler(page)

        try:
            # 检查是否已经进入 ChatGPT 主页
            # 主页特征：URL 是 chatgpt.com 且包含 "What's on the agenda" 或输入框
            current_url = page.url.lower()
            
            if 'chatgpt.com' in current_url and '/auth/' not in current_url:
                # 检查页面是否包含主页元素
                has_chat_interface = page.run_js("""
                    // 检查是否有聊天输入框或主页文本
                    const hasInput = document.querySelector('textarea[placeholder*="Ask"]') !== null ||
                                   document.querySelector('input[placeholder*="Ask"]') !== null;
                    const hasAgenda = document.body.innerText.includes("What's on the agenda") ||
                                    document.body.innerText.includes("New chat");
                    return hasInput || hasAgenda;
                """)
                
                if has_chat_interface:
                    print("[*] 已确认进入 ChatGPT 主页")
                    time.sleep(2)  # 再等待 2 秒确保 session 完全生成
                    return

        except Exception as error:
            if _is_transient_page_error(error):
                time.sleep(1)
                continue

        time.sleep(1)

    raise Exception("等待 ChatGPT 主页超时")
