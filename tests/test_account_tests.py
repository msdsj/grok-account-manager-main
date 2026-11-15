"""Provider-aware account test routing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grok_account_manager.api.services import accounts as account_service
from grok_account_manager.core import database as db


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def chat(self, account, route, payload, *, timeout: int = 180):
        self.calls.append({"action": "chat", "provider": route.provider, "model": route.public_model})
        return (
            {
                "model": route.public_model,
                "choices": [{"message": {"role": "assistant", "content": "测试回复"}}],
            },
            dict(account.credential),
        )

    def image(self, account, route, payload, *, timeout: int = 180):
        self.calls.append({"action": "image", "provider": route.provider, "model": route.public_model})
        return ({"data": [{"url": "https://example.test/image.png"}]}, dict(account.credential))

    def video(self, account, route, payload, *, timeout: int = 180):
        self.calls.append({"action": "video", "provider": route.provider, "model": route.public_model})
        return ({"request_id": "video-1", "status": "processing"}, dict(account.credential))

    def video_status(self, account, video_id, *, timeout: int = 180):
        self.calls.append({"action": "video_status", "video_id": video_id})
        return ({"status": "completed", "video_url": "https://example.test/video.mp4"}, dict(account.credential))


class AccountTestRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patchers = [
            patch.object(db, "DB_PATH", Path(self._tmpdir.name) / "app.db"),
            patch.object(db, "OUTPUT_DIR", Path(self._tmpdir.name) / "output"),
        ]
        for patcher in self._patchers:
            patcher.start()
        db.init_db()
        self.fake = FakeAdapter()
        self._adapter_patchers = [
            patch.object(account_service, "GROK_ADAPTER", self.fake),
            patch.object(account_service, "KIRO_ADAPTER", self.fake),
            patch.object(account_service, "CHATGPT_ADAPTER", self.fake),
        ]
        for patcher in self._adapter_patchers:
            patcher.start()
        self._add_account("grok", "db:grok:one", {"access_token": "grok-token", "provider": "grok"})
        self._add_account("kiro", "db:kiro:one", {"access_token": "kiro-token", "provider": "kiro"})
        self._add_account("chatgpt", "db:chatgpt:one", {"accessToken": "gpt-token", "provider": "chatgpt"})

    def tearDown(self) -> None:
        for patcher in reversed(self._adapter_patchers):
            patcher.stop()
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmpdir.cleanup()

    def _add_account(self, provider: str, export_key: str, credential: dict) -> None:
        db.upsert_account(
            {
                "id": export_key.rsplit(":", 1)[-1],
                "exportKey": export_key,
                "provider": provider,
                "email": f"{provider}@example.test",
            },
            credential,
            Path("database"),
            0,
        )

    def _availability(self, export_key: str) -> dict:
        account = next(item for item in account_service.list_accounts() if item["exportKey"] == export_key)
        return account.get("availability") or {}

    def test_chat_routes_by_provider(self) -> None:
        kiro = account_service.test_account_chat("db:kiro:one", "", [{"role": "user", "content": "你好"}])
        self.assertEqual(kiro["message"]["content"], "测试回复")
        self.assertEqual(self.fake.calls[-1], {"action": "chat", "provider": "kiro", "model": "kiro-auto"})
        self.assertTrue(self._availability("db:kiro:one")["chatAvailable"])

        grok = account_service.test_account_chat(
            "db:grok:one", "grok-4.20-auto", [{"role": "user", "content": "你好"}]
        )
        self.assertEqual(grok["message"]["content"], "测试回复")
        self.assertEqual(
            self.fake.calls[-1],
            {"action": "chat", "provider": "grok", "model": "grok-4.20-auto"},
        )

        with self.assertRaisesRegex(ValueError, "不支持对话测试"):
            account_service.test_account_chat("db:chatgpt:one", "", [{"role": "user", "content": "你好"}])

    def test_image_routes_by_provider(self) -> None:
        grok = account_service.test_account_image("db:grok:one", "", "a cat", 1, "1024x1024")
        self.assertEqual(grok["data"][0]["url"], "https://example.test/image.png")
        self.assertEqual(
            self.fake.calls[-1],
            {"action": "image", "provider": "grok", "model": "grok-imagine-image"},
        )

        gpt = account_service.test_account_image("db:chatgpt:one", "", "a cat", 1, "1024x1024")
        self.assertEqual(gpt["data"][0]["url"], "https://example.test/image.png")
        self.assertEqual(
            self.fake.calls[-1],
            {"action": "image", "provider": "chatgpt", "model": "gpt-image-1"},
        )
        self.assertTrue(self._availability("db:chatgpt:one")["imageAvailable"])

        with self.assertRaisesRegex(ValueError, "不支持生图测试"):
            account_service.test_account_image("db:kiro:one", "", "a cat", 1, "1024x1024")

    def test_video_only_supports_grok(self) -> None:
        result = account_service.test_account_video("db:grok:one", "", "a running cat", 6, "720p", "16:9")
        self.assertEqual(result["video"]["request_id"], "video-1")
        self.assertEqual(
            self.fake.calls[-1],
            {"action": "video", "provider": "grok", "model": "grok-imagine-video-1.5"},
        )
        self.assertTrue(self._availability("db:grok:one")["videoAvailable"])

        status = account_service.test_account_video_status("db:grok:one", "video-1")
        self.assertEqual(status["video"]["status"], "completed")
        self.assertEqual(self.fake.calls[-1], {"action": "video_status", "video_id": "video-1"})

        with self.assertRaisesRegex(ValueError, "仅 Grok 账号支持视频生成测试"):
            account_service.test_account_video("db:kiro:one", "", "a cat", 6, "720p", "16:9")
        with self.assertRaisesRegex(ValueError, "仅 Grok 账号支持视频任务查询"):
            account_service.test_account_video_status("db:chatgpt:one", "video-1")


if __name__ == "__main__":
    unittest.main()
