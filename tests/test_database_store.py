import tempfile
import unittest
import json
from pathlib import Path

from grok_account_manager.api.services import database as account_db
from grok_account_manager.sinks.json_credential import JsonCredentialSink
from grok_account_manager.sinks.txt_file import TxtFileSink


class DatabaseStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = account_db.DB_PATH
        account_db.DB_PATH = Path(self._tmpdir.name) / "accounts.db"
        account_db.init_db()

    def tearDown(self) -> None:
        account_db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    def test_upsert_export_and_soft_delete(self) -> None:
        account = {
            "id": "acc-1",
            "exportKey": "credential.json:0",
            "email": "user@example.com",
            "displayName": "User",
            "authMode": "oauth",
            "planType": "Super",
            "userId": "user-1",
            "createdAt": 123,
            "createdAtLabel": "1970-01-01 00:00:00",
            "hasRefreshToken": True,
            "hasAccessToken": True,
            "fileName": "credential.json",
            "filePath": "/tmp/credential.json",
            "quota": {},
            "usageUpdatedAt": 123,
        }
        credential = {"id": "acc-1", "email": "user@example.com", "access_token": "token"}

        account_db.upsert_account(account, credential, Path("/tmp/credential.json"), 0)

        stored_accounts = account_db.list_accounts()
        self.assertEqual(len(stored_accounts), 1)
        self.assertEqual(stored_accounts[0]["email"], "user@example.com")

        exported = account_db.export_credentials()
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["access_token"], "token")

        deleted = account_db.soft_delete_accounts(["credential.json:0"])
        self.assertEqual(deleted, 1)
        self.assertEqual(account_db.list_accounts(), [])

    def test_json_sink_persists_credentials_to_database(self) -> None:
        credentials_dir = Path(self._tmpdir.name) / "credentials"
        credential = {
            "id": "acc-2",
            "email": "sink@example.com",
            "access_token": "sink-token",
            "refresh_token": "sink-refresh",
            "auth_mode": "oauth",
            "created_at": 456,
        }

        sink = JsonCredentialSink(str(credentials_dir))
        sink.push("grok", {"full_credential": credential})
        sink.flush()

        stored_accounts = account_db.list_accounts()
        self.assertEqual(len(stored_accounts), 1)
        self.assertEqual(stored_accounts[0]["email"], "sink@example.com")
        exported = account_db.export_credentials(["grok_credentials.json:0"])
        self.assertEqual(exported[0]["refresh_token"], "sink-refresh")

    def test_json_sink_initializes_empty_credentials_file(self) -> None:
        credentials_dir = Path(self._tmpdir.name) / "credentials"
        credentials_dir.mkdir(parents=True, exist_ok=True)
        credentials_path = credentials_dir / "grok_credentials.json"
        credentials_path.write_text("", encoding="utf-8")
        credential = {
            "id": "acc-3",
            "email": "empty-file@example.com",
            "access_token": "empty-token",
            "refresh_token": "empty-refresh",
            "auth_mode": "oauth",
        }

        sink = JsonCredentialSink(str(credentials_dir))
        sink.push("grok", {"full_credential": credential})
        sink.flush()

        self.assertEqual(credentials_path.read_text(encoding="utf-8").strip().startswith("["), True)
        exported = account_db.export_credentials(["grok_credentials.json:0"])
        self.assertEqual(exported[0]["email"], "empty-file@example.com")

    def test_json_sink_upgrades_sso_record_in_place(self) -> None:
        credentials_dir = Path(self._tmpdir.name) / "credentials"
        credentials_path = credentials_dir / "grok_credentials.json"

        checkpoint_sink = JsonCredentialSink(str(credentials_dir))
        checkpoint_sink.push(
            "grok",
            {
                "email": "parallel@example.com",
                "credential": "browser-sso",
                "profile": {"first_name": "Parallel"},
                "oauth_status": "not_requested",
            },
        )
        checkpoint_sink.flush()
        checkpoint = json.loads(credentials_path.read_text(encoding="utf-8"))[0]

        final_sink = JsonCredentialSink(str(credentials_dir))
        final_sink.push(
            "grok",
            {
                "email": "parallel@example.com",
                "credential": "browser-sso",
                "oauth_status": "ready",
                "oauth_error": "",
                "full_credential": {
                    "id": "replacement-id",
                    "email": "parallel@example.com",
                    "access_token": "oauth-access",
                    "refresh_token": "oauth-refresh",
                    "sso_token": "browser-sso",
                    "auth_mode": "oauth",
                },
            },
        )
        final_sink.flush()

        credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
        self.assertEqual(len(credentials), 1)
        self.assertEqual(credentials[0]["id"], checkpoint["id"])
        self.assertEqual(credentials[0]["refresh_token"], "oauth-refresh")
        self.assertEqual(credentials[0]["oauth_exchange_status"], "ready")
        self.assertIsNone(credentials[0]["oauth_exchange_error"])
        stored_accounts = account_db.list_accounts()
        self.assertEqual(len(stored_accounts), 1)
        self.assertTrue(stored_accounts[0]["hasRefreshToken"])

    def test_json_sink_rejects_oauth_result_without_refresh_token(self) -> None:
        credentials_dir = Path(self._tmpdir.name) / "credentials"
        sink = JsonCredentialSink(str(credentials_dir))

        with self.assertRaisesRegex(ValueError, "refresh_token"):
            sink.push(
                "grok",
                {
                    "email": "discard@example.com",
                    "credential": "discard-sso",
                    "oauth_status": "failed",
                    "oauth_error": "rate limited",
                },
            )

        self.assertFalse((credentials_dir / "grok_credentials.json").exists())

    def test_txt_sink_rejects_oauth_result_without_refresh_token(self) -> None:
        output_path = Path(self._tmpdir.name) / "sso.txt"
        sink = TxtFileSink(output_path)

        with self.assertRaisesRegex(ValueError, "refresh_token"):
            sink.push(
                "grok",
                {
                    "email": "discard@example.com",
                    "credential": "discard-sso",
                    "oauth_status": "failed",
                },
            )

        self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
