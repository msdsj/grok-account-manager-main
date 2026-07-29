import unittest
from unittest.mock import Mock, patch

from grok_account_manager.mail import duckmail, sources
from grok_account_manager.mail.sources import (
    GoogleAccount,
    GoogleAccountPool,
    OutlookAccount,
    OutlookAccountPool,
    parse_google_accounts,
    parse_outlook_accounts,
)


class MailSourceParsingTests(unittest.TestCase):
    def test_google_accounts_accept_dash_format(self) -> None:
        accounts = parse_google_accounts("user@gmail.com----password----recovery@gmail.com")

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].email, "user@gmail.com")
        self.assertEqual(accounts[0].password, "password")
        self.assertEqual(accounts[0].recovery_email, "recovery@gmail.com")

    def test_google_accounts_accept_pipe_format(self) -> None:
        accounts = parse_google_accounts("user@gmail.com|password|recovery@gmail.com")

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].email, "user@gmail.com")
        self.assertEqual(accounts[0].password, "password")
        self.assertEqual(accounts[0].recovery_email, "recovery@gmail.com")

    def test_gmail_accounts_accept_pipe_without_recovery_email(self) -> None:
        accounts = parse_google_accounts("user@gmail.com|app-password")

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].email, "user@gmail.com")
        self.assertEqual(accounts[0].password, "app-password")
        self.assertEqual(accounts[0].recovery_email, "")

    def test_outlook_accounts_accept_pipe_format(self) -> None:
        accounts = parse_outlook_accounts("user@outlook.com|password|client-id|refresh-token")

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].email, "user@outlook.com")
        self.assertEqual(accounts[0].password, "password")
        self.assertEqual(accounts[0].client_id, "client-id")
        self.assertEqual(accounts[0].refresh_token, "refresh-token")
        self.assertEqual(accounts[0].mode, "auto")

    def test_outlook_accounts_accept_graph_mode(self) -> None:
        accounts = parse_outlook_accounts("user@outlook.com----password----client-id----refresh-token----graph")

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].mode, "graph")

    def test_outlook_accounts_accept_pipe_mode(self) -> None:
        accounts = parse_outlook_accounts("user@outlook.com|password|client-id|refresh-token|imap")

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].mode, "imap")

    def test_outlook_graph_scan_ignores_unchanged_inbox_count(self) -> None:
        with (
            patch.object(sources, "_get_outlook_graph_inbox_count_with_token", return_value=10),
            patch.object(sources, "_outlook_graph_get") as graph_get,
        ):
            code = sources._scan_outlook_graph_once("access-token", {"INBOX": 10})

        self.assertIsNone(code)
        graph_get.assert_not_called()

    def test_outlook_graph_scan_reads_new_messages(self) -> None:
        graph_payload = {
            "value": [
                {
                    "subject": "Your verification code is 123456",
                    "bodyPreview": "",
                    "body": {"content": ""},
                }
            ]
        }
        log_messages = []
        with (
            patch.object(sources, "_get_outlook_graph_inbox_count_with_token", return_value=12),
            patch.object(sources, "_outlook_graph_get", return_value=graph_payload) as graph_get,
        ):
            counts = {"INBOX": 10}
            code = sources._scan_outlook_graph_once(
                "access-token",
                counts,
                log_callback=lambda _level, message, **_extra: log_messages.append(message),
            )

        self.assertEqual(code, "123456")
        self.assertEqual(counts[sources.OUTLOOK_GRAPH_INBOX_KEY], 12)
        self.assertEqual(graph_get.call_args.kwargs["params"]["$top"], "2")
        self.assertFalse(any("123456" in message for message in log_messages))

    def test_outlook_pool_does_not_reuse_an_exhausted_account(self) -> None:
        account = OutlookAccount("user@outlook.com", "password", "client-id", "refresh-token")
        pool = OutlookAccountPool([account])

        with patch.object(sources.OutlookMailbox, "prepare"):
            first = pool.create_mailbox()
            with self.assertRaisesRegex(RuntimeError, "已耗尽"):
                pool.create_mailbox()

        self.assertEqual(first.email, account.email)

    def test_google_pool_does_not_reuse_an_exhausted_account(self) -> None:
        account = GoogleAccount("user@gmail.com", "password")
        for source_name in ("gmail", "google"):
            with self.subTest(source_name=source_name):
                pool = GoogleAccountPool([account], name=source_name)
                with patch.object(sources.GoogleMailbox, "prepare"):
                    first = pool.create_mailbox()
                    with self.assertRaisesRegex(RuntimeError, "已耗尽"):
                        pool.create_mailbox()
                self.assertEqual(first.email, account.email)

    def test_outlook_wait_advances_the_mailbox_counts_in_place(self) -> None:
        account = OutlookAccount(
            "user@outlook.com",
            "password",
            "client-id",
            "refresh-token",
            mode="imap",
        )
        counts = {"INBOX": 10}

        def scan_once(_account, _token, scan_counts, _log_callback):
            self.assertIs(scan_counts, counts)
            scan_counts["INBOX"] = 11
            return "ABC123"

        with (
            patch.object(sources, "refresh_outlook_token", return_value="access-token"),
            patch.object(sources, "_scan_outlook_imap_once", side_effect=scan_once),
        ):
            code = sources.wait_for_outlook_code(account, counts, timeout=1, interval=0)

        self.assertEqual(code, "ABC123")
        self.assertEqual(counts["INBOX"], 11)

    def test_gmail_unchanged_count_does_not_scan_old_messages(self) -> None:
        account = GoogleAccount("user@gmail.com", "app-password")
        counts = {"INBOX": 10}
        client = Mock()

        with (
            patch.object(sources, "_connect_google_imap", return_value=client),
            patch.object(sources, "_discover_google_folders", return_value=["INBOX"]),
            patch.object(sources, "_select_outlook_folder_count", return_value=10),
            patch.object(sources, "_fetch_message_content") as fetch_message,
        ):
            code = sources._scan_google_imap_once(account, counts)

        self.assertIsNone(code)
        self.assertEqual(counts, {"INBOX": 10})
        fetch_message.assert_not_called()
        client.logout.assert_called_once_with()

    def test_gmail_scan_advances_counts_without_logging_the_code(self) -> None:
        account = GoogleAccount("user@gmail.com", "app-password")
        counts = {"INBOX": 10}
        client = Mock()

        with (
            patch.object(sources, "_connect_google_imap", return_value=client),
            patch.object(sources, "_discover_google_folders", return_value=["INBOX"]),
            patch.object(sources, "_select_outlook_folder_count", return_value=11),
            patch.object(
                sources,
                "_fetch_message_content",
                return_value=("Your verification code is 123456", "", "", "no-reply@x.ai"),
            ),
            patch("builtins.print") as print_mock,
        ):
            code = sources._scan_google_imap_once(account, counts)

        self.assertEqual(code, "123456")
        self.assertEqual(counts["INBOX"], 11)
        printed = " ".join(str(call) for call in print_mock.call_args_list)
        self.assertNotIn("123456", printed)

    def test_duckmail_mailbox_forwards_timeout_and_interval(self) -> None:
        mailbox = sources.DuckMailMailbox("mail@example.com", "mail-token")

        with patch.object(sources, "get_oai_code", return_value="123456") as get_code:
            code = mailbox.wait_for_code(timeout=12, interval=4)

        self.assertEqual(code, "123456")
        get_code.assert_called_once_with(
            "mail-token",
            "mail@example.com",
            stop_event=None,
            timeout=12,
            interval=4,
        )


