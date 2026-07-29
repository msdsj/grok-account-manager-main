import unittest
from unittest.mock import Mock, patch

from grok_account_manager.grok.oauth_exchange import (
    MAX_CONSECUTIVE_PAGE_ERRORS,
    OAuthTerminalError,
    _drive_device_authorization_and_poll,
    _poll_device_token_once,
)


class _Session:
    def __init__(self, page) -> None:
        self.page = page

    def refresh_page(self):
        return self.page


class OAuthExchangeTests(unittest.TestCase):
    def _drive(self, page=object()):
        return _drive_device_authorization_and_poll(
            session=_Session(page),
            oauth_page=page,
            device_code="device-code",
            user_code="user-code",
            token_endpoint="https://auth.x.ai/oauth2/token",
            interval=5,
            expires_in=30,
        )

    def test_fatal_poll_response_raises_terminal_error(self) -> None:
        with patch(
            "grok_account_manager.grok.oauth_exchange._poll_device_token_once",
            return_value=("fatal", "Grok OAuth 授权已被拒绝"),
        ):
            with self.assertRaises(OAuthTerminalError):
                self._drive()

    def test_rate_limit_and_server_errors_are_retryable(self) -> None:
        for status_code in (429, 503):
            with self.subTest(status_code=status_code):
                response = Mock()
                response.status_code = status_code
                response.json.return_value = {"error": "temporarily_unavailable"}
                with patch(
                    "grok_account_manager.grok.oauth_exchange.requests.post",
                    return_value=response,
                ):
                    status, payload = _poll_device_token_once(
                        "device-code",
                        "https://auth.x.ai/oauth2/token",
                    )

                self.assertEqual(status, "transport_error")
                self.assertIn(str(status_code), str(payload))

    def test_expired_device_code_can_start_a_fresh_oauth_attempt(self) -> None:
        response = Mock()
        response.status_code = 400
        response.json.return_value = {"error": "expired_token"}
        with patch(
            "grok_account_manager.grok.oauth_exchange.requests.post",
            return_value=response,
        ):
            status, payload = _poll_device_token_once(
                "device-code",
                "https://auth.x.ai/oauth2/token",
            )

        self.assertEqual(status, "expired")
        self.assertIn("过期", str(payload))

    def test_repeated_page_driver_errors_stop_early(self) -> None:
        with (
            patch(
                "grok_account_manager.grok.oauth_exchange._poll_device_token_once",
                return_value=("pending", None),
            ),
            patch(
                "grok_account_manager.grok.oauth_exchange._drive_device_authorization_page",
                side_effect=RuntimeError("page disconnected"),
            ) as drive_page,
            patch("grok_account_manager.grok.oauth_exchange.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "连续驱动失败"):
                self._drive()

        self.assertEqual(drive_page.call_count, MAX_CONSECUTIVE_PAGE_ERRORS)


if __name__ == "__main__":
    unittest.main()
