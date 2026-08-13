import unittest
from unittest.mock import Mock, patch

from grok_account_manager.core.network import build_browser_http_session
from grok_account_manager.grok.client import fetch_complete_credential


class GrokClientNetworkTests(unittest.TestCase):
    def test_credential_fetch_reuses_context_session_and_browser_ua(self) -> None:
        billing_response = Mock()
        billing_response.json.return_value = {"config": {}}
        user_response = Mock()
        user_response.json.return_value = {}
        subscription_response = Mock()
        subscription_response.json.return_value = {}
        usage_response = Mock()
        usage_response.json.return_value = {}

        http_session = Mock()
        http_session.get.side_effect = [
            billing_response,
            user_response,
            subscription_response,
            usage_response,
        ]
        http_session.headers = {
            "User-Agent": "Mozilla/5.0 Test Chrome/146.0.0.0",
        }
        http_session._grok_browser_user_agent = "Mozilla/5.0 Test Chrome/146.0.0.0"

        with patch("grok_account_manager.grok.client.requests.get") as legacy_get:
            fetch_complete_credential(
                email="registered@example.com",
                sso_token="access-token",
                oauth_tokens={"access_token": "access-token"},
                http_session=http_session,
            )

        self.assertEqual(http_session.get.call_count, 4)
        legacy_get.assert_not_called()
        for call in http_session.get.call_args_list:
            self.assertEqual(
                call.kwargs["headers"]["User-Agent"],
                "Mozilla/5.0 Test Chrome/146.0.0.0",
            )

    def test_credential_fetch_without_context_keeps_legacy_requests_client(self) -> None:
        response = Mock()
        response.json.return_value = {}
        with patch(
            "grok_account_manager.grok.client.requests.get",
            return_value=response,
        ) as legacy_get:
            fetch_complete_credential(
                email="registered@example.com",
                sso_token="browser-sso",
            )

        self.assertGreaterEqual(legacy_get.call_count, 1)
        self.assertEqual(
            legacy_get.call_args_list[0].kwargs["headers"]["User-Agent"],
            "grok-cli/0.2.93",
        )

    def test_round_session_without_browser_ua_uses_cli_fallback(self) -> None:
        response = Mock()
        response.json.return_value = {}
        http_session = build_browser_http_session(object())
        try:
            with patch.object(http_session, "get", return_value=response) as get:
                fetch_complete_credential(
                    email="registered@example.com",
                    sso_token="browser-sso",
                    http_session=http_session,
                )

            self.assertEqual(
                get.call_args_list[0].kwargs["headers"]["User-Agent"],
                "grok-cli/0.2.93",
            )
        finally:
            http_session.close()


if __name__ == "__main__":
    unittest.main()
