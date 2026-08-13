import unittest

from grok_account_manager.core.network import (
    build_browser_http_session,
    build_requests_session,
)


class _BrowserSession:
    proxy_server = "http://198.51.100.10:8080"

    def get_user_agent(self) -> str:
        return "Mozilla/5.0 Test Chrome/146.0.0.0"


class NetworkContextTests(unittest.TestCase):
    def test_build_requests_session_pins_proxy_ua_and_ignores_environment(self) -> None:
        session = build_requests_session(
            proxy_url="198.51.100.10:8080",
            user_agent="Mozilla/5.0 Browser UA",
        )
        try:
            self.assertFalse(session.trust_env)
            self.assertEqual(
                session.proxies,
                {
                    "http": "http://198.51.100.10:8080",
                    "https": "http://198.51.100.10:8080",
                },
            )
            self.assertEqual(session.headers["User-Agent"], "Mozilla/5.0 Browser UA")
            self.assertEqual(
                session._grok_browser_user_agent,
                "Mozilla/5.0 Browser UA",
            )
            self.assertTrue(session.verify)
        finally:
            session.close()

    def test_browser_session_captures_current_round_identity(self) -> None:
        session = build_browser_http_session(_BrowserSession())
        try:
            self.assertEqual(
                session.proxies["https"],
                "http://198.51.100.10:8080",
            )
            self.assertEqual(
                session.headers["User-Agent"],
                "Mozilla/5.0 Test Chrome/146.0.0.0",
            )
        finally:
            session.close()

    def test_browser_builder_tolerates_minimal_legacy_session(self) -> None:
        session = build_browser_http_session(object())
        try:
            self.assertEqual(session.proxies, {})
            self.assertFalse(session.trust_env)
            self.assertEqual(session._grok_browser_user_agent, "")
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
