import time
import unittest
from unittest.mock import patch

from grok_account_manager.grok.account_tester import generate_grok_image, send_grok_chat, test_grok_account


class _Response:
    def __init__(self, status_code=200, payload=None, text="") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class AccountTesterTests(unittest.TestCase):
    def test_classifies_cli45_account(self) -> None:
        calls = []
        urls = []
        headers = []

        def fake_post(url, **kwargs):
            urls.append(url)
            headers.append(kwargs["headers"])
            calls.append(kwargs["json"]["model"])
            return _Response(200, {"id": "resp"})

        def fake_get(url, **kwargs):
            return _Response(200, {"data": []})

        with (
            patch("grok_account_manager.grok.account_tester.requests.post", side_effect=fake_post),
            patch("grok_account_manager.grok.account_tester.requests.get", side_effect=fake_get),
        ):
            result, _ = test_grok_account({"email": "a@example.com", "access_token": "token"}, timeout=5)

        self.assertEqual(result["category"], "cli-4.5")
        self.assertTrue(result["baseAvailable"])
        self.assertTrue(result["cli45Available"])
        self.assertTrue(result["grok45Available"])
        self.assertEqual(calls[:2], ["grok-4.20-0309-non-reasoning", "grok-4.5"])
        self.assertEqual(urls[0], "https://cli-chat-proxy.grok.com/v1/responses")
        self.assertEqual(headers[0]["X-Grok-Client-Version"], "0.2.93")

    def test_classifies_base_only_when_grok45_fails(self) -> None:
        def fake_post(url, **kwargs):
            if kwargs["json"]["model"] == "grok-4.5":
                return _Response(404, {"error": {"message": "model not found"}})
            return _Response(200, {"id": "resp"})

        def fake_get(url, **kwargs):
            return _Response(200, {"data": []})

        with (
            patch("grok_account_manager.grok.account_tester.requests.post", side_effect=fake_post),
            patch("grok_account_manager.grok.account_tester.requests.get", side_effect=fake_get),
        ):
            result, _ = test_grok_account({"email": "a@example.com", "access_token": "token"}, timeout=5)

        self.assertEqual(result["category"], "base-only")
        self.assertTrue(result["baseAvailable"])
        self.assertFalse(result["cli45Available"])
        self.assertFalse(result["grok45Available"])

    def test_detects_image_model_from_model_list(self) -> None:
        def fake_post(url, **kwargs):
            if kwargs["json"]["model"] == "grok-4.5":
                return _Response(404, {"error": {"message": "model not found"}})
            return _Response(200, {"id": "resp"})

        def fake_get(url, **kwargs):
            return _Response(200, {"data": [{"id": "grok-imagine-image", "object": "model"}]})

        with (
            patch("grok_account_manager.grok.account_tester.requests.post", side_effect=fake_post),
            patch("grok_account_manager.grok.account_tester.requests.get", side_effect=fake_get),
        ):
            result, _ = test_grok_account({"email": "a@example.com", "access_token": "token"}, timeout=5)

        self.assertEqual(result["category"], "chat-image")
        self.assertTrue(result["imageAvailable"])
        self.assertEqual(result["imageModel"], "grok-imagine-image")
        self.assertEqual(result["imageSource"], "model-list")

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

        def fake_get(url, **kwargs):
            return _Response(200, {"data": []})

        with (
            patch("grok_account_manager.grok.account_tester.requests.post", side_effect=fake_post),
            patch("grok_account_manager.grok.account_tester.requests.get", side_effect=fake_get),
        ):
            result, updated = test_grok_account(
                {
                    "email": "a@example.com",
                    "access_token": "old-token",
                    "refresh_token": "old-refresh",
                    "expires_at": int(time.time()) - 10,
                },
                timeout=5,
            )

        self.assertEqual(result["category"], "cli-4.5")
        self.assertEqual(updated["access_token"], "new-token")
        self.assertEqual(updated["refresh_token"], "new-refresh")

    def test_send_chat_refreshes_rt_and_uses_cli_gateway(self) -> None:
        seen = []

        def fake_post(url, **kwargs):
            seen.append((url, kwargs))
            if "oauth2/token" in url:
                return _Response(200, {"access_token": "fresh-token", "expires_in": 3600})
            self.assertEqual(url, "https://cli-chat-proxy.grok.com/v1/responses")
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fresh-token")
            self.assertEqual(kwargs["headers"]["X-Grok-Client-Mode"], "interactive")
            self.assertEqual(kwargs["json"]["model"], "grok-4.20-auto")
            return _Response(
                200,
                {"output": [{"content": [{"type": "output_text", "text": "chat ok"}]}]},
            )

        with patch("grok_account_manager.grok.account_tester.requests.post", side_effect=fake_post):
            result, updated = send_grok_chat(
                {"email": "a@example.com", "refresh_token": "rt"},
                model="grok-4.20-auto",
                messages=[{"role": "user", "content": "ping"}],
                timeout=5,
            )

        self.assertEqual(result["content"], "chat ok")
        self.assertEqual(updated["access_token"], "fresh-token")
        self.assertEqual(len(seen), 2)

    def test_send_chat_does_not_force_refresh_when_access_token_is_valid(self) -> None:
        seen = []

        def fake_post(url, **kwargs):
            seen.append((url, kwargs))
            self.assertNotIn("oauth2/token", url)
            self.assertEqual(url, "https://cli-chat-proxy.grok.com/v1/responses")
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer valid-token")
            return _Response(
                200,
                {"output": [{"content": [{"type": "output_text", "text": "chat ok"}]}]},
            )

        with patch("grok_account_manager.grok.account_tester.requests.post", side_effect=fake_post):
            result, updated = send_grok_chat(
                {
                    "email": "a@example.com",
                    "access_token": "valid-token",
                    "refresh_token": "rt",
                    "expires_at": int(time.time()) + 3600,
                },
                model="grok-4.5",
                messages=[{"role": "user", "content": "ping"}],
                timeout=5,
            )

        self.assertEqual(result["content"], "chat ok")
        self.assertEqual(updated["access_token"], "valid-token")
        self.assertEqual(len(seen), 1)

    def test_generate_image_refreshes_rt_and_uses_media_api(self) -> None:
        def fake_post(url, **kwargs):
            if "oauth2/token" in url:
                return _Response(200, {"access_token": "fresh-token", "expires_in": 3600})
            self.assertEqual(url, "https://api.x.ai/v1/images/generations")
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fresh-token")
            self.assertNotIn("X-Grok-Client-Version", kwargs["headers"])
            self.assertEqual(kwargs["json"]["model"], "grok-imagine-image")
            self.assertNotIn("size", kwargs["json"])
            return _Response(200, {"data": [{"url": "https://example.com/image.png"}]})

        with patch("grok_account_manager.grok.account_tester.requests.post", side_effect=fake_post):
            result, updated = generate_grok_image(
                {"email": "a@example.com", "refresh_token": "rt"},
                model="grok-imagine-image",
                prompt="draw",
                size="1024x1024",
                timeout=5,
            )

        self.assertEqual(result["data"][0]["url"], "https://example.com/image.png")
        self.assertEqual(updated["access_token"], "fresh-token")


if __name__ == "__main__":
    unittest.main()
