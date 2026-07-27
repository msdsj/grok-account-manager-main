import unittest
from unittest.mock import patch

from grok_account_manager.providers.grok import GrokProvider


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


class GrokProviderTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
