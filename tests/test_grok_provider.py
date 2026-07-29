import unittest
from unittest.mock import patch

from grok_account_manager.grok.oauth_exchange import OAuthTerminalError
from grok_account_manager.providers.grok import (
    GrokProvider,
    _fill_code_and_submit,
    _get_turnstile_token,
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


if __name__ == "__main__":
    unittest.main()
