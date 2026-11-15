import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from grok_account_manager.api.routers import gateway as gateway_router
from grok_account_manager.core import database as db
from grok_account_manager.gateway import catalog
from grok_account_manager.gateway.auth import (
    LOCAL_CONTEXT,
    authenticate,
    create_client_key,
    require_scope,
)
from grok_account_manager.gateway.errors import GatewayError
from grok_account_manager.providers.grok.gateway import upstream
from grok_account_manager.providers.grok.gateway.video import build_video_body
from grok_account_manager.gateway.registry import get_adapter
from grok_account_manager.gateway.pool import PoolAccount, cool_down, pick, reset_state


class GatewayCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "app.db"
        self._output_dir = Path(self._tmpdir.name) / "output"
        self._patchers = [
            patch.object(db, "DB_PATH", self._db_path),
            patch.object(db, "OUTPUT_DIR", self._output_dir),
        ]
        for patcher in self._patchers:
            patcher.start()
        db.init_db()
        reset_state()

    def tearDown(self) -> None:
        reset_state()
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmpdir.cleanup()

    def _add_account(self, export_key: str, email: str, token: str = "access-token") -> None:
        db.upsert_account(
            {
                "id": export_key.rsplit(":", 1)[-1],
                "exportKey": export_key,
                "provider": "grok",
                "email": email,
                "createdAt": 1,
            },
            {"email": email, "access_token": token},
            Path("database"),
            0,
        )

    def test_catalog_resolves_defaults_and_inferred_models(self) -> None:
        catalog.ensure_seeded()

        chat_route = catalog.resolve_model("grok-4.20-auto")
        self.assertEqual(chat_route.provider, "grok")
        self.assertEqual(chat_route.capability, "chat")

        image_route = catalog.resolve_model("grok-imagine-image")
        self.assertEqual(image_route.capability, "image")

        inferred_image = catalog.resolve_model("grok-custom-image")
        self.assertEqual(inferred_image.provider, "grok")
        self.assertEqual(inferred_image.capability, "image")

        gpt_route = catalog.resolve_model("gpt-image-1")
        self.assertEqual(gpt_route.provider, "chatgpt")

        kiro_route = catalog.resolve_model("kiro-claude-sonnet")
        self.assertEqual(kiro_route.provider, "kiro")

        with self.assertRaises(GatewayError) as context:
            catalog.resolve_model("mystery-model")
        self.assertEqual(context.exception.status_code, 404)

    def test_client_key_auth_and_scope(self) -> None:
        self.assertTrue(authenticate().local)

        record = create_client_key("测试密钥", scopes=["chat"], models=["grok-4.20-auto"])
        self.assertTrue(record["key"].startswith("gak_"))

        auth = authenticate(f"Bearer {record['key']}")
        self.assertFalse(auth.local)
        self.assertEqual(auth.key_id, record["id"])
        self.assertEqual(auth.scopes, ("chat",))

        require_scope(auth, "chat", "grok-4.20-auto")
        with self.assertRaises(GatewayError):
            require_scope(auth, "image", "grok-imagine-image")
        with self.assertRaises(GatewayError):
            require_scope(auth, "chat", "grok-4.5")
        with self.assertRaises(GatewayError):
            authenticate("Bearer gak_invalid")

    def test_pool_selection_skips_cooled_down_accounts(self) -> None:
        self._add_account("db:grok:a", "a@example.test")
        self._add_account("db:grok:b", "b@example.test")

        accounts = pick("grok", "chat")
        self.assertIsNotNone(accounts)
        first_key = accounts.export_key
        cool_down(first_key, 60, upstream_status=429)

        seen = {pick("grok", "chat", exclude={first_key}).export_key}
        self.assertEqual(seen, {key for key in {"db:grok:a", "db:grok:b"} if key != first_key})

        self.assertIsNone(pick("grok", "chat", exclude={"db:grok:a", "db:grok:b"}))

    def test_chat_body_and_completion_conversion(self) -> None:
        body = upstream.build_chat_body(
            "grok-4.20-auto",
            {
                "messages": [
                    {"role": "developer", "content": "你是助手"},
                    {"role": "user", "content": [{"type": "text", "text": "你好"}]},
                ],
                "max_tokens": 128,
                "temperature": 0.2,
            },
            stream=False,
        )
        self.assertEqual(body["model"], "grok-4.20-auto")
        self.assertEqual(body["input"][0], {"role": "system", "content": "你是助手"})
        self.assertEqual(body["input"][1], {"role": "user", "content": "你好"})
        self.assertEqual(body["max_output_tokens"], 128)
        self.assertAlmostEqual(body["temperature"], 0.2)
        self.assertFalse(body["stream"])

        payload = {
            "id": "resp-1",
            "output": [{"content": [{"type": "output_text", "text": "世界"}]}],
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }
        completion = upstream.completion_payload(payload, "grok-4.20-auto")
        self.assertEqual(completion["choices"][0]["message"]["content"], "世界")
        self.assertEqual(completion["usage"]["prompt_tokens"], 3)
        self.assertEqual(completion["usage"]["completion_tokens"], 4)
        self.assertEqual(completion["usage"]["total_tokens"], 7)

    def test_stream_events_are_converted_to_openai_chunks(self) -> None:
        events = [
            'data: {"type":"response.output_text.delta","delta":"你"}'.encode("utf-8"),
            'data: {"type":"response.output_text.delta","delta":"好"}'.encode("utf-8"),
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":2}}}',
            b"data: [DONE]",
        ]

        class FakeResponse:
            def iter_lines(self, decode_unicode=False):
                return iter(events)

            def close(self) -> None:
                pass

        usage: dict[str, int] = {}
        chunks = list(upstream.iter_openai_chunks(FakeResponse(), public_model="grok-4.20-auto", usage_out=usage))

        contents = []
        for chunk in chunks:
            if not chunk.startswith("data: {"):
                continue
            data = json.loads(chunk[len("data: ") :])
            content = data["choices"][0]["delta"].get("content")
            if content:
                contents.append(content)
        self.assertEqual("".join(contents), "你好")
        self.assertTrue(chunks[-1].startswith("data: [DONE]"))
        self.assertEqual(usage, {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})

    def test_video_body_maps_image_and_references(self) -> None:
        body = build_video_body(
            "grok-imagine-video-1.5-preview",
            {
                "prompt": "a running cat",
                "image": "https://example.test/frame.png",
                "reference_images": [{"url": "https://example.test/ref.png"}],
                "duration": 6,
                "resolution": "720p",
            },
        )
        self.assertEqual(body["model"], "grok-imagine-video-1.5-preview")
        self.assertEqual(body["image"], {"url": "https://example.test/frame.png"})
        self.assertEqual(body["reference_images"], [{"url": "https://example.test/ref.png"}])
        self.assertEqual(body["duration"], 6)
        self.assertEqual(body["resolution"], "720p")

        with self.assertRaises(GatewayError):
            build_video_body("grok-imagine-video", {})

    def test_registry_exposes_all_three_providers(self) -> None:
        self.assertEqual(get_adapter("grok").provider, "grok")
        self.assertEqual(get_adapter("kiro").provider, "kiro")
        self.assertEqual(get_adapter("chatgpt").provider, "chatgpt")
        with self.assertRaises(GatewayError):
            get_adapter("unknown")

    def test_image_endpoint_uses_chatgpt_adapter(self) -> None:
        account = PoolAccount(
            export_key="db:chatgpt:a",
            account_id="a",
            email="a@example.test",
            provider="chatgpt",
            credential={"access_token": "token"},
            account={"id": "a"},
        )

        class FakeAdapter:
            def image(self, pool_account, route, payload, *, timeout=180):
                return {"created": 1, "data": [{"b64_json": "ZmFrZQ=="}]}, {}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/images/generations",
                "headers": [(b"content-type", b"application/json")],
                "query_string": b"",
            }
        )
        payload = {"model": "gpt-image-1", "prompt": "a cat", "n": 1}
        with patch.object(gateway_router, "pick", return_value=account), patch.object(
            gateway_router, "get_adapter", return_value=FakeAdapter()
        ):
            result = gateway_router.generate_images(request, payload)

        self.assertEqual(result["data"][0]["b64_json"], "ZmFrZQ==")
        records = db.list_gateway_requests(provider="chatgpt")
        self.assertEqual(records["total"], 1)
        self.assertEqual(records["items"][0]["capability"], "image")

    def test_chat_endpoint_uses_adapter_and_records_audit(self) -> None:
        self._add_account("db:grok:a", "a@example.test")
        account = PoolAccount(
            export_key="db:grok:a",
            account_id="a",
            email="a@example.test",
            provider="grok",
            credential={"access_token": "token"},
            account={"id": "a"},
        )

        class FakeAdapter:
            def chat(self, pool_account, route, payload, *, timeout=180):
                self.assertions = (pool_account.export_key, route.public_model, timeout)
                return {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "model": route.public_model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }, {}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [(b"content-type", b"application/json")],
                "query_string": b"",
            }
        )
        payload = {"model": "grok-4.20-auto", "messages": [{"role": "user", "content": "hi"}]}
        with patch.object(gateway_router, "pick", return_value=account), patch.object(
            gateway_router, "get_adapter", return_value=FakeAdapter()
        ):
            result = gateway_router.chat_completions(request, payload)

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        records = db.list_gateway_requests(limit=10)
        self.assertEqual(records["total"], 1)
        self.assertEqual(records["items"][0]["status"], "success")
        self.assertEqual(records["items"][0]["accountId"], "a")
        self.assertEqual(records["items"][0]["provider"], "grok")

    def test_chat_endpoint_rejects_disabled_capability(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [],
                "query_string": b"",
            }
        )
        response = gateway_router.chat_completions(
            request,
            {"model": "grok-imagine-image", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
