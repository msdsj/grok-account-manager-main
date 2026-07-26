import unittest
from unittest.mock import patch

from grok_account_manager.mail import sources
from grok_account_manager.mail.sources import parse_google_accounts, parse_outlook_accounts


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
        with (
            patch.object(sources, "_get_outlook_graph_inbox_count_with_token", return_value=12),
            patch.object(sources, "_outlook_graph_get", return_value=graph_payload) as graph_get,
        ):
            counts = {"INBOX": 10}
            code = sources._scan_outlook_graph_once("access-token", counts)

        self.assertEqual(code, "123456")
        self.assertEqual(counts[sources.OUTLOOK_GRAPH_INBOX_KEY], 12)
        self.assertEqual(graph_get.call_args.kwargs["params"]["$top"], "2")


if __name__ == "__main__":
    unittest.main()
