import os
from pathlib import Path
import threading
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from grok_account_manager.api.services.pending import (
    OAUTH_PENDING,
    PERSISTENCE_FAILED,
    PendingResultStore,
)
from grok_account_manager.api.services.jobs import (
    RegistrationJobManager,
    _is_oauth_access_denied,
)


class RegistrationJobManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.pending_store = PendingResultStore(Path(self._tmpdir.name) / "pending.json")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _manager(self) -> RegistrationJobManager:
        return RegistrationJobManager(pending_store=self.pending_store)

    def test_parallel_registration_limits_only_oauth_stage(self) -> None:
        manager = self._manager()
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
        manager = self._manager()
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
        pending = self.pending_store.list(OAUTH_PENDING)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["result"]["credential"], "pending-sso")

    def test_ready_oauth_result_is_persisted_then_removed_from_pending_store(self) -> None:
        manager = self._manager()
        with patch("grok_account_manager.api.services.jobs.threading.Thread.start"):
            manager.start(total=1, concurrency=1, oauth_exchange=True, email_source="duckmail")
        manager._registration_checkpoint_callback(1, 1)(
            {
                "email": "ready@example.com",
                "credential": "browser-sso",
                "oauth_status": "pending",
            }
        )
        pending_id = self.pending_store.list(OAUTH_PENDING)[0]["id"]
        ready_result = {
            "email": "ready@example.com",
            "credential": "browser-sso",
            "oauth_status": "ready",
            "full_credential": {
                "email": "ready@example.com",
                "access_token": "access",
                "refresh_token": "refresh",
                "sso_token": "browser-sso",
            },
        }

        def assert_durable_before_persist(*_args, **_kwargs) -> None:
            retryable = self.pending_store.list(PERSISTENCE_FAILED)
            self.assertEqual(len(retryable), 1)
            self.assertEqual(retryable[0]["result"]["full_credential"]["refresh_token"], "refresh")

        with patch.object(manager, "_persist_result", side_effect=assert_durable_before_persist) as persist:
            status, error, saved = manager._persist_completed_result(
                "grok",
                ready_result,
                oauth_required=True,
                pending_id=pending_id,
            )

        persist.assert_called_once_with("grok", ready_result, write_txt=True)
        self.assertEqual((status, error, saved), ("ready", "", True))
        self.assertEqual(self.pending_store.list(), [])

    def test_final_persistence_failure_replaces_oauth_pending_with_retryable_result(self) -> None:
        manager = self._manager()
        with patch("grok_account_manager.api.services.jobs.threading.Thread.start"):
            manager.start(total=1, concurrency=1, oauth_exchange=True, email_source="duckmail")
        manager._registration_checkpoint_callback(1, 1)(
            {
                "email": "retry@example.com",
                "credential": "browser-sso",
                "oauth_status": "pending",
            }
        )
        pending_id = self.pending_store.list(OAUTH_PENDING)[0]["id"]
        ready_result = {
            "email": "retry@example.com",
            "credential": "browser-sso",
            "oauth_status": "ready",
            "full_credential": {
                "email": "retry@example.com",
                "access_token": "access",
                "refresh_token": "refresh",
                "sso_token": "browser-sso",
            },
        }

        with patch.object(manager, "_persist_result", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                manager._persist_completed_result(
                    "grok",
                    ready_result,
                    oauth_required=True,
                    pending_id=pending_id,
                )

        failed = self.pending_store.list(PERSISTENCE_FAILED)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["result"]["full_credential"]["refresh_token"], "refresh")
        self.assertEqual(failed[0]["error"], "disk full")

    def test_manager_initialization_retries_only_persistence_failures(self) -> None:
        oauth_result = {
            "email": "pending@example.com",
            "credential": "pending-sso",
            "oauth_status": "pending",
        }
        ready_result = {
            "email": "retry@example.com",
            "credential": "ready-sso",
            "oauth_status": "ready",
            "full_credential": {
                "email": "retry@example.com",
                "access_token": "access",
                "refresh_token": "refresh",
            },
        }
        self.pending_store.upsert(
            "old-job:1",
            status=OAUTH_PENDING,
            provider_name="grok",
            result=oauth_result,
            write_txt=True,
            oauth_required=True,
        )
        self.pending_store.upsert(
            "old-job:2",
            status=PERSISTENCE_FAILED,
            provider_name="grok",
            result=ready_result,
            write_txt=True,
            oauth_required=True,
        )

        with patch.object(RegistrationJobManager, "_persist_result") as persist:
            manager = RegistrationJobManager(pending_store=self.pending_store)

        persist.assert_called_once_with("grok", ready_result, write_txt=True)
        self.assertEqual(manager.pending_retry_summary, {"found": 1, "completed": 1, "failed": 0})
        remaining = self.pending_store.list()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["status"], OAUTH_PENDING)

    def test_failed_account_history_marks_job_completed_with_errors(self) -> None:
        manager = self._manager()
        manager._job = {
            "id": "job-with-persistence-error",
            "status": "running",
            "concurrency": 0,
            "oauthExchange": False,
            "failed": 0,
            "workerErrors": 0,
            "refreshTokenFailed": 0,
            "failedAccounts": [{"reason": "disk full"}],
            "events": [],
        }

        manager._run_job("job-with-persistence-error")

        self.assertEqual(manager._job["status"], "completed_with_errors")
        self.assertIn("落盘失败", manager._job["events"][-1]["message"])

    def test_failed_oauth_result_is_not_persisted(self) -> None:
        manager = self._manager()
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

    def test_worker_session_is_registered_before_browser_start(self) -> None:
        manager = self._manager()
        manager._job = {"concurrency": 1, "windowsMinimized": True}
        fake_session = Mock()

        def assert_registered() -> None:
            self.assertIn(fake_session, manager._sessions)

        fake_session.start.side_effect = assert_registered
        with (
            patch("grok_account_manager.api.services.jobs.DrissionBrowserSession", return_value=fake_session),
            patch("grok_account_manager.api.services.jobs.build_chromium_options") as build_options,
        ):
            result = manager._start_worker_session(1)

        self.assertIs(result, fake_session)
        build_options.assert_called_once_with(
            "zh-CN",
            headless=False,
            window_index=0,
            window_count=1,
            start_minimized=True,
        )
        fake_session.set_window_minimized.assert_called_once_with(True, apply_now=False)

    def test_window_command_updates_ready_and_starting_sessions(self) -> None:
        manager = self._manager()
        manager._job = {
            "status": "running",
            "windowsMinimized": False,
            "events": [],
        }
        ready_session = Mock()
        ready_session.set_window_minimized.return_value = True
        starting_session = Mock()
        starting_session.set_window_minimized.return_value = False
        manager._sessions.update({ready_session, starting_session})

        result = manager.set_windows_minimized(True)

        self.assertTrue(result["job"]["windowsMinimized"])
        self.assertEqual(result["changed"], 1)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["errors"], [])
        ready_session.set_window_minimized.assert_called_once_with(True)
        starting_session.set_window_minimized.assert_called_once_with(True)

    def test_retry_preserves_browser_window_preference(self) -> None:
        manager = self._manager()
        manager._last_config = {
            "oauth_exchange": True,
            "minimize_browsers": False,
            "email_source": "duckmail",
        }

        with patch.object(manager, "start", return_value={}) as start:
            manager.retry()

        self.assertFalse(start.call_args.kwargs["minimize_browsers"])

    def test_round_timeout_requests_cooperative_stop(self) -> None:
        manager = self._manager()
        round_stop = threading.Event()

        class Provider:
            current_stage = "waiting"
            current_email = ""

            @staticmethod
            def run_round(_session):
                while not round_stop.is_set():
                    time.sleep(0.005)
                return {}

        status, payload = manager._run_round_with_timeout(
            Provider(),
            Mock(),
            timeout_seconds=0.02,
            worker_index=1,
            round_index=1,
            round_stop_event=round_stop,
        )

        self.assertEqual(status, "timeout")
        self.assertIsNone(payload)
        self.assertTrue(round_stop.is_set())

    def test_access_denied_classifier_ignores_transport_errors(self) -> None:
        self.assertTrue(_is_oauth_access_denied("invalid_grant (Access denied)"))
        self.assertTrue(_is_oauth_access_denied("access_denied"))
        self.assertFalse(_is_oauth_access_denied("connection reset"))

    def test_repeated_access_denied_opens_oauth_circuit(self) -> None:
        manager = self._manager()
        manager._job = {
            "status": "running",
            "refreshTokenCompleted": 0,
            "refreshTokenFailed": 0,
            "oauthAccessDeniedStreak": 0,
            "oauthCircuitOpen": False,
            "oauthCircuitThreshold": 2,
            "registeredAccounts": [
                {"round": 1, "oauthFinalized": False},
                {"round": 2, "oauthFinalized": False},
            ],
            "workers": [{"worker": 1}, {"worker": 2}],
            "events": [],
        }

        error = "Grok OAuth 失败: invalid_grant (Access denied)"
        manager._finalize_registered_account(
            worker_index=1,
            round_index=1,
            oauth_status="failed",
            oauth_error=error,
        )
        self.assertFalse(manager._stop_event.is_set())

        manager._finalize_registered_account(
            worker_index=2,
            round_index=2,
            oauth_status="failed",
            oauth_error=error,
        )
        snapshot = manager.snapshot()
        self.assertTrue(manager._stop_event.is_set())
        self.assertTrue(snapshot["oauthCircuitOpen"])
        self.assertEqual(snapshot["oauthAccessDeniedStreak"], 2)
        self.assertEqual(snapshot["status"], "stopping")
        self.assertIn("Access denied", snapshot["workers"][0]["message"])

    def test_stop_returns_via_async_browser_cleanup(self) -> None:
        manager = self._manager()
        manager._job = {"status": "running", "events": []}
        session = Mock()
        manager._sessions.add(session)

        with patch.object(manager, "_close_sessions_async") as close_async:
            snapshot = manager.stop()

        self.assertEqual(snapshot["status"], "stopping")
        session.request_stop.assert_called_once_with()
        close_async.assert_called_once_with([session])


if __name__ == "__main__":
    unittest.main()
