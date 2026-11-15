import json
import struct
import unittest
import zlib

from grok_account_manager.gateway.catalog import ModelRoute
from grok_account_manager.gateway.errors import GatewayError
from grok_account_manager.providers.kiro.gateway import eventstream, translator, upstream
from grok_account_manager.providers.kiro.models import (
    CLAUDE_3_7_SONNET_MODEL_ID,
    CLAUDE_OPUS_4_MODEL_ID,
    CLAUDE_SONNET_4_5_MODEL_ID,
    DEFAULT_MODEL_ID,
    map_model_id,
)


def _header(name: str, value: str) -> bytes:
    raw_name = name.encode("utf-8")
    raw_value = value.encode("utf-8")
    return bytes([len(raw_name)]) + raw_name + bytes([7]) + struct.pack(">H", len(raw_value)) + raw_value


def _frame(event_type: str, payload: dict) -> bytes:
    headers = _header(":event-type", event_type) + _header(":content-type", "application/json")
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    total_length = 12 + len(headers) + len(payload_bytes) + 4
    prelude = struct.pack(">II", total_length, len(headers))
    prelude_crc = struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + prelude_crc + headers + payload_bytes
    message_crc = struct.pack(">I", zlib.crc32(message) & 0xFFFFFFFF)
    return message + message_crc


def _fixture() -> bytes:
    return b"".join(
        [
            _frame("assistantResponseEvent", {"content": "你"}),
            _frame("assistantResponseEvent", {"content": "好"}),
            _frame(
                "messageMetadataEvent",
                {
                    "tokenUsage": {
                        "uncachedInputTokens": 10,
                        "cacheReadInputTokens": 2,
                        "cacheWriteInputTokens": 1,
                        "outputTokens": 5,
                        "totalTokens": 18,
                    }
                },
            ),
        ]
    )


class FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size: int = 8192):
        return iter(self._chunks)

    def close(self) -> None:
        self.closed = True


class KiroEventStreamTests(unittest.TestCase):
    def test_eventstream_decodes_text_and_usage_frames(self) -> None:
        data = _fixture()
        messages = list(eventstream.iter_messages([data[:9], data[9:37], data[37:]]))

        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0].event_type, "assistantResponseEvent")
        self.assertEqual(messages[0].json()["content"], "你")
        self.assertEqual(messages[1].json()["content"], "好")
        usage = messages[2].json()["tokenUsage"]
        self.assertEqual(usage["uncachedInputTokens"], 10)
        self.assertEqual(usage["outputTokens"], 5)

    def test_eventstream_rejects_broken_crc(self) -> None:
        broken = bytearray(_frame("assistantResponseEvent", {"content": "x"}))
        broken[-1] ^= 0xFF
        with self.assertRaises(eventstream.EventStreamError):
            list(eventstream.iter_messages([bytes(broken)]))


