import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from grok_account_manager.api.routers.register import start_registration
from grok_account_manager.api.schemas import RegisterRequest
from grok_account_manager.api.services.jobs import (
    RegistrationJobManager,
    _build_proxy_pool,
)
from grok_account_manager.api.services.pending import PendingResultStore
from grok_account_manager.core.proxy_pool import ProxyPool


class RegistrationProxyJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.manager = RegistrationJobManager(
            pending_store=PendingResultStore(Path(self._tmpdir.name) / "pending.json")
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_inline_pool_is_random_and_masks_worker_state(self) -> None:
        self.manager._job = {
            "proxyPoolUsed": 0,
            "proxyPoolRemaining": 2,
            "workers": [{"worker": 1}],
            "events": [],
        }
        self.manager._proxy_pool = ProxyPool.from_lines(
            ["198.51.100.10:8080", "198.51.100.11:8080"]
        )

        first = self.manager._acquire_proxy(1, 1)
        second = self.manager._acquire_proxy(1, 2)

        self.assertNotEqual(first, second)
        self.assertEqual(self.manager._job["proxyPoolUsed"], 2)
        self.assertEqual(self.manager._job["proxyPoolRemaining"], 0)
        self.assertRegex(self.manager._job["workers"][0]["proxy"], r"^http://198\.\*{3}\.\*{3}\.(10|11):8080$")
        self.assertNotIn("198.51.100.11", str(self.manager.snapshot()))

    def test_ten_concurrent_workers_receive_ten_distinct_proxies(self) -> None:
        workers = [{"worker": index, "proxy": "直连"} for index in range(1, 11)]
        self.manager._job = {
            "proxyPoolUsed": 0,
            "proxyPoolRemaining": 10,
            "workers": workers,
            "events": [],
        }
        self.manager._proxy_pool = ProxyPool.from_lines(
            [f"198.51.100.{index}:8080" for index in range(1, 11)]
        )
        barrier = threading.Barrier(10)
        assigned: list[str] = []
        assigned_lock = threading.Lock()

        def claim(worker_index: int) -> None:
            barrier.wait(timeout=2)
            proxy = self.manager._acquire_proxy(worker_index, worker_index)
            with assigned_lock:
                assigned.append(str(proxy))

        threads = [threading.Thread(target=claim, args=(index,)) for index in range(1, 11)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(assigned), 10)
        self.assertEqual(len(set(assigned)), 10)
        self.assertEqual(self.manager._job["proxyPoolUsed"], 10)
        self.assertEqual(self.manager._job["proxyPoolRemaining"], 0)
        self.assertEqual(
            len({worker["proxy"] for worker in self.manager._job["workers"]}),
            10,
        )

    def test_start_caps_total_to_available_proxy_count_and_retry_preserves_source(self) -> None:
        data = "\n".join(
            [
                "198.51.100.20:8080",
                "198.51.100.21:8080",
            ]
        )
        with patch("grok_account_manager.api.services.jobs.threading.Thread.start"):
            job = self.manager.start(
                total=5,
                concurrency=4,
                oauth_exchange=False,
                email_source="duckmail",
                proxy_pool_enabled=True,
                proxy_data=data,
            )

        self.assertTrue(job["proxyPoolEnabled"])
        self.assertEqual(job["proxyPoolSource"], "inline")
        self.assertEqual(job["proxyPoolTotal"], 2)
        self.assertEqual(job["proxyPoolRemaining"], 2)
        self.assertEqual(job["total"], 2)
        self.assertEqual(self.manager._last_config["proxy_data"], data)
        with patch.object(self.manager, "start", return_value={}) as start:
            self.manager.retry()
        self.assertTrue(start.call_args.kwargs["proxy_pool_enabled"])
        self.assertEqual(start.call_args.kwargs["proxy_data"], data)

    def test_explicit_disabled_pool_skips_existing_default_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            default_file = Path(directory) / "xx.txt"
            default_file.write_text("198.51.100.30:8080\n", encoding="utf-8")
            with patch("grok_account_manager.api.services.jobs.DEFAULT_PROXY_POOL_PATH", default_file):
                pool, source = _build_proxy_pool(enabled=False, data="", file_path="")
        self.assertIsNone(pool)
        self.assertEqual(source, "")

    def test_auto_mode_without_any_source_uses_direct_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_default = Path(directory) / "xx.txt"
            with (
                patch("grok_account_manager.api.services.jobs.DEFAULT_PROXY_POOL_PATH", missing_default),
                patch(
                    "grok_account_manager.api.services.jobs.load_saved_registration_proxies",
                    return_value=(),
                ),
            ):
                pool, source = _build_proxy_pool(enabled=None, data="", file_path="")
        self.assertIsNone(pool)
        self.assertEqual(source, "")

    def test_worker_claims_proxy_only_after_claiming_each_round(self) -> None:
        class Session:
            identity = None
            isolation_summary = ""

            def __init__(self) -> None:
                self.restarts: list[str | None] = []

            def restart(self, *, proxy_server: str | None = None):
                self.restarts.append(proxy_server)
                return self

            def stop(self) -> None:
                return None

        class Provider:
            chrome_lang = "zh-CN"
            name = "grok"
            current_email = ""
            current_stage = ""
            registration_succeeded = False

        session = Session()
        self.manager._job = {
            "id": "proxy-job",
            "status": "running",
            "total": 3,
            "concurrency": 1,
            "oauthExchange": False,
            "windowsMinimized": True,
            "issued": 0,
            "active": 0,
            "completed": 0,
            "failed": 0,
            "registered": 0,
            "refreshTokenCompleted": 0,
            "refreshTokenFailed": 0,
            "workerErrors": 0,
            "failedAccounts": [],
            "registeredAccounts": [],
            "proxyPoolUsed": 0,
            "proxyPoolRemaining": 3,
            "workers": [{"worker": 1, "proxy": "直连"}],
            "events": [],
        }
        self.manager._proxy_pool = ProxyPool.from_lines(
            ["198.51.100.40:8080", "198.51.100.41:8080", "198.51.100.42:8080"]
        )

        with (
            patch.object(self.manager, "_stagger_worker_start"),
            patch.object(self.manager, "_pace_before_next_round"),
            patch.object(self.manager, "_start_worker_session", return_value=session) as start_session,
            patch.object(self.manager, "_create_provider", side_effect=[Provider(), Provider(), Provider()]),
            patch.object(self.manager, "_run_round_with_timeout", return_value=("ok", {"email": "test@example.com"})),
            patch.object(self.manager, "_persist_completed_result", return_value=("not_requested", "", True)),
        ):
            self.manager._run_worker(1, False)

        initial_proxy = start_session.call_args_list[0].kwargs["proxy_server"]
        self.assertEqual(len(start_session.call_args_list), 1)
        self.assertEqual(len(session.restarts), 2)
        assigned = [initial_proxy, *session.restarts]
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(self.manager._job["issued"], 3)
        self.assertEqual(self.manager._job["proxyPoolUsed"], 3)
        self.assertEqual(self.manager._job["proxyPoolRemaining"], 0)

    def test_registration_route_forwards_proxy_request_fields(self) -> None:
        body = RegisterRequest(
            total=2,
            concurrency=1,
            proxyPoolEnabled=True,
            proxyData="198.51.100.50:8080",
            proxyFile="",
        )

        with patch(
            "grok_account_manager.api.routers.register.JOB_MANAGER.start",
            return_value={"id": "proxy-job"},
        ) as start:
            result = start_registration(body)

        self.assertEqual(result, {"job": {"id": "proxy-job"}})
        self.assertTrue(start.call_args.kwargs["proxy_pool_enabled"])
        self.assertEqual(start.call_args.kwargs["proxy_data"], "198.51.100.50:8080")
        self.assertEqual(start.call_args.kwargs["proxy_file"], "")

    def test_worker_replaces_consumed_proxy_after_fresh_browser_start_failure(self) -> None:
        class Session:
            identity = None
            isolation_summary = ""

            def restart(self, *, proxy_server: str | None = None):
                return self

            def stop(self) -> None:
                return None

        class Provider:
            chrome_lang = "zh-CN"
            name = "grok"
            current_email = ""
            current_stage = ""
            registration_succeeded = False

        first_session = Session()
        third_session = Session()
        self.manager._job = {
            "id": "proxy-restart-job",
            "status": "running",
            "total": 3,
            "concurrency": 1,
            "oauthExchange": False,
            "windowsMinimized": True,
            "issued": 0,
            "active": 0,
            "completed": 0,
            "failed": 0,
            "registered": 0,
            "refreshTokenCompleted": 0,
            "refreshTokenFailed": 0,
            "workerErrors": 0,
            "failedAccounts": [],
            "registeredAccounts": [],
            "proxyPoolUsed": 0,
            "proxyPoolRemaining": 3,
            "workers": [{"worker": 1, "proxy": "直连"}],
            "events": [],
        }
        self.manager._proxy_pool = ProxyPool.from_lines(
            ["198.51.100.60:8080", "198.51.100.61:8080", "198.51.100.62:8080"]
        )

        with (
            patch.object(self.manager, "_stagger_worker_start"),
            patch.object(self.manager, "_pace_before_next_round"),
            patch.object(
                self.manager,
                "_start_worker_session",
                side_effect=[first_session, RuntimeError("browser start failed"), third_session],
            ) as start_session,
            patch.object(self.manager, "_create_provider", side_effect=[Provider(), Provider(), Provider()]),
            patch.object(
                self.manager,
                "_run_round_with_timeout",
                side_effect=[("timeout", None), ("ok", {"email": "test@example.com"})],
            ),
            patch.object(self.manager, "_persist_completed_result", return_value=("not_requested", "", True)),
        ):
            self.manager._run_worker(1, False)

        assigned = [call.kwargs["proxy_server"] for call in start_session.call_args_list]
        self.assertEqual(len(assigned), 3)
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(self.manager._job["proxyPoolUsed"], 3)
        self.assertEqual(self.manager._job["proxyPoolRemaining"], 0)

    def test_provider_retry_claims_a_fresh_proxy(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.restarts: list[str | None] = []

            def restart(self, *, proxy_server: str | None = None):
                self.restarts.append(proxy_server)
                return self

        self.manager._job = {
            "proxyPoolUsed": 0,
            "proxyPoolRemaining": 2,
            "workers": [{"worker": 1, "proxy": "直连"}],
            "events": [],
        }
        self.manager._proxy_pool = ProxyPool.from_lines(
            ["198.51.100.70:8080", "198.51.100.71:8080"]
        )
        initial_proxy = self.manager._acquire_proxy(1, 1)
        provider = self.manager._create_provider(False, threading.Event(), 1, 1)
        provider.max_registration_attempts = 2
        provider.registration_succeeded = False
        session = Session()

        with (
            patch.object(
                provider,
                "_run_round_once",
                side_effect=[RuntimeError("temporary registration failure"), {"email": "test@example.com"}],
            ),
            patch.object(provider, "_interruptible_sleep"),
        ):
            result = provider.run_round(session)

        self.assertEqual(result["email"], "test@example.com")
        self.assertEqual(len(session.restarts), 1)
        self.assertNotEqual(session.restarts[0], initial_proxy)
        self.assertEqual(self.manager._job["proxyPoolUsed"], 2)
        self.assertEqual(self.manager._job["proxyPoolRemaining"], 0)

    def test_browser_start_failure_uses_a_new_round_instead_of_ending_worker(self) -> None:
        class Session:
            identity = None
            isolation_summary = ""

            def restart(self, *, proxy_server: str | None = None):
                return self

            def stop(self) -> None:
                return None

        class Provider:
            chrome_lang = "zh-CN"
            name = "grok"
            current_email = ""
            current_stage = ""
            registration_succeeded = False

        self.manager._job = {
            "id": "retry-browser-start-job",
            "status": "running",
            "total": 2,
            "concurrency": 1,
            "oauthExchange": False,
            "windowsMinimized": True,
            "issued": 0,
            "active": 0,
            "completed": 0,
            "failed": 0,
            "registered": 0,
            "refreshTokenCompleted": 0,
            "refreshTokenFailed": 0,
            "workerErrors": 0,
            "failedAccounts": [],
            "registeredAccounts": [],
            "proxyPoolUsed": 0,
            "proxyPoolRemaining": 2,
            "workers": [{"worker": 1, "proxy": "直连"}],
            "events": [],
        }
        self.manager._proxy_pool = ProxyPool.from_lines(
            ["198.51.100.80:8080", "198.51.100.81:8080"]
        )
        recovered_session = Session()

        with (
            patch.object(self.manager, "_stagger_worker_start"),
            patch.object(self.manager, "_pace_before_next_round"),
            patch.object(
                self.manager,
                "_start_worker_session",
                side_effect=[RuntimeError("browser start failed"), recovered_session],
            ) as start_session,
            patch.object(self.manager, "_create_provider", side_effect=[Provider(), Provider()]),
            patch.object(
                self.manager,
                "_run_round_with_timeout",
                return_value=("ok", {"email": "test@example.com"}),
            ),
            patch.object(
                self.manager,
                "_persist_completed_result",
                return_value=("not_requested", "", True),
            ),
            patch("grok_account_manager.api.services.jobs.traceback.print_exc"),
        ):
            self.manager._run_worker(1, False)

        self.assertEqual(start_session.call_count, 2)
        self.assertEqual(self.manager._job["issued"], 2)
        self.assertEqual(self.manager._job["completed"], 1)
        self.assertEqual(self.manager._job["failed"], 1)
        self.assertEqual(self.manager._job["proxyPoolUsed"], 2)
        self.assertEqual(self.manager._job["proxyPoolRemaining"], 0)

    def test_job_snapshot_redacts_proxy_credentials_in_errors(self) -> None:
        secret = "proxy-password"
        error = f"browser failed --proxy-server=http://alice:{secret}@198.51.100.90:8080"
        self.manager._job = {
            "workers": [{"worker": 1, "proxy": "直连"}],
            "failedAccounts": [],
            "events": [],
        }

        self.manager._update_worker_state(1, message=error)
        self.manager._event("error", error)
        self.manager._record_failed_account(
            email="test@example.com",
            round_index=1,
            worker_index=1,
            stage="browser_start",
            reason=error,
        )

        snapshot = self.manager.snapshot()
        self.assertIsNotNone(snapshot)
        self.assertNotIn(secret, str(snapshot))
        self.assertNotIn("alice", str(snapshot))
        self.assertNotIn("198.51.100.90", str(snapshot))


if __name__ == "__main__":
    unittest.main()
