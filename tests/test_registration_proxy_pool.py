import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grok_account_manager.api.services.registration_proxy_pool import (
    clear_saved_registration_proxies,
    load_saved_registration_proxies,
    registration_proxy_pool_snapshot,
    save_registration_proxy_nodes,
)


class RegistrationProxyPoolStorageTests(unittest.TestCase):
    def test_import_is_persistent_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registration-proxies.json"
            with patch(
                "grok_account_manager.api.services.registration_proxy_pool.REGISTRATION_PROXY_POOL_PATH",
                path,
            ):
                result = save_registration_proxy_nodes(
                    "198.51.100.10:8080\n198.51.100.11:8080\n"
                )
                self.assertEqual(result["count"], 2)
                self.assertEqual(
                    load_saved_registration_proxies(),
                    (
                        "http://198.51.100.10:8080",
                        "http://198.51.100.11:8080",
                    ),
                )
                self.assertEqual(registration_proxy_pool_snapshot()["count"], 2)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

                result = save_registration_proxy_nodes(
                    "198.51.100.11:8080\n198.51.100.12:8080\n"
                )
                self.assertEqual(result["added"], 1)
                self.assertEqual(result["skipped"], 1)
                self.assertEqual(len(load_saved_registration_proxies()), 3)

    def test_replace_discards_previous_nodes_and_clear_removes_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registration-proxies.json"
            with patch(
                "grok_account_manager.api.services.registration_proxy_pool.REGISTRATION_PROXY_POOL_PATH",
                path,
            ):
                save_registration_proxy_nodes("198.51.100.20:8080")
                result = save_registration_proxy_nodes(
                    "https://198.51.100.21:8443", replace=True
                )
                self.assertEqual(result["count"], 1)
                self.assertEqual(
                    load_saved_registration_proxies(),
                    ("https://198.51.100.21:8443",),
                )
                clear_saved_registration_proxies()
                self.assertFalse(path.exists())
                self.assertEqual(load_saved_registration_proxies(), ())

    def test_credentialed_nodes_are_rejected_without_echoing_the_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registration-proxies.json"
            with patch(
                "grok_account_manager.api.services.registration_proxy_pool.REGISTRATION_PROXY_POOL_PATH",
                path,
            ):
                with self.assertRaises(ValueError) as context:
                    save_registration_proxy_nodes("http://user:secret@198.51.100.22:8080")
                snapshot = registration_proxy_pool_snapshot()
        self.assertNotIn("user", str(context.exception))
        self.assertNotIn("secret", str(context.exception))
        self.assertEqual(snapshot["count"], 0)


if __name__ == "__main__":
    unittest.main()