class KiroTranslatorTests(unittest.TestCase):
    def test_openai_messages_become_conversation_state(self) -> None:
        payload = {
            "messages": [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": [{"type": "text", "text": "你好"}]},
                {"role": "assistant", "content": "你好呀"},
                {"role": "user", "content": [{"type": "text", "text": "看图"}]},
            ],
            "max_tokens": 128,
            "temperature": 0.2,
        }
        body = translator.build_payload("kiro-claude-sonnet-4.5", payload, profile_arn="arn:aws:test")

        state = body["conversationState"]
        current = state["currentMessage"]["userInputMessage"]
        self.assertEqual(current["content"], "看图")
        self.assertEqual(current["modelId"], CLAUDE_SONNET_4_5_MODEL_ID)
        self.assertEqual(current["origin"], "AI_EDITOR")
        self.assertEqual(body["profileArn"], "arn:aws:test")
        self.assertEqual(body["inferenceConfig"], {"maxTokens": 128, "temperature": 0.2})

        history = state["history"]
        self.assertEqual(history[0]["userInputMessage"]["content"], "你是助手")
        self.assertEqual(history[1]["assistantResponseMessage"]["content"], translator.SYSTEM_ACKNOWLEDGEMENT)
        self.assertEqual(history[2]["userInputMessage"]["content"], "你好")
        self.assertEqual(history[3]["assistantResponseMessage"]["content"], "你好呀")

    def test_assistant_last_message_uses_continue(self) -> None:
        body = translator.build_payload(
            "kiro-auto",
            {"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]},
        )
        self.assertEqual(body["conversationState"]["currentMessage"]["userInputMessage"]["content"], "Continue.")
        self.assertEqual(body["conversationState"]["currentMessage"]["userInputMessage"]["modelId"], DEFAULT_MODEL_ID)

    def test_profile_arn_decision(self) -> None:
        self.assertEqual(translator.resolve_profile_arn({"profileArn": "arn:real"}), "arn:real")
        self.assertEqual(translator.resolve_profile_arn({}), translator.BUILDER_ID_PROFILE_ARN)
        self.assertEqual(
            translator.resolve_profile_arn({"authMethod": "social"}),
            translator.SOCIAL_PROFILE_ARN,
        )
        enterprise = translator.resolve_profile_arn({"authMethod": "external_idp", "region": "eu-west-1"})
        self.assertTrue(enterprise.startswith("arn:aws:codewhisperer:eu-central-1:"))


class KiroStreamTests(unittest.TestCase):
    def test_stream_chunks_emit_openai_sse_with_done(self) -> None:
        route = ModelRoute(
            public_model="kiro-claude-sonnet-4",
            provider="kiro",
            upstream_model="CLAUDE_SONNET_4_20250514_V1_0",
            capability="chat",
            config={},
        )
        data = _fixture()
        response = FakeResponse([data[:11], data[11:42], data[42:]])
        usage: dict[str, int] = {}

        chunks = list(upstream.iter_openai_chunks(response, public_model=route.public_model, usage_out=usage))

        content = []
        finish_reasons = []
        for chunk in chunks:
            if not chunk.startswith("data: {"):
                continue
            data_payload = json.loads(chunk[len("data: ") :])
            choice = data_payload["choices"][0]
            if choice["delta"].get("content"):
                content.append(choice["delta"]["content"])
            finish_reasons.append(choice["finish_reason"])
        self.assertEqual("".join(content), "你好")
        self.assertIn("stop", finish_reasons)
        self.assertEqual(chunks[-1], "data: [DONE]\n\n")
        self.assertEqual(usage, {"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18})
        self.assertTrue(response.closed)

    def test_stream_error_event_raises_gateway_error(self) -> None:
        response = FakeResponse([_frame("invalidStateEvent", {"_type": "ValidationException", "message": "bad"})])
        with self.assertRaises(GatewayError) as context:
            list(upstream.iter_openai_chunks(response, public_model="kiro-auto", usage_out={}))
        self.assertEqual(context.exception.status_code, 502)
        self.assertIn("bad", context.exception.message)


class KiroModelMappingTests(unittest.TestCase):
    def test_required_model_aliases(self) -> None:
        cases = {
            "kiro-auto": DEFAULT_MODEL_ID,
            "kiro-claude-sonnet-4": DEFAULT_MODEL_ID,
            "kiro-claude-sonnet-4.5": CLAUDE_SONNET_4_5_MODEL_ID,
            "kiro-claude-opus-4": CLAUDE_OPUS_4_MODEL_ID,
            "kiro-claude-3.7-sonnet": CLAUDE_3_7_SONNET_MODEL_ID,
            "claude-sonnet-4-5": CLAUDE_SONNET_4_5_MODEL_ID,
        }
        for public_model, expected in cases.items():
            with self.subTest(public_model=public_model):
                self.assertEqual(map_model_id(public_model), expected)

    def test_unknown_and_internal_models(self) -> None:
        self.assertEqual(map_model_id("kiro-claude-unknown"), DEFAULT_MODEL_ID)
        self.assertEqual(map_model_id("claude-foo"), DEFAULT_MODEL_ID)
        self.assertEqual(map_model_id(""), DEFAULT_MODEL_ID)
        self.assertEqual(map_model_id("CLAUDE_SONNET_4_20250514_V1_0"), "CLAUDE_SONNET_4_20250514_V1_0")


if __name__ == "__main__":
    unittest.main()