class DuckMailReliabilityTests(unittest.TestCase):
    @staticmethod
    def _response(status_code: int, payload: dict | None = None) -> Mock:
        response = Mock()
        response.status_code = status_code
        response.json.return_value = payload or {}
        return response

    def test_code_extraction_requires_context_or_trusted_xai_sender(self) -> None:
        self.assertIsNone(
            duckmail.extract_verification_code(
                "Invoice 654321",
                "Your order number is 123456",
                sender="billing@example.com",
            )
        )
        self.assertEqual(
            duckmail.extract_verification_code("Security", "Your verification code is 654321"),
            "654321",
        )
        self.assertEqual(
            duckmail.extract_verification_code(
                "Account notice",
                "ABC-123",
                sender="xAI <no-reply@accounts.x.ai>",
            ),
            "ABC-123",
        )

    def test_poll_retries_timeout_rate_limit_and_server_error(self) -> None:
        rate_limited = self._response(429)
        server_error = self._response(503)
        message_list = self._response(
            200,
            {
                "hydra:member": [
                    {
                        "id": "message-id",
                        "subject": "ABC-123 xAI confirmation code",
                        "createdAt": "2026-01-01T00:00:00Z",
                    }
                ]
            },
        )
        message_detail = self._response(
            200,
            {
                "subject": "ABC-123 xAI confirmation code",
                "text": "Use this code to verify your account.",
                "from": {"address": "no-reply@x.ai"},
            },
        )

        with (
            patch.object(
                duckmail.requests,
                "get",
                side_effect=[
                    duckmail.requests.Timeout("timed out"),
                    rate_limited,
                    server_error,
                    message_list,
                    message_detail,
                ],
            ) as get_mock,
            patch.object(duckmail.requests, "patch") as mark_seen,
            patch.object(duckmail, "_interruptible_sleep", return_value=False),
            patch("builtins.print") as print_mock,
        ):
            code = duckmail.get_oai_code(
                "mail-token",
                "mail@example.com",
                timeout=1,
                interval=0,
            )

        self.assertEqual(code, "ABC123")
        self.assertEqual(get_mock.call_count, 5)
        mark_seen.assert_called_once()
        printed = " ".join(str(call) for call in print_mock.call_args_list)
        self.assertNotIn("ABC-123", printed)
        self.assertNotIn("ABC123", printed)


if __name__ == "__main__":
    unittest.main()
