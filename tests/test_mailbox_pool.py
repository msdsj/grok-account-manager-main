import os
import stat
import tempfile
import unittest
from pathlib import Path

from grok_account_manager.api.services import mailbox_pool


class OutlookMailboxPoolTests(unittest.TestCase):
    def test_inspect_accepts_compatible_rows_without_returning_secrets(self) -> None:
        result = mailbox_pool.inspect_outlook_mailbox_pool(
            "user@outlook.com----password----client-id----refresh-token----graph"
        )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["invalid"], 0)
        self.assertEqual(result["accounts"], [{"email": "user@outlook.com", "mode": "graph"}])
        self.assertNotIn("refresh-token", repr(result))

    def test_save_rejects_invalid_rows(self) -> None:
        with self.assertRaisesRegex(ValueError, "格式无效"):
            mailbox_pool.save_outlook_mailbox_pool("not-an-account")

    @unittest.skipUnless(os.name == "posix", "POSIX file modes are required")
    def test_save_writes_owner_only_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_path = mailbox_pool.OUTLOOK_MAILBOX_POOL_PATH
            mailbox_pool.OUTLOOK_MAILBOX_POOL_PATH = Path(directory) / "outlook-accounts.txt"
            try:
                mailbox_pool.save_outlook_mailbox_pool(
                    "user@outlook.com----password----client-id----refresh-token----auto"
                )
                mode = stat.S_IMODE(mailbox_pool.OUTLOOK_MAILBOX_POOL_PATH.stat().st_mode)
                self.assertEqual(mode, 0o600)
            finally:
                mailbox_pool.OUTLOOK_MAILBOX_POOL_PATH = old_path


if __name__ == "__main__":
    unittest.main()
