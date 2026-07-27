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

    def test_five_windows_tile_inside_main_display(self) -> None:
        screen = (0, 0, 1920, 1050)
        bounds = [calculate_window_bounds(index, 5, screen) for index in range(5)]

        self.assertEqual(len(set(bounds)), 5)
        for left, top, width, height in bounds:
            self.assertGreaterEqual(left, 0)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(left + width, 1920)
            self.assertLessEqual(top + height, 1050)


if __name__ == "__main__":
    unittest.main()
