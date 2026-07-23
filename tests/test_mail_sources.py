import unittest

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


if __name__ == "__main__":
    unittest.main()

