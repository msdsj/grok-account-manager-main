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
            # 没有显式 per-round 代理时套用默认出口（本机本地代理）。
            #
            # 这一步很关键：Chrome 会自动继承 macOS 的系统代理设置，而
            # ``requests`` 只认 HTTP_PROXY / HTTPS_PROXY 环境变量、不认系统
            # 代理。两边不一致就会出现「浏览器注册走代理、网关调用却是本机
            # 直连」——服务端看到同一个账号在两个出口 IP 之间跳变。
            self.assertEqual(
                session.proxies.get("https"),
                "http://127.0.0.1:7890",
            )
            self.assertEqual(session._grok_browser_user_agent, "")
        finally:
            session.close()

    def test_default_egress_proxy_can_be_disabled(self) -> None:
        """显式写成 none/off/direct 时不套用默认出口。"""
        import os
        from unittest import mock

        from grok_account_manager.core.proxy_pool import (
            EGRESS_PROXY_ENV_VAR,
            get_fixed_egress_proxy,
        )

        with mock.patch.dict(os.environ, {EGRESS_PROXY_ENV_VAR: "none"}):
            self.assertIsNone(get_fixed_egress_proxy())
            session = build_requests_session()
            try:
                self.assertEqual(session.proxies, {})
            finally:
                session.close()

    def test_explicit_egress_proxy_overrides_the_default(self) -> None:
        import os
        from unittest import mock

        from grok_account_manager.core.proxy_pool import (
            EGRESS_PROXY_ENV_VAR,
            get_fixed_egress_proxy,
        )

        with mock.patch.dict(os.environ, {EGRESS_PROXY_ENV_VAR: "198.51.100.20:1080"}):
            self.assertEqual(get_fixed_egress_proxy(), "http://198.51.100.20:1080")
            session = build_requests_session()
            try:
                self.assertEqual(
                    session.proxies.get("https"), "http://198.51.100.20:1080"
                )
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
