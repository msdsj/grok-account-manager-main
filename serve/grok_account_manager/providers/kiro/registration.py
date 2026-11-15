"""Kiro 注册：通过 Google 账号完成 Kiro Web 登录并提取会话令牌。"""

from __future__ import annotations

import time
from typing import Any

from ...core.browser import DrissionBrowserSession, _collect_cookie_candidates, _cookie_item_parts
from ..base import RegistrationResult
from ..gpt.registration import _complete_google_signin


class KiroProvider:
    name = "kiro"
    signup_url = "https://app.kiro.dev/signin"
    chrome_lang = "en-US"
    success_cookie_name = "RefreshToken"
    mail_source: Any | None = None
    event_callback = None
    result_callback = None
    stop_event = None
    window_index: int = 0
    window_count: int = 1
    browser_window_label = ""

    def run_round(self, session: DrissionBrowserSession) -> RegistrationResult:
        if self.mail_source is None:
            raise RuntimeError("Kiro 注册缺少邮箱源")
        registration_mode = str(getattr(self.mail_source, "registration_mode", "email") or "email")
        if registration_mode != "google":
            raise RuntimeError("Kiro 注册当前需要选择「Google 账号」邮箱源")

        mailbox = self.mail_source.create_mailbox(log_callback=self._log)
        email = str(getattr(mailbox, "email", "") or "").strip()
        password = str(getattr(mailbox, "password", "") or "").strip()
        recovery_email = str(getattr(mailbox, "recovery_email", "") or "").strip()
        if not email or not password:
            raise RuntimeError("Google 账号池需要邮箱和密码")

        self._set_stage("open_signin", "打开 Kiro 登录页")
        page = session.open_new_tab(self.signup_url)
        self._wait_for_page(session, timeout=60)
        self._dismiss_cookie_banner(page)

        self._set_stage("google_login", "使用 Google 账号登录 Kiro")
        self._click_google(session, timeout=60)
        _complete_google_signin(
            session,
            email=email,
            password=password,
            recovery_email=recovery_email,
            stop_event=self.stop_event,
            action_handler=self._handle_browser_overlay_action,
        )

        self._set_stage("wait_session", "等待 Kiro 会话令牌")
        cookies = self._wait_for_kiro_cookies(session, timeout=180)
        access_token = cookies.get("AccessToken") or cookies.get("AccessToken-beta") or ""
        refresh_token = cookies.get("RefreshToken") or cookies.get("RefreshToken-beta") or ""
        session_token = cookies.get("SessionToken") or cookies.get("SessionToken-beta") or ""
        idp = cookies.get("Idp") or cookies.get("Idp-beta") or "Google"
        if not access_token or not refresh_token:
            raise RuntimeError("Kiro 登录完成但未取得 AccessToken / RefreshToken")

        credential = {
            "email": email,
            "accessToken": access_token,
            "access_token": access_token,
            "refreshToken": refresh_token,
            "refresh_token": refresh_token,
            "sessionToken": session_token,
            "idp": idp,
            "provider": idp,
            "authMethod": "social",
            "region": "us-east-1",
            "oauth_exchange_status": "ready",
        }
        self._log("success", f"Kiro 账号已登录并取得 refresh_token：{email}", email=email)
        self._set_stage("done", "Kiro 账号完成")
        return {
            "email": email,
            "credential": access_token,
            "full_credential": credential,
            "oauth_status": "ready",
        }

    def _wait_for_page(self, session: DrissionBrowserSession, *, timeout: int = 60) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.stop_event and self.stop_event.is_set():
                raise RuntimeError("任务已停止")
            page = session.refresh_page()
            if page is not None:
                try:
                    if str(getattr(page, "url", "") or ""):
                        return
                except Exception:
                    pass
            time.sleep(1)
        raise RuntimeError("Kiro 登录页加载超时")

    def _dismiss_cookie_banner(self, page) -> None:
        try:
            page.run_js(
                """
const labels = ['accept', '接受', 'agree', 'allow all', '全部接受'];
const nodes = Array.from(document.querySelectorAll('button'));
const target = nodes.find((node) => labels.some((label) => (node.innerText || '').trim().toLowerCase().includes(label)));
if (target) target.click();
"""
            )
        except Exception:
            pass

    def _click_google(self, session: DrissionBrowserSession, *, timeout: int = 60) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.stop_event and self.stop_event.is_set():
                raise RuntimeError("任务已停止")
            page = session.refresh_page()
            if page is None:
                time.sleep(1)
                continue
            try:
                clicked = page.run_js(
                    """
const nodes = Array.from(document.querySelectorAll('button, a'));
const target = nodes.find((node) => /google/i.test((node.innerText || node.textContent || '').trim()));
if (!target) return false;
target.click();
return true;
"""
                )
            except Exception:
                clicked = False
            if clicked:
                return
            time.sleep(1)
        raise RuntimeError("Kiro 登录页未找到 Google 登录入口")

    def _wait_for_kiro_cookies(
        self,
        session: DrissionBrowserSession,
        *,
        timeout: int = 180,
    ) -> dict[str, str]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.stop_event and self.stop_event.is_set():
                raise RuntimeError("任务已停止")
            page = session.refresh_page()
            if page is None:
                time.sleep(1)
                continue
            try:
                url = str(getattr(page, "url", "") or "")
            except Exception:
                url = ""
            cookies, _errors = _collect_cookie_candidates(session, page)
            values: dict[str, str] = {}
            for item in cookies:
                name, value, _domain = _cookie_item_parts(item)
                if name and value:
                    values[name] = value
            if any(name.startswith("RefreshToken") for name in values):
                return values
            if "app.kiro.dev" not in url and "kiro.dev" not in url:
                time.sleep(1)
                continue
            time.sleep(1)
        raise RuntimeError("等待 Kiro 会话令牌超时")

    def _handle_browser_overlay_action(self, page) -> None:
        self._dismiss_cookie_banner(page)

    def _set_stage(self, stage: str, message: str) -> None:
        self._log("info", message, stage=stage)

    def _log(self, level: str, message: str, **extra) -> None:
        if callable(self.event_callback):
            self.event_callback(level, message, **extra)
