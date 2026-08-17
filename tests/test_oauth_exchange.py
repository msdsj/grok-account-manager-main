import unittest
from unittest.mock import Mock, patch
from requests.cookies import RequestsCookieJar

from grok_account_manager.grok.oauth_exchange import (
    CloudflareBlockedError,
    MAX_CONSECUTIVE_PAGE_ERRORS,
    OAuthTerminalError,
    _discover_oauth_endpoints,
    _drive_device_authorization_and_poll,
    _drive_device_authorization_page,
    _poll_device_token_once,
    _request_device_code,
    _try_direct_device_authorization,
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

    def test_supplied_session_routes_discovery_device_and_poll(self) -> None:
        discovery_response = Mock()
        discovery_response.json.return_value = {
            "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
            "token_endpoint": "https://auth.x.ai/oauth2/token",
        }
        token_response = Mock()
        token_response.status_code = 400
        token_response.json.return_value = {"error": "authorization_pending"}
        device_response = Mock()
        device_response.json.return_value = {
            "device_code": "device-code",
            "user_code": "user-code",
        }
        http_session = Mock()
        http_session.get.return_value = discovery_response
        http_session.post.side_effect = [device_response, token_response]
        endpoints = _discover_oauth_endpoints(http_session=http_session)
        device_data = _request_device_code(
            endpoints["device_authorization_endpoint"],
            http_session=http_session,
        )
        status, payload = _poll_device_token_once(
            device_data["device_code"],
            endpoints["token_endpoint"],
            http_session=http_session,
        )

        http_session.get.assert_called_once()
        self.assertEqual(http_session.post.call_count, 2)
        self.assertEqual(status, "pending")
        self.assertIsNone(payload)

    def test_direct_device_authorization_uses_sso_cookie_and_allow_action(self) -> None:
        verify_response = Mock(status_code=303)
        verify_response.headers = {"Location": "https://accounts.x.ai/oauth2/device/consent"}
        approve_response = Mock(status_code=303)
        approve_response.headers = {"Location": "https://accounts.x.ai/oauth2/device/done"}
        http_session = Mock()
        http_session.cookies = RequestsCookieJar()
        http_session.post.side_effect = [verify_response, approve_response]

        authorized, description = _try_direct_device_authorization(
            http_session=http_session,
            user_code="ABCD-EFGH",
            sso_token="browser-sso",
        )

        self.assertTrue(authorized)
        self.assertIn("verify/approve", description)
        self.assertEqual(http_session.cookies.get_dict()["sso"], "browser-sso")
        self.assertEqual(http_session.cookies.get_dict()["sso-rw"], "browser-sso")
        self.assertEqual(http_session.post.call_count, 2)
        verify_call, approve_call = http_session.post.call_args_list
        self.assertEqual(verify_call.kwargs["data"], {"user_code": "ABCD-EFGH"})
        self.assertEqual(approve_call.kwargs["data"]["action"], "allow")
        self.assertFalse(approve_call.kwargs["allow_redirects"])

    def test_direct_device_authorization_falls_back_when_verify_is_not_consent(self) -> None:
        verify_response = Mock(status_code=200)
        verify_response.headers = {"Location": "https://accounts.x.ai/sign-in"}
        http_session = Mock()
        http_session.cookies = RequestsCookieJar()
        http_session.post.return_value = verify_response

        authorized, description = _try_direct_device_authorization(
            http_session=http_session,
            user_code="ABCD-EFGH",
            sso_token="browser-sso",
        )

        self.assertFalse(authorized)
        self.assertIn("重新登录", description)
        http_session.post.assert_called_once()

    def test_direct_authorization_polls_without_touching_browser(self) -> None:
        with patch(
            "grok_account_manager.grok.oauth_exchange._poll_device_token_once",
            return_value=("complete", {"access_token": "access", "refresh_token": "refresh"}),
        ):
            result = _drive_device_authorization_and_poll(
                session=Mock(),
                oauth_page=None,
                device_code="device-code",
                user_code="user-code",
                token_endpoint="https://auth.x.ai/oauth2/token",
                interval=5,
                expires_in=30,
                drive_browser=False,
            )

        self.assertEqual(result["refresh_token"], "refresh")

    def test_session_discovery_is_not_shared_between_rounds(self) -> None:
        def session_with_token_endpoint(endpoint: str):
            response = Mock()
            response.json.return_value = {
                "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
                "token_endpoint": endpoint,
            }
            http_session = Mock()
            http_session.get.return_value = response
            return http_session

        first_session = session_with_token_endpoint("https://auth.x.ai/oauth2/token")
        second_session = session_with_token_endpoint("https://login.x.ai/oauth2/token")

        first_result = _discover_oauth_endpoints(http_session=first_session)
        second_result = _discover_oauth_endpoints(http_session=second_session)

        self.assertEqual(first_result["token_endpoint"], "https://auth.x.ai/oauth2/token")
        self.assertEqual(second_result["token_endpoint"], "https://login.x.ai/oauth2/token")
        first_session.get.assert_called_once()
        second_session.get.assert_called_once()

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

    def test_cloudflare_block_is_terminal(self) -> None:
        with (
            patch(
                "grok_account_manager.grok.oauth_exchange._poll_device_token_once",
                return_value=("pending", None),
            ),
            patch(
                "grok_account_manager.grok.oauth_exchange._drive_device_authorization_page",
                return_value="blocked",
            ),
        ):
            with self.assertRaises(CloudflareBlockedError):
                self._drive()

    def test_login_preference_is_forwarded_to_page_driver(self) -> None:
        page = Mock()
        page.run_js.return_value = "idle"
        _drive_device_authorization_page(
            page,
            email="outlook@example.com",
            password="secret",
            recovery_email=None,
            user_code="ABCD",
            email_code=None,
            prefer_google_login=False,
        )
        self.assertIs(page.run_js.call_args.args[-1], False)

        with (
            patch(
                "grok_account_manager.grok.oauth_exchange._poll_device_token_once",
                return_value=("pending", None),
            ),
            patch(
                "grok_account_manager.grok.oauth_exchange._drive_device_authorization_page",
                return_value="device-invalid-action",
            ) as drive_page,
        ):
            with self.assertRaisesRegex(RuntimeError, "Invalid action"):
                _drive_device_authorization_and_poll(
                    session=_Session(page),
                    oauth_page=page,
                    device_code="device-code",
                    user_code="user-code",
                    token_endpoint="https://auth.x.ai/oauth2/token",
                    interval=5,
                    expires_in=30,
                    prefer_google_login=True,
                )

        self.assertTrue(drive_page.call_args.kwargs["prefer_google_login"])


if __name__ == "__main__":
    unittest.main()
