import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from DrissionPage import ChromiumOptions

from grok_account_manager.core.browser import (
    DrissionBrowserSession,
    _ACTIVE_PROFILE_PATHS,
    _path_key,
    _validate_browser_isolation,
    build_chromium_options,
)


class BrowserIsolationTests(unittest.TestCase):
    def test_proxy_server_is_forwarded_to_chrome_options(self) -> None:
        options = build_chromium_options(proxy_url="198.51.100.10:8080")

        self.assertIn("--proxy-server=http://198.51.100.10:8080", options.arguments)
        self.assertEqual(options.proxy, "http://198.51.100.10:8080")
        self.assertEqual(options._grok_proxy_server, "http://198.51.100.10:8080")

    def test_proxy_is_required_by_strict_browser_isolation_validation(self) -> None:
        with tempfile.TemporaryDirectory() as profile, tempfile.TemporaryDirectory() as cache:
            command = [
                "chrome",
                "--remote-debugging-port=43123",
                f"--user-data-dir={profile}",
                f"--disk-cache-dir={cache}",
                "--incognito",
                "--proxy-server=http://198.51.100.10:8080",
            ]

            _validate_browser_isolation(
                profile_dir=Path(profile),
                cache_dir=Path(cache),
                debug_port=43123,
                browser_address="127.0.0.1:43123",
                browser_user_data_path=profile,
                browser_pid=1234,
                browser_command=command,
                expected_proxy_server="http://198.51.100.10:8080",
            )
            with self.assertRaisesRegex(RuntimeError, "指定代理"):
                _validate_browser_isolation(
                    profile_dir=Path(profile),
                    cache_dir=Path(cache),
                    debug_port=43123,
                    browser_address="127.0.0.1:43123",
                    browser_user_data_path=profile,
                    browser_pid=1234,
                    browser_command=command,
                    expected_proxy_server="http://198.51.100.11:8080",
                )

    def test_minimized_browser_option_is_forwarded_to_chrome(self) -> None:
        options = build_chromium_options(start_minimized=True)

        self.assertIn("--start-minimized", options.arguments)
        self.assertTrue(options._grok_start_minimized)

    def test_window_can_be_minimized_and_restored_to_assigned_bounds(self) -> None:
        options = ChromiumOptions()
        options._grok_window_bounds = (10, 20, 800, 600)
        session = DrissionBrowserSession(options)
        page = Mock()
        page.run_cdp.side_effect = lambda method, **_kwargs: (
            {"windowId": 42} if method == "Browser.getWindowForTarget" else {}
        )
        session._page = page

        self.assertTrue(session.set_window_minimized(True))
        page.run_cdp.assert_any_call(
            "Browser.setWindowBounds",
            windowId=42,
            bounds={"windowState": "minimized"},
        )

        page.reset_mock()
        page.run_cdp.side_effect = lambda method, **_kwargs: (
            {"windowId": 42} if method == "Browser.getWindowForTarget" else {}
        )
        self.assertTrue(session.set_window_minimized(False))
        page.run_cdp.assert_any_call(
            "Browser.setWindowBounds",
            windowId=42,
            bounds={"windowState": "normal"},
        )
        page.run_cdp.assert_any_call(
            "Browser.setWindowBounds",
            windowId=42,
            bounds={"left": 10, "top": 20, "width": 800, "height": 600},
        )
        page.run_cdp.assert_any_call("Page.bringToFront")

    def test_window_state_is_remembered_while_browser_starts(self) -> None:
        session = DrissionBrowserSession(ChromiumOptions())

        self.assertFalse(session.set_window_minimized(True))
        self.assertTrue(session._minimize_on_start)

    def test_start_minimized_does_not_restore_window_first(self) -> None:
        options = ChromiumOptions()
        options._grok_window_bounds = (10, 20, 800, 600)
        options._grok_start_minimized = True
        session = DrissionBrowserSession(options)
        page = Mock()
        page.run_cdp.side_effect = lambda method, **_kwargs: (
            {"windowId": 42} if method == "Browser.getWindowForTarget" else {}
        )
        session._page = page

        session._apply_window_bounds()

        calls = [call for call in page.run_cdp.call_args_list if call.args[0] == "Browser.setWindowBounds"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs["bounds"], {"windowState": "minimized"})

    def test_valid_chrome_process_resources_pass_strict_validation(self) -> None:
        with tempfile.TemporaryDirectory() as profile, tempfile.TemporaryDirectory() as cache:
            _validate_browser_isolation(
                profile_dir=Path(profile),
                cache_dir=Path(cache),
                debug_port=43123,
                browser_address="127.0.0.1:43123",
                browser_user_data_path=profile,
                browser_pid=1234,
                browser_command=[
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "--remote-debugging-port=43123",
                    f"--user-data-dir={profile}",
                    f"--disk-cache-dir={cache}",
                    "--incognito",
                ],
            )

    def test_profile_or_port_mismatch_fails_closed(self) -> None:
        with (
            tempfile.TemporaryDirectory() as profile,
            tempfile.TemporaryDirectory() as other_profile,
            tempfile.TemporaryDirectory() as cache,
        ):
            with self.assertRaisesRegex(RuntimeError, "隔离校验失败"):
                _validate_browser_isolation(
                    profile_dir=Path(profile),
                    cache_dir=Path(cache),
                    debug_port=43123,
                    browser_address="127.0.0.1:43123",
                    browser_user_data_path=other_profile,
                    browser_pid=1234,
                    browser_command=[
                        "chrome",
                        "--remote-debugging-port=9222",
                        f"--user-data-dir={other_profile}",
                        f"--disk-cache-dir={cache}",
                        "--incognito",
                    ],
                )

    def test_terminal_stop_prevents_restart_and_new_tab(self) -> None:
        session = DrissionBrowserSession(ChromiumOptions())
        session.stop()

        with self.assertRaisesRegex(RuntimeError, "禁止重新启动"):
            session.start()
        with self.assertRaisesRegex(RuntimeError, "浏览器已关闭"):
            session.open_new_tab()
        with self.assertRaisesRegex(RuntimeError, "禁止重新启动"):
            session.restart()

    def test_start_retries_after_cleanup(self) -> None:
        session = DrissionBrowserSession(ChromiumOptions())
        start_attempt = Mock(side_effect=[RuntimeError("transient startup failure"), session])

        with (
            patch.object(session, "_start_locked", start_attempt),
            patch.object(session, "_shutdown_browser_locked") as cleanup,
            patch("grok_account_manager.core.browser.time.sleep") as retry_sleep,
        ):
            result = session.start()

        self.assertIs(result, session)
        self.assertEqual(start_attempt.call_count, 2)
        cleanup.assert_called_once_with()
        retry_sleep.assert_called_once()

    def test_start_retry_count_is_bounded(self) -> None:
        session = DrissionBrowserSession(ChromiumOptions())
        start_attempt = Mock(side_effect=RuntimeError("persistent startup failure"))

        with (
            patch.object(session, "_start_locked", start_attempt),
            patch.object(session, "_shutdown_browser_locked") as cleanup,
            patch("grok_account_manager.core.browser.time.sleep") as retry_sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "persistent startup failure"):
                session.start()

        self.assertEqual(start_attempt.call_count, 3)
        self.assertEqual(cleanup.call_count, 3)
        self.assertEqual(retry_sleep.call_count, 2)

    def test_start_retry_log_redacts_proxy_credentials(self) -> None:
        session = DrissionBrowserSession(ChromiumOptions())
        start_attempt = Mock(
            side_effect=RuntimeError(
                "launch failed --proxy-server=http://alice:proxy-password@198.51.100.14:8080"
            )
        )

        with (
            patch.object(session, "_start_locked", start_attempt),
            patch.object(session, "_shutdown_browser_locked"),
            patch("grok_account_manager.core.browser.time.sleep"),
            patch("builtins.print") as printed,
        ):
            with self.assertRaises(RuntimeError):
                session.start()

        output = " ".join(str(call) for call in printed.call_args_list)
        self.assertNotIn("alice", output)
        self.assertNotIn("proxy-password", output)
        self.assertNotIn("198.51.100.14", output)

    def test_stop_request_cancels_start_retries(self) -> None:
        session = DrissionBrowserSession(ChromiumOptions())

        def fail_after_stop_request():
            session._stop_requested = True
            raise RuntimeError("startup interrupted")

        with (
            patch.object(session, "_start_locked", side_effect=fail_after_stop_request) as start_attempt,
            patch.object(session, "_shutdown_browser_locked") as cleanup,
            patch("grok_account_manager.core.browser.time.sleep") as retry_sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "取消浏览器启动重试"):
                session.start()

        start_attempt.assert_called_once_with()
        cleanup.assert_called_once_with()
        retry_sleep.assert_not_called()

    def test_restart_uses_start_retries_after_normal_shutdown(self) -> None:
        session = DrissionBrowserSession(ChromiumOptions())
        start_attempt = Mock(side_effect=[RuntimeError("restart startup failure"), session])

        with (
            patch.object(session, "_start_locked", start_attempt),
            patch.object(session, "_shutdown_browser_locked") as cleanup,
            patch("grok_account_manager.core.browser.time.sleep"),
        ):
            result = session.restart()

        self.assertIs(result, session)
        self.assertEqual(start_attempt.call_count, 2)
        self.assertEqual(cleanup.call_count, 2)

    def test_restart_accepts_proxy_url_and_updates_launch_argument(self) -> None:
        session = DrissionBrowserSession(ChromiumOptions())
        start_attempt = Mock(side_effect=[session])

        with (
            patch.object(session, "_start_locked", start_attempt),
            patch.object(session, "_shutdown_browser_locked"),
        ):
            result = session.restart(proxy_url="198.51.100.12:8080")

        self.assertIs(result, session)
        self.assertIn("--proxy-server=http://198.51.100.12:8080", session._options.arguments)
        self.assertEqual(session.proxy_summary, "http://198.***.***.12:8080")

    def test_set_proxy_while_running_requires_explicit_restart(self) -> None:
        session = DrissionBrowserSession(ChromiumOptions())
        session._browser = Mock()

        with self.assertRaisesRegex(RuntimeError, "需要重启"):
            session.set_proxy("198.51.100.13:8080")

    def test_set_proxy_can_restart_into_a_new_launch_configuration(self) -> None:
        session = DrissionBrowserSession(ChromiumOptions())
        session._browser = Mock()

        with (
            patch.object(session, "_start_with_retry_locked", return_value=session),
            patch.object(session, "_shutdown_browser_locked") as cleanup,
        ):
            result = session.set_proxy(proxy_url="198.51.100.14:8080", restart=True)

        self.assertIs(result, session)
        cleanup.assert_called_once_with()
        self.assertIn("--proxy-server=http://198.51.100.14:8080", session._options.arguments)

    def test_failed_unverified_start_cleans_matching_chrome_process(self) -> None:
        session = DrissionBrowserSession(ChromiumOptions())
        session._profile_dir = Path("/tmp/grok-chrome-profile-test")
        session._cache_dir = Path("/tmp/grok-chrome-cache-test")
        session.debug_port = 43123
        process = Mock()
        process.pid = 4321
        process.create_time.return_value = 12.5

        with (
            patch(
                "grok_account_manager.core.browser._find_owned_browser_processes",
                return_value=[(process, [])],
            ),
            patch("grok_account_manager.core.browser._stop_owned_browser_process") as stop_process,
            patch.object(session, "_cleanup_profile") as cleanup_profile,
        ):
            session._shutdown_browser_locked()

        stop_process.assert_called_once_with(4321, 12.5)
        cleanup_profile.assert_called_once_with()

    def test_stop_request_is_visible_while_start_lock_is_busy(self) -> None:
        session = DrissionBrowserSession(ChromiumOptions())
        session._lifecycle_lock.acquire()
        stop_thread = threading.Thread(target=session.stop)
        try:
            stop_thread.start()
            deadline = time.monotonic() + 1
            while not session._stop_requested and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(session._stop_requested)
        finally:
            session._lifecycle_lock.release()
            stop_thread.join(timeout=1)
        self.assertFalse(stop_thread.is_alive())

    def test_request_stop_terminates_only_owned_process(self) -> None:
        session = DrissionBrowserSession(ChromiumOptions())
        session._owns_browser_process = True
        session.browser_pid = 4321
        session._browser_process_create_time = 12.5
        process = Mock()
        process.create_time.return_value = 12.5
        process.children.return_value = []

        with patch("grok_account_manager.core.browser.psutil.Process", return_value=process):
            session.request_stop()

        process.kill.assert_called_once_with()
        self.assertTrue(session._stop_requested)

    def test_profile_allocation_failure_cleans_partial_directory(self) -> None:
        session = DrissionBrowserSession(ChromiumOptions())
        with tempfile.TemporaryDirectory() as parent:
            profile = Path(parent) / "partial-profile"
            profile.mkdir()
            with patch(
                "grok_account_manager.core.browser.tempfile.mkdtemp",
                side_effect=[str(profile), OSError("cache allocation failed")],
            ):
                with self.assertRaisesRegex(OSError, "cache allocation failed"):
                    session._prepare_fresh_profile()

            self.assertFalse(profile.exists())
            self.assertNotIn(_path_key(profile), _ACTIVE_PROFILE_PATHS)


if __name__ == "__main__":
    unittest.main()
