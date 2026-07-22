import time
import unittest
from unittest.mock import patch

from grok_account_manager.grok.account_tester import test_grok_account


class _Response:
    def __init__(self, status_code=200, payload=None, text="") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class AccountTesterTests(unittest.TestCase):
    def test_classifies_grok45_account(self) -> None:
        calls = []

        def fake_post(url, **kwargs):
            calls.append(kwargs["json"]["model"])
            return _Response(200, {"id": "resp"})

        with patch("grok_account_manager.grok.account_tester.requests.post", side_effect=fake_post):
            result, _ = test_grok_account({"email": "a@example.com", "access_token": "token"}, timeout=5)

        self.assertEqual(result["category"], "grok-4.5")
        self.assertTrue(result["baseAvailable"])
        self.assertTrue(result["grok45Available"])
        self.assertEqual(calls[:2], ["grok-4.20-0309-non-reasoning", "grok-4.5"])

    def test_classifies_base_only_when_grok45_fails(self) -> None:
        def fake_post(url, **kwargs):
            if kwargs["json"]["model"] == "grok-4.5":
                return _Response(404, {"error": {"message": "model not found"}})
            return _Response(200, {"id": "resp"})

        with patch("grok_account_manager.grok.account_tester.requests.post", side_effect=fake_post):
            result, _ = test_grok_account({"email": "a@example.com", "access_token": "token"}, timeout=5)

        self.assertEqual(result["category"], "base-only")
        self.assertTrue(result["baseAvailable"])
        self.assertFalse(result["grok45Available"])

    def test_refreshes_expired_access_token(self) -> None:
        def fake_post(url, **kwargs):
            if "oauth2/token" in url:
                return _Response(
                    200,
                    {
                        "access_token": "new-token",
                        "refresh_token": "new-refresh",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    },
                )
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer new-token")
            return _Response(200, {"id": "resp"})

        with patch("grok_account_manager.grok.account_tester.requests.post", side_effect=fake_post):
            result, updated = test_grok_account(
                {
                    "email": "a@example.com",
                    "access_token": "old-token",
                    "refresh_token": "old-refresh",
                    "expires_at": int(time.time()) - 10,
                },
                timeout=5,
            )

        self.assertEqual(result["category"], "grok-4.5")
        self.assertEqual(updated["access_token"], "new-token")
        self.assertEqual(updated["refresh_token"], "new-refresh")


if __name__ == "__main__":
    unittest.main()
