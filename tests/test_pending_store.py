import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from grok_account_manager.api.services.pending import (
    OAUTH_PENDING,
    PERSISTENCE_FAILED,
    PendingResultStore,
)


class PendingResultStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "pending.json"
        self.store = PendingResultStore(self.path)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_atomic_store_uses_private_permissions_and_supports_status_filter(self) -> None:
        self.store.upsert(
            "job-1:1",
            status=OAUTH_PENDING,
            provider_name="grok",
            result={"email": "pending@example.com", "credential": "secret"},
            write_txt=True,
            oauth_required=True,
            job_id="job-1",
            worker_index=2,
            round_index=1,
        )
        self.store.upsert(
            "job-2:1",
            status=PERSISTENCE_FAILED,
            provider_name="grok",
            result={"email": "failed@example.com", "credential": "secret-2"},
            write_txt=False,
            oauth_required=False,
            error="disk full",
        )

        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(len(self.store.list()), 2)
        self.assertEqual([item["id"] for item in self.store.list(OAUTH_PENDING)], ["job-1:1"])
        self.assertEqual(
            [item["id"] for item in self.store.list(PERSISTENCE_FAILED)],
            ["job-2:1"],
        )
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))[0]["status"], OAUTH_PENDING)

    def test_update_error_and_remove_are_durable(self) -> None:
        self.store.upsert(
            "job-1:1",
            status=PERSISTENCE_FAILED,
            provider_name="grok",
            result={"email": "failed@example.com"},
            write_txt=True,
            oauth_required=False,
        )

        self.assertTrue(self.store.note_error("job-1:1", "still full", increment_attempts=True))
        record = self.store.list(PERSISTENCE_FAILED)[0]
        self.assertEqual(record["error"], "still full")
        self.assertEqual(record["attempts"], 1)

        self.assertTrue(self.store.remove("job-1:1"))
        self.assertEqual(self.store.list(), [])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
