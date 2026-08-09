import tempfile
import threading
import unittest
from pathlib import Path
from random import Random

from grok_account_manager.core.proxy_pool import (
    ProxyPool,
    ProxyPoolError,
    ProxyPoolExhaustedError,
    load_proxy_file,
    mask_proxy_server,
    normalize_proxy_server,
    redact_proxy_secrets,
)


class ProxyPoolTests(unittest.TestCase):
    def test_file_parser_normalizes_comments_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxies.txt"
            path.write_text(
                "# ignored\n"
                "198.51.100.10:8080\n"
                "HTTP://198.51.100.10:8080 # duplicate\n"
                "HTTPS://Example.Proxy.Test:8443\n",
                encoding="utf-8",
            )

            self.assertEqual(
                load_proxy_file(path),
                (
                    "http://198.51.100.10:8080",
                    "https://example.proxy.test:8443",
                ),
            )

    def test_invalid_file_line_includes_source_line_without_echoing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxies.txt"
            path.write_text(
                "198.51.100.10:8080\n"
                "http://user:secret@host:not-a-port\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ProxyPoolError, r"第 2 行") as context:
                load_proxy_file(path)
            self.assertNotIn("secret", str(context.exception))

    def test_acquire_is_random_without_replacement(self) -> None:
        pool = ProxyPool.from_lines(
            [f"198.51.100.{index}:8080" for index in range(1, 9)],
            rng=Random(7),
        )

        acquired = [pool.acquire() for _ in range(8)]

        self.assertEqual(len(acquired), len(set(acquired)))
        self.assertEqual(pool.remaining, 0)
        with self.assertRaises(ProxyPoolExhaustedError):
            pool.acquire()
        self.assertIsNone(pool.try_acquire())

    def test_concurrent_acquire_returns_each_endpoint_once(self) -> None:
        pool = ProxyPool.from_lines(
            [f"198.51.100.{index}:8080" for index in range(1, 65)],
            rng=Random(19),
        )
        acquired: list[str] = []
        result_lock = threading.Lock()

        def take_one() -> None:
            endpoint = pool.acquire()
            with result_lock:
                acquired.append(endpoint)

        threads = [threading.Thread(target=take_one) for _ in range(64)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(acquired), 64)
        self.assertEqual(len(set(acquired)), 64)
        self.assertEqual(pool.used, 64)

    def test_summary_and_error_redaction_hide_proxy_credentials(self) -> None:
        summary = mask_proxy_server("http://198.51.100.10:8080")
        error = redact_proxy_secrets(
            "browser failed --proxy-server=http://admin:secret@198.51.100.10:8080"
        )

        self.assertEqual(summary, "http://198.***.***.10:8080")
        self.assertNotIn("admin", error)
        self.assertNotIn("secret", error)
        self.assertNotIn("198.51.100.10", error)
        self.assertEqual(mask_proxy_server(None), "直连")

    def test_normalizer_rejects_unsupported_credentials_or_missing_port(self) -> None:
        with self.assertRaises(ProxyPoolError):
            normalize_proxy_server("socks5://198.51.100.10:1080")
        with self.assertRaises(ProxyPoolError):
            normalize_proxy_server("http://admin:secret@198.51.100.10:8080")
        with self.assertRaises(ProxyPoolError):
            normalize_proxy_server("198.51.100.10")

    def test_normalizer_deduplicates_hostname_case_and_ipv6_spelling(self) -> None:
        pool = ProxyPool.from_lines(
            [
                "HTTP://EXAMPLE.PROXY.TEST:8080",
                "http://example.proxy.test:8080",
                "http://[2001:0DB8:0:0:0:0:0:1]:8080",
                "http://[2001:db8::1]:8080",
            ]
        )

        self.assertEqual(pool.total, 2)
        self.assertEqual(
            set(pool.summaries()),
            {"http://ex***st:8080", "http://[IPv6]:8080"},
        )


if __name__ == "__main__":
    unittest.main()
