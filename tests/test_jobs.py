import os
import unittest
from unittest.mock import patch

from grok_account_manager.api.services.jobs import RegistrationJobManager


class RegistrationJobManagerTests(unittest.TestCase):
    def test_parallel_registration_limits_only_oauth_stage(self) -> None:
        manager = RegistrationJobManager()
        with (
            patch.dict(
                os.environ,
                {"GROK_ACCOUNT_MANAGER_MAX_OAUTH_CONCURRENCY": "2"},
                clear=False,
            ),
            patch("grok_account_manager.api.services.jobs.threading.Thread.start"),
        ):
            job = manager.start(
                total=5,
                concurrency=5,
                oauth_exchange=True,
                email_source="duckmail",
            )

        self.assertEqual(job["concurrency"], 5)
        self.assertEqual(job["oauthConcurrency"], 2)
        self.assertGreater(job["roundTimeoutSeconds"], 180)
        self.assertEqual(len(job["workers"]), 5)
        self.assertEqual({worker["worker"] for worker in job["workers"]}, {1, 2, 3, 4, 5})

    def test_oauth_checkpoint_is_visible_but_not_persisted(self) -> None:
        manager = RegistrationJobManager()
        with (
            patch.dict(
                os.environ,
                {"GROK_ACCOUNT_MANAGER_MAX_OAUTH_CONCURRENCY": "2"},
                clear=False,
            ),
            patch("grok_account_manager.api.services.jobs.threading.Thread.start"),
        ):
            manager.start(
                total=1,
                concurrency=1,
                oauth_exchange=True,
                email_source="duckmail",
            )

        callback = manager._registration_checkpoint_callback(1, 1)
        with patch.object(manager, "_persist_result") as persist:
            callback(
                {
                    "email": "pending@example.com",
                    "credential": "pending-sso",
                    "oauth_status": "pending",
                }
            )

        persist.assert_not_called()
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["registered"], 1)
        self.assertEqual(snapshot["registeredAccounts"][0]["oauthStatus"], "pending")

    def test_failed_oauth_result_is_not_persisted(self) -> None:
        manager = RegistrationJobManager()
        with patch.object(manager, "_persist_result") as persist:
            status, error, saved = manager._persist_completed_result(
                "grok",
                {
                    "email": "failed@example.com",
                    "credential": "failed-sso",
                    "oauth_status": "failed",
                    "oauth_error": "rate limited",
                },
                oauth_required=True,
            )

        persist.assert_not_called()
        self.assertEqual(status, "failed")
        self.assertEqual(error, "rate limited")
        self.assertFalse(saved)


if __name__ == "__main__":
    unittest.main()
