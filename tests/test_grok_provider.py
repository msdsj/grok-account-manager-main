import threading
import unittest
from unittest.mock import Mock, patch

from grok_account_manager.grok.oauth_exchange import CloudflareBlockedError, OAuthTerminalError
from grok_account_manager.providers.grok import (
    BrowserStepRetryRequested,
    GrokProvider,
    _dismiss_cookie_banner,
    _fill_code_and_submit,
    _get_turnstile_token,
    _render_browser_step_overlay,
    _resubmit_final_profile,
    _wait_for_sso_cookie_with_resubmit,
)


class _Mailbox:
    email = "registered@example.com"

    def wait_for_code(self, timeout=180, interval=3, stop_event=None):
        return "123456"


class _MailSource:
    name = "duckmail"
    registration_mode = "email"

    def create_mailbox(self, log_callback=None):
        return _Mailbox()


class _Session:
    def open_url(self, _url):
        return None


class _RetrySession(_Session):
    def __init__(self) -> None:
        self.restart_count = 0

    def restart(self) -> None:
        self.restart_count += 1


class _OtpPage:
    url = "https://accounts.x.ai/sign-up"

    def __init__(self) -> None:
        self.calls = 0

    def run_js(self, _script, *_args):
        if "onetrust-reject-all-handler" in _script:
            return "absent"
        self.calls += 1
        return "filled" if self.calls == 1 else "clicked"


class _OtpSession:
    def __init__(self) -> None:
        self.page = _OtpPage()

    def refresh_page(self):
        return self.page


class _ProfilePage:
    def run_js(self, script, *_args):
        if "givenInput" in script and "familyInput" in script:
            return True
        if "submitButton" in script:
            return True
        return None


class _ProfileSession:
    def __init__(self) -> None:
        self.page = _ProfilePage()

    def refresh_page(self):
        return self.page


class _CookiePage:
    def __init__(self, status: str) -> None:
        self.status = status
        self.script = ""

    def run_js(self, script, *_args):
        self.script = script
        return self.status


class _OverlayPage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def run_js(self, script, *args):
        self.calls.append((script, args))
        return True


class _OverlaySession:
    def __init__(self) -> None:
        self.page = _OverlayPage()

    def refresh_page(self):
        return self.page


class _TurnstileButton:
    def __init__(self, page) -> None:
        self.page = page

    def click(self) -> None:
        self.page.clicked = True


class _TurnstileShadowRoot:
    def __init__(self, page, *, iframe=False) -> None:
        self.page = page
        self.iframe = iframe

    def ele(self, _selector):
        if self.iframe:
            return _TurnstileButton(self.page)
        return _TurnstileIframe(self.page)


class _TurnstileBody:
    def __init__(self, page) -> None:
        self.shadow_root = _TurnstileShadowRoot(page, iframe=True)


class _TurnstileIframe:
    def __init__(self, page) -> None:
        self.page = page

    def run_js(self, _script):
        raise RuntimeError("coordinate patch rejected")

    def ele(self, _selector):
        return _TurnstileBody(self.page)


class _TurnstileWrapper:
    def __init__(self, page) -> None:
        self.shadow_root = _TurnstileShadowRoot(page)


class _TurnstileSolution:
    def __init__(self, page) -> None:
        self.page = page

    def parent(self):
        return _TurnstileWrapper(self.page)


class _TurnstilePage:
    def __init__(self) -> None:
        self.clicked = False

    def run_js(self, script):
        if "getResponse" in script:
            return "turnstile-token" if self.clicked else None
        return None

    def ele(self, _selector):
        return _TurnstileSolution(self)


