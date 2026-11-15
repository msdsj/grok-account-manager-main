import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grok_account_manager.core import database as db


class AccountTagsAndLogsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "app.db"
        self._output_dir = Path(self._tmpdir.name) / "output"
        self._patchers = [
            patch.object(db, "DB_PATH", self._db_path),
            patch.object(db, "OUTPUT_DIR", self._output_dir),
        ]
        for patcher in self._patchers:
            patcher.start()
        db.init_db()

    def tearDown(self) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmpdir.cleanup()

    def test_accounts_are_stored_with_tags_in_database(self) -> None:
        account = {
            "id": "account-1",
            "exportKey": "db:grok:account-1",
            "provider": "grok",
            "email": "user@example.test",
            "createdAt": 1,
        }
        db.upsert_account(account, {"email": "user@example.test"}, Path("database"), 0)

        self.assertEqual(db.update_account_tags(["db:grok:account-1"], ["primary", "verified"]), {"updated": 1})
        accounts = db.list_accounts()

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["tags"], ["primary", "verified"])
        self.assertEqual(db.list_account_tags(), [
            {"tag": "primary", "count": 1},
            {"tag": "verified", "count": 1},
        ])

    def test_operation_and_job_logs_are_persisted(self) -> None:
        db.insert_operation_log(
            "tester",
            "account.tags.update",
            target_type="account",
            target_id="account-1",
            detail={"tags": ["primary"]},
            result="success",
        )
        db.insert_job_log("job-1", "info", "job started", stage="start", worker=1, payload={"total": 1})

        operations = db.list_operation_logs(limit=10)
        jobs = db.list_job_logs("job-1")

        self.assertEqual(operations["total"], 1)
        self.assertEqual(operations["items"][0]["actor"], "tester")
        self.assertEqual(operations["items"][0]["action"], "account.tags.update")
        self.assertEqual(jobs["total"], 1)
        self.assertEqual(jobs["items"][0]["message"], "job started")


if __name__ == "__main__":
    unittest.main()
