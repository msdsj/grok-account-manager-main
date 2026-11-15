import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from grok_account_manager.core import database as account_db
from grok_account_manager.providers.grok.oauth_authorize import _write_private_credential
from grok_account_manager.sinks.txt_file import TxtFileSink


@unittest.skipUnless(os.name == "posix", "POSIX file modes are required")
class SensitiveFilePermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def assert_private(self, path: Path) -> None:
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_txt_sink_creates_and_tightens_private_file(self) -> None:
        output_path = self.root / "sso.txt"
        output_path.write_text("existing\n", encoding="utf-8")
        output_path.chmod(0o644)

        TxtFileSink(output_path).push(
            "grok",
            {"credential": "new-secret", "oauth_status": "ready"},
        )

        self.assert_private(output_path)
        self.assertEqual(output_path.read_text(encoding="utf-8"), "existing\nnew-secret\n")

    def test_txt_sink_is_idempotent_for_pending_replay(self) -> None:
        output_path = self.root / "sso-idempotent.txt"
        sink = TxtFileSink(output_path)
        result = {"credential": "same-secret", "oauth_status": "ready"}

        sink.push("grok", result)
        sink.push("grok", result)

        self.assert_private(output_path)
        self.assertEqual(output_path.read_text(encoding="utf-8"), "same-secret\n")

    def test_manual_oauth_credential_is_private(self) -> None:
        output_path = self.root / "credentials" / "credential.json"

        _write_private_credential(output_path, {"refresh_token": "secret-refresh"})

        self.assert_private(output_path)
        self.assertEqual(json.loads(output_path.read_text(encoding="utf-8"))[0]["refresh_token"], "secret-refresh")

    def test_sqlite_database_is_private(self) -> None:
        old_db_path = account_db.DB_PATH
        account_db.DB_PATH = self.root / "accounts.db"
        try:
            account_db.DB_PATH.write_bytes(b"")
            account_db.DB_PATH.chmod(0o644)
            account_db.init_db()

            self.assert_private(account_db.DB_PATH)
            for sidecar in account_db._database_sidecar_paths():
                if sidecar.exists():
                    self.assert_private(sidecar)
        finally:
            account_db.DB_PATH = old_db_path


if __name__ == "__main__":
    unittest.main()