class GrokProviderTests(unittest.TestCase):
    def test_registration_page_failure_restarts_and_retries(self) -> None:
        provider = GrokProvider()
        provider.registration_succeeded = False
        provider._interruptible_sleep = lambda _seconds: None
        session = _RetrySession()
        expected = {"email": "retried@example.com", "credential": "sso"}

        with (
            patch.dict(
                "os.environ",
                {"GROK_ACCOUNT_MANAGER_MAX_REGISTRATION_ATTEMPTS": "2"},
            ),
            patch.object(
                provider,
                "_run_round_once",
                side_effect=[RuntimeError("page disconnected"), expected],
            ) as run_once,
        ):
            result = provider.run_round(session)

        self.assertEqual(result, expected)
        self.assertEqual(run_once.call_count, 2)
        self.assertEqual(session.restart_count, 1)

    def test_exhausted_mail_pool_is_not_retried(self) -> None:
        provider = GrokProvider()
        provider.registration_succeeded = False
        session = _RetrySession()

        with patch.object(
            provider,
            "_run_round_once",
            side_effect=RuntimeError("Outlook 邮箱池已耗尽，不会循环复用已领取账号"),
        ) as run_once:
            with self.assertRaisesRegex(RuntimeError, "已耗尽"):
                provider.run_round(session)

        run_once.assert_called_once_with(session)
        self.assertEqual(session.restart_count, 0)

    def test_cloudflare_block_is_not_retried_before_registration(self) -> None:
        provider = GrokProvider()
        stop_event = threading.Event()
        provider.stop_event = stop_event
        session = _RetrySession()

        with patch.object(
            provider,
            "_run_round_once",
            side_effect=CloudflareBlockedError("You have been blocked"),
        ) as run_once:
            with self.assertRaises(CloudflareBlockedError):
                provider.run_round(session)

        self.assertTrue(stop_event.is_set())
        run_once.assert_called_once_with(session)
        self.assertEqual(session.restart_count, 0)

    def test_otp_submission_requires_profile_page(self) -> None:
        session = _OtpSession()
        with (
            patch(
                "grok_account_manager.providers.grok._wait_for_profile_after_code",
                return_value=False,
            ),
            patch("grok_account_manager.providers.grok.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "未进入最终注册页"):
                _fill_code_and_submit(
                    session,
                    "registered@example.com",
                    _Mailbox(),
                    timeout=1,
                )

    def test_turnstile_click_survives_coordinate_patch_failure(self) -> None:
        page = _TurnstilePage()
        with patch("grok_account_manager.providers.grok.time.sleep"):
            token = _get_turnstile_token(page)

        self.assertTrue(page.clicked)
        self.assertEqual(token, "turnstile-token")

    def test_cookie_wait_resubmits_profile_once(self) -> None:
        session = _ProfileSession()
        with (
            patch(
                "grok_account_manager.providers.grok.wait_for_cookie",
                side_effect=[RuntimeError("not ready"), "browser-sso"],
            ) as wait_cookie,
            patch(
                "grok_account_manager.providers.grok._resubmit_final_profile",
                return_value=True,
            ) as resubmit,
        ):
            token = _wait_for_sso_cookie_with_resubmit(session, "sso")

        self.assertEqual(token, "browser-sso")
        self.assertEqual(wait_cookie.call_count, 2)
        self.assertEqual(wait_cookie.call_args_list[0].kwargs["timeout"], 30)
        self.assertEqual(wait_cookie.call_args_list[1].kwargs["timeout"], 150)
        resubmit.assert_called_once_with(session.page)

    def test_cookie_banner_prefers_reject_all(self) -> None:
        page = _CookiePage("dismissed")

        self.assertTrue(_dismiss_cookie_banner(page))
        self.assertIn("reject all", page.script.lower())
        self.assertIn("onetrust-reject-all-handler", page.script)

    def test_profile_resubmit_dismisses_cookie_banner_first(self) -> None:
        page = _ProfilePage()
        with patch(
            "grok_account_manager.providers.grok._dismiss_cookie_banner",
            return_value=True,
        ) as dismiss:
            self.assertTrue(_resubmit_final_profile(page))

        dismiss.assert_called_once_with(page)

    def test_browser_step_overlay_has_continue_and_retry_controls(self) -> None:
        page = _OverlayPage()

        self.assertTrue(_render_browser_step_overlay(
            page,
            stage="fill_profile",
            stage_label="填写账号资料",
            message="正在填写账号资料",
            window_label="注册窗口 1 · 第 1 轮",
        ))

        script, args = page.calls[-1]
        self.assertIn("继续执行", script)
        self.assertIn("再次运行", script)
        self.assertEqual(args, ("fill_profile", "填写账号资料", "正在填写账号资料", "注册窗口 1 · 第 1 轮"))

    def test_browser_retry_action_requests_a_new_registration_round(self) -> None:
        provider = GrokProvider()
        provider.current_stage = "fill_profile"
        page = _OverlayPage()

        with patch(
            "grok_account_manager.providers.grok._consume_browser_step_action",
            return_value="retry",
        ):
            with self.assertRaises(BrowserStepRetryRequested):
                provider._handle_browser_overlay_action(page)

    def test_stage_update_renders_overlay_for_its_browser(self) -> None:
        provider = GrokProvider()
        session = _OverlaySession()
        provider._browser_session = session
        provider.browser_window_label = "注册窗口 2 · 第 4 轮"

        provider._set_stage("fill_email", "正在填写邮箱并发送验证码")

        script, args = session.page.calls[-1]
        self.assertIn("__grok-registration-step-overlay", script)
        self.assertEqual(args, ("fill_email", "填写邮箱", "正在填写邮箱并发送验证码", "注册窗口 2 · 第 4 轮"))

    def test_oauth_failure_keeps_registered_account(self) -> None:
        provider = GrokProvider()
        provider.enable_oauth_exchange = True
        provider.mail_source = _MailSource()
        provider._interruptible_sleep = lambda _seconds: None
        checkpoints = []
        provider.result_callback = checkpoints.append

        with (
            patch("grok_account_manager.providers.grok._click_email_signup_button"),
            patch(
                "grok_account_manager.providers.grok._fill_email_and_submit",
                return_value="registered@example.com",
            ),
            patch("grok_account_manager.providers.grok._fill_code_and_submit"),
            patch(
                "grok_account_manager.providers.grok._fill_profile_and_submit",
                return_value={"first_name": "Test", "last_name": "User", "password": "secret"},
            ),
            patch("grok_account_manager.providers.grok.wait_for_cookie", return_value="browser-sso"),
            patch(
                "grok_account_manager.providers.grok.exchange_sso_for_oauth_tokens",
                side_effect=RuntimeError("rate limited"),
            ) as exchange,
            patch(
                "grok_account_manager.providers.grok.fetch_complete_credential",
                return_value={
                    "id": "account-id",
                    "email": "registered@example.com",
                    "access_token": "browser-sso",
                },
            ),
        ):
            result = provider.run_round(_Session())

        self.assertTrue(provider.registration_succeeded)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["oauth_status"], "pending")
        self.assertEqual(exchange.call_count, 2)
        self.assertEqual(result["oauth_status"], "failed")
        self.assertIn("rate limited", result["oauth_error"])
        self.assertEqual(result["full_credential"]["sso_token"], "browser-sso")
        self.assertEqual(result["full_credential"]["oauth_exchange_status"], "failed")

    def test_oauth_and_enrichment_share_one_round_http_session(self) -> None:
        provider = GrokProvider()
        provider.enable_oauth_exchange = True
        provider.mail_source = _MailSource()
        http_session = Mock()

        with (
            patch("grok_account_manager.providers.grok._click_email_signup_button"),
            patch(
                "grok_account_manager.providers.grok._fill_email_and_submit",
                return_value="registered@example.com",
            ),
            patch("grok_account_manager.providers.grok._fill_code_and_submit"),
            patch(
                "grok_account_manager.providers.grok._fill_profile_and_submit",
                return_value={"first_name": "Test", "last_name": "User", "password": "secret"},
            ),
            patch("grok_account_manager.providers.grok.wait_for_cookie", return_value="browser-sso"),
            patch(
                "grok_account_manager.providers.grok.build_browser_http_session",
                return_value=http_session,
            ) as build_session,
            patch(
                "grok_account_manager.providers.grok.exchange_sso_for_oauth_tokens",
                return_value={"access_token": "access-token", "refresh_token": "refresh-token"},
            ) as exchange,
            patch(
                "grok_account_manager.providers.grok.fetch_complete_credential",
                return_value={
                    "id": "account-id",
                    "email": "registered@example.com",
                    "access_token": "access-token",
                },
            ) as fetch,
        ):
            provider.run_round(_Session())

        build_session.assert_called_once()
        self.assertIs(exchange.call_args.kwargs["http_session"], http_session)
        self.assertIs(fetch.call_args.kwargs["http_session"], http_session)
        http_session.close.assert_called_once_with()

    def test_terminal_oauth_error_is_not_retried(self) -> None:
        provider = GrokProvider()
        provider.enable_oauth_exchange = True
        provider.mail_source = _MailSource()
        provider._interruptible_sleep = lambda _seconds: None

        with (
            patch("grok_account_manager.providers.grok._click_email_signup_button"),
            patch(
                "grok_account_manager.providers.grok._fill_email_and_submit",
                return_value="registered@example.com",
            ),
            patch("grok_account_manager.providers.grok._fill_code_and_submit"),
            patch(
                "grok_account_manager.providers.grok._fill_profile_and_submit",
                return_value={"first_name": "Test", "last_name": "User", "password": "secret"},
            ),
            patch("grok_account_manager.providers.grok.wait_for_cookie", return_value="browser-sso"),
            patch(
                "grok_account_manager.providers.grok.exchange_sso_for_oauth_tokens",
                side_effect=OAuthTerminalError("access_denied"),
            ) as exchange,
            patch(
                "grok_account_manager.providers.grok.fetch_complete_credential",
                return_value={
                    "id": "account-id",
                    "email": "registered@example.com",
                    "access_token": "browser-sso",
                },
            ),
        ):
            result = provider.run_round(_Session())

        self.assertEqual(exchange.call_count, 1)
        self.assertEqual(result["oauth_status"], "failed")
        self.assertEqual(result["oauth_error"], "access_denied")

    def test_cloudflare_block_stops_oauth_task(self) -> None:
        provider = GrokProvider()
        provider.enable_oauth_exchange = True
        provider.mail_source = _MailSource()
        provider._interruptible_sleep = lambda _seconds: None
        stop_event = threading.Event()
        provider.stop_event = stop_event

        with (
            patch("grok_account_manager.providers.grok._click_email_signup_button"),
            patch(
                "grok_account_manager.providers.grok._fill_email_and_submit",
                return_value="registered@example.com",
            ),
            patch("grok_account_manager.providers.grok._fill_code_and_submit"),
            patch(
                "grok_account_manager.providers.grok._fill_profile_and_submit",
                return_value={"first_name": "Test", "last_name": "User", "password": "secret"},
            ),
            patch("grok_account_manager.providers.grok.wait_for_cookie", return_value="browser-sso"),
            patch(
                "grok_account_manager.providers.grok.exchange_sso_for_oauth_tokens",
                side_effect=CloudflareBlockedError("You have been blocked"),
            ) as exchange,
        ):
            with self.assertRaises(CloudflareBlockedError):
                provider.run_round(_Session())

        self.assertTrue(stop_event.is_set())
        self.assertEqual(exchange.call_count, 1)


if __name__ == "__main__":
    unittest.main()
