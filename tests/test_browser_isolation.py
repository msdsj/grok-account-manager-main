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
