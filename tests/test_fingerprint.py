import unittest

from grok_account_manager.core.fingerprint import (
    BrowserIdentity,
    build_fingerprint_script,
    random_identity,
)
from grok_account_manager.core.browser import calculate_window_bounds


class FingerprintTests(unittest.TestCase):
    def test_gpu_profile_matches_host_platform(self) -> None:
        mac_identity = random_identity("darwin")
        windows_identity = random_identity("win32")
        linux_identity = random_identity("linux")

        self.assertIn("Metal Renderer", mac_identity.gpu_renderer)
        self.assertNotIn("Direct3D11", mac_identity.gpu_renderer)
        self.assertIn("Direct3D11", windows_identity.gpu_renderer)
        self.assertIn("OpenGL", linux_identity.gpu_renderer)
        self.assertLessEqual(mac_identity.device_memory, 8)
        self.assertLessEqual(windows_identity.device_memory, 8)
        self.assertLessEqual(linux_identity.device_memory, 8)

    def test_canvas_noise_is_coordinate_deterministic(self) -> None:
        identity = BrowserIdentity(
            gpu_vendor="Google Inc. (Apple)",
            gpu_renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)",
            hardware_concurrency=8,
            device_memory=8,
            canvas_seed=123456,
        )

        script = build_fingerprint_script(identity)

        self.assertIn("pixelNoise(123456, x, y)", script)
        self.assertIn("origToBlob.apply(noisyCanvas(this), arguments)", script)
        self.assertNotIn("canvasRandom", script)
        self.assertNotIn("ctx.putImageData(imageData, 0, 0)", script)

    def test_identity_carries_languages_platform_and_webgl_extensions(self) -> None:
        identity = random_identity("darwin", lang="en-US")

        self.assertEqual(identity.languages[0], "en-US")
        self.assertEqual(identity.platform, "MacIntel")
        self.assertTrue(identity.webgl_extensions)
        self.assertIn("WEBGL_debug_renderer_info", identity.webgl_extensions)

    def test_languages_follow_requested_locale(self) -> None:
        self.assertEqual(random_identity("darwin", lang="zh-CN").languages, ("zh-CN", "zh", "en"))
        self.assertEqual(random_identity("darwin", lang="ja-JP").languages[0], "ja-JP")
        self.assertEqual(random_identity("win32", lang="").languages, ("en-US", "en"))

    def test_script_hides_patched_natives_from_toString(self) -> None:
        script = build_fingerprint_script(random_identity("darwin", lang="en-US"))

        # 没有这层伪装，所有 patch 都能被 fn.toString() 一眼识破。
        self.assertIn("toStringShim", script)
        self.assertIn("fakeRegistry", script)
        self.assertIn("[native code]", script)
        # 真实 Chrome 的 navigator.webdriver 是 false，写成 undefined 反而更显眼。
        self.assertIn("'webdriver', false", script)
        # 新增的指纹信号
        self.assertIn("getSupportedExtensions", script)
        self.assertIn("audioNoise", script)
        self.assertIn("Permissions", script)

    def test_all_placeholders_are_substituted(self) -> None:
        script = build_fingerprint_script(random_identity("darwin", lang="en-US"))

        for placeholder in (
            "__CANVAS_SEED__",
            "__AUDIO_SEED__",
            "__LANGUAGES__",
            "__HARDWARE_CONCURRENCY__",
            "__DEVICE_MEMORY__",
            "__PLATFORM__",
            "__GPU_VENDOR__",
            "__GPU_RENDERER__",
            "__WEBGL_EXTENSIONS__",
        ):
            self.assertNotIn(placeholder, script)

    def test_five_windows_tile_inside_main_display(self) -> None:
        screen = (0, 0, 1920, 1050)
        bounds = [calculate_window_bounds(index, 5, screen) for index in range(5)]

        self.assertEqual(len(set(bounds)), 5)
        for left, top, width, height in bounds:
            self.assertGreaterEqual(left, 0)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(left + width, 1920)
            self.assertLessEqual(top + height, 1050)

    def test_more_than_six_windows_do_not_overlap_exactly(self) -> None:
        """超过六个并发窗口时，多出来的窗口要错开，不能像素级完全重叠。"""
        screen = (0, 0, 1920, 1050)
        bounds = [calculate_window_bounds(index, 10, screen) for index in range(10)]

        self.assertEqual(len(set(bounds)), 10)
        for left, top, width, height in bounds:
            self.assertGreaterEqual(left, 0)
            self.assertGreaterEqual(top, 0)
            self.assertGreater(width, 0)
            self.assertGreater(height, 0)
            self.assertLessEqual(left + width, 1920)
            self.assertLessEqual(top + height, 1050)


if __name__ == "__main__":
    unittest.main()
