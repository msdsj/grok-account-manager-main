from __future__ import annotations

import os
import threading
import unittest
from unittest.mock import patch

import requests

from grok_account_manager.mail.cloud_mail import (
    CloudMailRequestError,
    CloudMailSource,
    _message_matches_recipient,
    parse_cloud_mail_domains,
)


class _Response:
    def __init__(self, status_code: int, payload: object, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _NonJsonResponse(_Response):
    def json(self):
        raise ValueError("not json")


class _Session:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []
        self.closed = False
        self.verify = True

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected Cloud Mail request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


class CloudMailTests(unittest.TestCase):
    def test_domain_parser_accepts_newlines_commas_and_deduplicates(self) -> None:
        self.assertEqual(
            parse_cloud_mail_domains("@a.example\nb.example, A.EXAMPLE"),
            ("a.example", "b.example"),
        )

    def test_recipient_filter_requires_an_exact_address(self) -> None:
        self.assertTrue(
            _message_matches_recipient(
                {"toEmail": "User <user@example.com>"},
                "user@example.com",
            )
        )
        self.assertFalse(
            _message_matches_recipient(
                {"toEmail": "otheruser@example.com"},
                "user@example.com",
            )
        )

    def test_public_token_creates_mailbox_and_filters_message_recipient(self) -> None:
        create_session = _Session([
            _Response(200, {"code": 200, "message": "success", "data": None}),
        ])
        poll_session = _Session([
            _Response(
                200,
                {
                    "code": 200,
                    "message": "success",
                    "data": [
                        {
                            "emailId": 10,
                            "toEmail": "fixed@example.com",
                            "sendEmail": "no-reply@x.ai",
                            "subject": "Old verification code",
                            "text": "Verification code: 111111",
                        },
                        {
                            "emailId": 99,
                            "toEmail": "another@example.com",
                            "sendEmail": "no-reply@x.ai",
                            "subject": "Verification code",
                            "text": "Verification code: 999999",
                        },
                        {
                            "emailId": 12,
                            "toEmail": "fixed@example.com",
                            "sendEmail": "no-reply@x.ai",
                            "subject": "Your xAI verification code",
                            "text": "Verification code: 654321",
                        },
                    ],
                },
            ),
        ])
        source = CloudMailSource(
            api_base="https://mail.example.com/api",
            public_token="public-token",
            domains="example.com",
        )

        with (
            patch.object(source, "_new_session", side_effect=[create_session, poll_session]),
            patch(
                "grok_account_manager.mail.cloud_mail._random_text",
                side_effect=["fixed", "mailbox-password"],
            ),
        ):
            mailbox = source.create_mailbox()
            mailbox._last_email_id = 11
            code = mailbox.wait_for_code(timeout=1, interval=0)

        self.assertEqual(mailbox.email, "fixed@example.com")
        self.assertEqual(code, "654321")
        self.assertTrue(create_session.closed)
        self.assertTrue(poll_session.closed)
        self.assertTrue(create_session.verify)
        self.assertTrue(poll_session.verify)
        create_call = create_session.calls[0][2]
        self.assertEqual(create_call["headers"]["Authorization"], "public-token")
        self.assertNotIn("verify", create_call)
        self.assertEqual(
            create_call["json"],
            {"list": [{"email": "fixed@example.com", "password": "mailbox-password"}]},
        )
        poll_call = poll_session.calls[0][2]
        self.assertEqual(poll_call["headers"]["Authorization"], "public-token")
        self.assertEqual(poll_call["json"]["toEmail"], "fixed@example.com")

    def test_login_mode_reauthenticates_once_after_business_401(self) -> None:
        create_session = _Session([
            _Response(200, {"code": 200, "data": {"token": "old-token"}}),
            _Response(200, {"code": 200, "data": {"accountId": 7}}),
        ])
        poll_session = _Session([
            _Response(200, {"code": 401, "message": "expired", "data": None}),
            _Response(200, {"code": 200, "data": {"token": "new-token"}}),
            _Response(
                200,
                {
                    "code": 200,
                    "data": [
                        {
                            "emailId": 3,
                            "recipient": {"email": "fixed@example.com"},
                            "sendEmail": "no-reply@x.ai",
                            "subject": "Security code",
                            "code": "ABC-123",
                        },
                    ],
                },
            ),
        ])
        source = CloudMailSource(
            api_base="https://mail.example.com",
            login_email="admin@example.com",
            login_password="secret",
            domains="example.com",
        )

        with (
            patch.object(source, "_new_session", side_effect=[create_session, poll_session]),
            patch(
                "grok_account_manager.mail.cloud_mail._random_text",
                side_effect=["fixed", "mailbox-password"],
            ),
        ):
            mailbox = source.create_mailbox()
            code = mailbox.wait_for_code(timeout=1, interval=0)

        self.assertEqual(code, "ABC123")
        self.assertEqual(poll_session.calls[0][2]["headers"]["Authorization"], "old-token")
        self.assertNotIn("Authorization", poll_session.calls[1][2]["headers"])
        self.assertEqual(poll_session.calls[2][2]["headers"]["Authorization"], "new-token")
        self.assertEqual(poll_session.calls[0][2]["params"]["emailId"], 0)
        self.assertTrue(create_session.closed)
        self.assertTrue(poll_session.closed)

    def test_login_mode_creates_mailbox_and_reads_code_without_reauth(self) -> None:
        create_session = _Session([
            _Response(200, {"code": 200, "data": {"token": "login-token"}}),
            _Response(200, {"code": 200, "data": {"accountId": 7}}),
        ])
        poll_session = _Session([
            _Response(
                200,
                {
                    "code": 200,
                    "data": [
                        {
                            "emailId": 1,
                            "toEmail": "fixed@example.com",
                            "sendEmail": "no-reply@x.ai",
                            "subject": "Your verification code",
                            "text": "Verification code: 321654",
                        },
                    ],
                },
            ),
        ])
        source = CloudMailSource(
            api_base="https://mail.example.com",
            login_email="admin@example.com",
            login_password="secret",
            domains="example.com",
        )

        with (
            patch.object(source, "_new_session", side_effect=[create_session, poll_session]),
            patch(
                "grok_account_manager.mail.cloud_mail._random_text",
                side_effect=["fixed", "mailbox-password"],
            ),
        ):
            mailbox = source.create_mailbox()
            code = mailbox.wait_for_code(timeout=1, interval=0)

        self.assertEqual(code, "321654")
        self.assertEqual(create_session.calls[1][2]["headers"]["Authorization"], "login-token")
        self.assertEqual(poll_session.calls[0][2]["headers"]["Authorization"], "login-token")
        self.assertEqual(poll_session.calls[0][2]["params"]["accountId"], 7)

    def test_login_mode_reauthenticates_once_after_http_401(self) -> None:
        source = CloudMailSource(
            api_base="https://mail.example.com",
            login_email="admin@example.com",
            login_password="secret",
            domains="example.com",
        )
        source._login_token = "old-token"
        session = _Session([
            _Response(401, {"code": 401, "message": "expired", "data": None}),
            _Response(200, {"code": 200, "data": {"token": "new-token"}}),
            _Response(200, {"code": 200, "data": {"accountId": 7}}),
        ])

        data, token = source._user_request(
            session,
            "POST",
            "/api/account/add",
            payload={"email": "fixed@example.com", "token": ""},
        )

        self.assertEqual(data["accountId"], 7)
        self.assertEqual(token, "new-token")
        self.assertEqual(session.calls[0][2]["headers"]["Authorization"], "old-token")
        self.assertNotIn("Authorization", session.calls[1][2]["headers"])
        self.assertEqual(session.calls[2][2]["headers"]["Authorization"], "new-token")

    def test_http_200_with_non_200_business_code_is_an_error(self) -> None:
        source = CloudMailSource(
            api_base="https://mail.example.com",
            public_token="token",
            domains="example.com",
        )
        session = _Session([
            _Response(200, {"code": 500, "message": "failed", "data": None}),
        ])

        with self.assertRaises(CloudMailRequestError) as raised:
            source._request(session, "GET", "/api/test", token="raw-token")

        self.assertFalse(raised.exception.unauthorized)
        self.assertEqual(session.calls[0][2]["headers"]["Authorization"], "raw-token")

    def test_error_detail_redacts_configured_secrets(self) -> None:
        source = CloudMailSource(
            api_base="https://mail.example.com",
            public_token="public-secret",
            login_email="admin@example.com",
            login_password="password-secret",
            domains="example.com",
        )
        session = _Session([
            _Response(
                500,
                {"code": 500, "message": "public-secret password-secret"},
            ),
        ])

        with self.assertRaises(CloudMailRequestError) as raised:
            source._request(session, "GET", "/api/test", token="public-secret")

        message = str(raised.exception)
        self.assertNotIn("public-secret", message)
        self.assertNotIn("password-secret", message)
        self.assertIn("[REDACTED]", message)

    def test_create_error_redacts_generated_mailbox_password(self) -> None:
        session = _Session([
            _Response(
                500,
                {"code": 500, "message": "mailbox-password already exists"},
            ),
        ])
        source = CloudMailSource(
            api_base="https://mail.example.com",
            public_token="public-token",
            domains="example.com",
        )

        with (
            patch.object(source, "_new_session", return_value=session),
            patch(
                "grok_account_manager.mail.cloud_mail._random_text",
                side_effect=["fixed", "mailbox-password"],
            ),
            self.assertRaises(CloudMailRequestError) as raised,
        ):
            source.create_mailbox()

        self.assertNotIn("mailbox-password", str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))
        self.assertTrue(session.closed)

    def test_poll_retries_transient_network_error(self) -> None:
        create_session = _Session([
            _Response(200, {"code": 200, "data": None}),
        ])
        poll_session = _Session([
            requests.Timeout("temporary timeout"),
            _Response(
                200,
                {
                    "code": 200,
                    "data": [
                        {
                            "emailId": 1,
                            "toEmail": "fixed@example.com",
                            "sendEmail": "no-reply@x.ai",
                            "subject": "Verification code",
                            "text": "Verification code: 741852",
                        },
                    ],
                },
            ),
        ])
        source = CloudMailSource(
            api_base="https://mail.example.com",
            public_token="public-token",
            domains="example.com",
        )

        with (
            patch.object(source, "_new_session", side_effect=[create_session, poll_session]),
            patch(
                "grok_account_manager.mail.cloud_mail._random_text",
                side_effect=["fixed", "mailbox-password"],
            ),
        ):
            mailbox = source.create_mailbox()
            code = mailbox.wait_for_code(timeout=1, interval=0)

        self.assertEqual(code, "741852")
        self.assertEqual(len(poll_session.calls), 2)
        self.assertTrue(poll_session.closed)

    def test_session_uses_configured_fixed_egress_proxy(self) -> None:
        source = CloudMailSource(
            api_base="https://mail.example.com",
            public_token="public-token",
            domains="example.com",
        )

        with patch.dict(
            os.environ,
            {"GROK_ACCOUNT_MANAGER_EGRESS_PROXY": "198.51.100.10:8080"},
        ):
            session = source._new_session()

        try:
            self.assertFalse(session.trust_env)
            self.assertEqual(
                session.proxies,
                {
                    "http": "http://198.51.100.10:8080",
                    "https": "http://198.51.100.10:8080",
                },
            )
            self.assertTrue(session.verify)
        finally:
            session.close()

    def test_http_200_with_non_json_envelope_is_an_error(self) -> None:
        source = CloudMailSource(
            api_base="https://mail.example.com",
            public_token="token",
            domains="example.com",
        )
        session = _Session([
            _NonJsonResponse(200, None, text="upstream html"),
        ])

        with self.assertRaisesRegex(CloudMailRequestError, "code=None"):
            source._request(session, "GET", "/api/test", token="raw-token")

    def test_pre_set_stop_event_interrupts_and_closes_session(self) -> None:
        create_session = _Session([
            _Response(200, {"code": 200, "data": None}),
        ])
        poll_session = _Session([])
        source = CloudMailSource(
            api_base="https://mail.example.com",
            public_token="token",
            domains="example.com",
        )

        with (
            patch.object(source, "_new_session", side_effect=[create_session, poll_session]),
            patch(
                "grok_account_manager.mail.cloud_mail._random_text",
                side_effect=["fixed", "mailbox-password"],
            ),
        ):
            mailbox = source.create_mailbox()
            stopped = threading.Event()
            stopped.set()
            with self.assertRaisesRegex(RuntimeError, "任务已停止"):
                mailbox.wait_for_code(timeout=10, interval=3, stop_event=stopped)

        self.assertTrue(poll_session.closed)
        self.assertEqual(poll_session.calls, [])


if __name__ == "__main__":
    unittest.main()
