import json
import struct
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from grok_account_manager.api.routers import gateway as gateway_router
from grok_account_manager.core import database as db
from grok_account_manager.gateway import pool
from grok_account_manager.providers.gpt.gateway import upstream as gpt_upstream


def _event_frame(payload: dict) -> bytes:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    total_length = 12 + len(data) + 4
    prelude = struct.pack(">II", total_length, 0)
    prelude_crc = struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)
    body = prelude + prelude_crc + data
    return body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


class MockUpstreamHandler(BaseHTTPRequestHandler):
    requests_log: list[dict] = []

    def log_message(self, *_args) -> None:  # 静默
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.__class__.requests_log.append(
            {"path": self.path, "authorization": self.headers.get("Authorization"), "body": body}
        )
        if self.path.endswith("/responses"):
            self._json(
                {
                    "id": "resp-1",
                    "output": [{"content": [{"type": "output_text", "text": "你好，我是 Grok"}]}],
                    "usage": {"input_tokens": 3, "output_tokens": 5},
                }
            )
            return
        if self.path.endswith("/images/generations"):
            self._json({"data": [{"url": "https://example.test/image.png"}]})
            return
        if self.path.endswith("/videos/generations"):
            self._json({"request_id": "video-1", "status": "processing"})
            return
        if "/generateAssistantResponse" in self.path:
            body = (
                _event_frame({"assistantResponseEvent": {"content": "Kiro 回复"}})
                + _event_frame(
                    {"messageMetadataEvent": {"tokenUsage": {"uncachedInputTokens": 4, "outputTokens": 6}}}
                )
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.amazon.eventstream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"error": {"message": "not found"}}, 404)

    def do_GET(self) -> None:
        self.__class__.requests_log.append({"path": self.path, "authorization": self.headers.get("Authorization")})
        if "/videos/" in self.path:
            self._json({"status": "completed", "video_url": "https://example.test/video.mp4"})
            return
        self._json({"error": {"message": "not found"}}, 404)


class GatewayIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstreamHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "app.db"
        self._env = patch.dict("os.environ", {"KIRO_ENDPOINT_BASE": f"{self.base}/kiro"})
        self._env.start()
        self._patchers = [
            patch.object(db, "DB_PATH", self._db_path),
            patch.object(db, "OUTPUT_DIR", Path(self._tmpdir.name) / "output"),
        ]
        for patcher in self._patchers:
            patcher.start()
        db.init_db()
        pool.reset_state()
        MockUpstreamHandler.requests_log = []

    def tearDown(self) -> None:
        pool.reset_state()
        self._env.stop()
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmpdir.cleanup()

    def _add_account(self, provider: str, credential: dict, export_key: str) -> None:
        db.upsert_account(
            {"id": export_key.rsplit(":", 1)[-1], "exportKey": export_key, "provider": provider, "email": f"{export_key.split(':')[-1]}@example.test"},
            credential,
            Path("database"),
            0,
        )

    @staticmethod
    def _request(path: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [(b"content-type", b"application/json")],
                "query_string": b"",
            }
        )

    def test_grok_chat_image_and_video(self) -> None:
        self._add_account(
            "grok",
            {"access_token": "grok-token", "provider": "grok", "base_url": f"{self.base}/v1"},
            "db:grok:one",
        )

        chat = gateway_router.chat_completions(
            self._request("/v1/chat/completions"),
            {"model": "grok-4.20-auto", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(chat["choices"][0]["message"]["content"], "你好，我是 Grok")
        self.assertEqual(chat["usage"]["prompt_tokens"], 3)

        image = gateway_router.generate_images(
            self._request("/v1/images/generations"),
            {"model": "grok-imagine-image", "prompt": "a cat"},
        )
        self.assertEqual(image["data"][0]["url"], "https://example.test/image.png")

        video = gateway_router.generate_video(
            self._request("/v1/videos/generations"),
            {"model": "grok-imagine-video-1.5", "prompt": "a running cat"},
        )
        self.assertEqual(video["request_id"], "video-1")

        request = Request(
            {"type": "http", "method": "GET", "path": "/v1/videos/video-1", "headers": [], "query_string": b""}
        )
        status = gateway_router.get_video("video-1", request)
        self.assertEqual(status["status"], "completed")

    def test_kiro_chat_uses_event_stream(self) -> None:
        self._add_account(
            "kiro",
            {"access_token": "kiro-token", "provider": "kiro", "region": "us-east-1"},
            "db:kiro:one",
        )
        chat = gateway_router.chat_completions(
            self._request("/v1/chat/completions"),
            {"model": "kiro-claude-sonnet-4", "messages": [{"role": "user", "content": "你好"}]},
        )
        self.assertEqual(chat["choices"][0]["message"]["content"], "Kiro 回复")
        self.assertEqual(chat["usage"]["completion_tokens"], 6)
        kiro_requests = [item for item in MockUpstreamHandler.requests_log if "generateAssistantResponse" in item["path"]]
        self.assertEqual(len(kiro_requests), 1)
        self.assertEqual(kiro_requests[0]["authorization"], "Bearer kiro-token")

    def test_gpt_image_generation_pipeline(self) -> None:
        self._add_account(
            "chatgpt",
            {"access_token": "gpt-token", "provider": "chatgpt"},
            "db:chatgpt:one",
        )

        class FakeStream:
            def iter_lines(self, decode_unicode=False):
                yield 'data: {"content":"![img](data:image/png;base64,ZmFrZQ==)","conversation_id":"c1"}'.encode("utf-8")

            def close(self) -> None:
                pass

        with (
            patch.object(gpt_upstream, "bootstrap", return_value=([], "")),
            patch.object(gpt_upstream, "get_chat_requirements", return_value=gpt_upstream.ChatRequirements(token="t")),
            patch.object(gpt_upstream, "prepare_image_conversation", return_value="conduit"),
            patch.object(gpt_upstream, "start_image_generation", return_value=FakeStream()),
        ):
            image = gateway_router.generate_images(
                self._request("/v1/images/generations"),
                {"model": "gpt-image-1", "prompt": "a cat"},
            )
        self.assertEqual(image["data"][0]["b64_json"], "ZmFrZQ==")

    def test_gateway_returns_503_without_accounts(self) -> None:
        chat = gateway_router.chat_completions(
            self._request("/v1/chat/completions"),
            {"model": "grok-4.20-auto", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(chat.status_code, 503)


if __name__ == "__main__":
    unittest.main()
