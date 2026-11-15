"""Kiro Google 注册流程的离线测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from grok_account_manager.providers.kiro import registration as kiro_registration
from grok_account_manager.providers.kiro.registration import KiroProvider


class FakePage:
    url = "https://app.kiro.dev/home"

    def run_js(self, _script: str) -> bool:
        return True


class FakeSession:
    def __init__(self) -> None:
        self.page = FakePage()

    def open_new_tab(self, _url: str = "") -> FakePage:
        return self.page

    def refresh_page(self) -> FakePage:
        return self.page


class FakeMailbox:
    email = "kiro@example.test"
    password = "password"
    recovery_email = "recovery@example.test"


class FakeMailSource:
    registration_mode = "google"

    def create_mailbox(self, log_callback=None) -> FakeMailbox:
        return FakeMailbox()


class KiroRegistrationTests(unittest.TestCase):
    def test_google_registration_builds_kiro_credential(self) -> None:
        provider = KiroProvider()
        provider.mail_source = FakeMailSource()
        cookies = [
            {"name": "AccessToken", "value": "access", "domain": ".kiro.dev"},
            {"name": "RefreshToken", "value": "refresh", "domain": ".kiro.dev"},
            {"name": "Idp", "value": "Google", "domain": ".kiro.dev"},
        ]
        with (
            patch.object(kiro_registration, "_collect_cookie_candidates", return_value=(cookies, [])),
            patch.object(kiro_registration, "_complete_google_signin"),
        ):
            result = provider.run_round(FakeSession())

        self.assertEqual(result["oauth_status"], "ready")
        self.assertEqual(result["email"], "kiro@example.test")
        credential = result["full_credential"]
        self.assertEqual(credential["accessToken"], "access")
        self.assertEqual(credential["refresh_token"], "refresh")
        self.assertEqual(credential["provider"], "Google")
        self.assertEqual(credential["authMethod"], "social")

    def test_non_google_source_is_rejected(self) -> None:
        provider = KiroProvider()
        provider.mail_source = type("Mail", (), {"registration_mode": "email", "create_mailbox": lambda self, log_callback=None: FakeMailbox()})()
        with self.assertRaisesRegex(RuntimeError, "Google 账号"):
            provider.run_round(FakeSession())


if __name__ == "__main__":
    unittest.main()
