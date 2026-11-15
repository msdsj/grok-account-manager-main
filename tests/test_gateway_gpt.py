import json
import unittest

from grok_account_manager.providers.gpt.gateway import upstream
from grok_account_manager.gateway.errors import GatewayError


class FakeSSEResponse:
    def __init__(self, events):
        self._events = events
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        for event in self._events:
            yield event.encode("utf-8") if isinstance(event, str) else event

    def close(self):
        self.closed = True


class FakeHTTPResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class ChatGPTGatewayTests(unittest.TestCase):
    def test_image_request_body_contains_model_prompt_size_quality_and_n(self):
        plan = upstream.build_image_plan(
            "gpt-image-1",
            {
                "model": "gpt-image-1",
                "prompt": "一只坐在窗台上的猫",
                "n": 2,
                "size": "1024x1536",
                "quality": "high",
                "response_format": "b64_json",
            },
        )
        self.assertEqual(plan.model, "gpt-image-1")
        self.assertEqual(plan.n, 2)
        self.assertEqual(plan.size, "1024x1536")
        self.assertEqual(plan.quality, "high")

        body = upstream.build_conversation_body(plan)
        self.assertEqual(body["model"], "gpt-image-1")
        self.assertEqual(body["system_hints"], ["picture_v2"])
        parts = body["messages"][0]["content"]["parts"]
        self.assertIn("一只坐在窗台上的猫", parts[0])
        self.assertIn("1024x1536", parts[0])
        self.assertIn("high", parts[0])

        prepare_body = upstream.build_prepare_body(plan)
        self.assertEqual(prepare_body["model"], "gpt-image-1")
        self.assertEqual(prepare_body["partial_query"]["content"]["parts"][0], parts[0])

    def test_image_edit_body_uses_uploaded_reference(self):
        plan = upstream.build_image_plan(
            "gpt-image-2",
            {"prompt": "把背景换成海边", "image": "QUJD"},
        )
        self.assertEqual(plan.model, "gpt-image-2")
        self.assertEqual(plan.images, ["QUJD"])

        reference = {
            "file_id": "file-abc123",
            "file_name": "image_1.png",
            "file_size": 3,
            "mime_type": "image/png",
            "width": 1024,
            "height": 1024,
        }
        body = upstream.build_conversation_body(plan, [reference])
        parts = body["messages"][0]["content"]["parts"]
        self.assertEqual(parts[0]["content_type"], "image_asset_pointer")
        self.assertEqual(parts[0]["asset_pointer"], "file-service://file-abc123")
        self.assertEqual(body["messages"][0]["metadata"]["attachments"][0]["id"], "file-abc123")

    def test_sse_image_response_parsing(self):
        events = [
            'data: {"conversation_id":"conv-123","message":{"author":{"role":"assistant"},'
            '"content":{"content_type":"text","parts":["正在生成图片"]}}}',
            'data: {"message":{"author":{"role":"tool"},"metadata":{"async_task_type":"image_gen"},'
            '"content":{"content_type":"multimodal_text","parts":[{"content_type":"image_asset_pointer",'
            '"asset_pointer":"file-service://file-abc123"}]}}}',
            'data: {"v":{"message":{"author":{"role":"tool"},"content":{"parts":'
            '[{"asset_pointer":"sediment://sediment-xyz"}]}}}}',
            'data: {"data":[{"b64_json":"QUJD"}]}',
            "data: [DONE]",
        ]
        response = FakeSSEResponse(events)
        result = upstream.parse_image_stream(response)

        self.assertTrue(response.closed)
        self.assertEqual(result.conversation_id, "conv-123")
        self.assertIn("file-abc123", result.file_ids)
        self.assertIn("sediment-xyz", result.sediment_ids)
        self.assertEqual(result.inline_images, ["QUJD"])
        self.assertEqual(result.text, "正在生成图片")

    def test_sse_data_url_image_is_extracted(self):
        response = FakeSSEResponse(
            [
                'data: {"conversation_id":"conv-9","message":{"author":{"role":"tool"},'
                '"content":{"parts":["data:image/png;base64,QUJDRA=="]}}}',
                "data: [DONE]",
            ]
        )
        result = upstream.parse_image_stream(response)
        self.assertEqual(result.conversation_id, "conv-9")
        self.assertEqual(result.inline_images, ["QUJDRA=="])

    def test_error_status_mapping(self):
        cases = [
            (401, 401, "authentication_error", 401),
            (403, 401, "authentication_error", 403),
            (429, 429, "rate_limit_error", 429),
            (500, 502, "upstream_error", 500),
            (503, 502, "upstream_error", 503),
            (400, 400, "invalid_request_error", 400),
        ]
        for status, expected_status, expected_code, expected_upstream in cases:
            with self.subTest(status=status):
                with self.assertRaises(GatewayError) as context:
                    upstream.raise_for_upstream(FakeHTTPResponse(status, {"detail": "upstream failed"}))
                error = context.exception
                self.assertEqual(error.status_code, expected_status)
                self.assertEqual(error.code, expected_code)
                self.assertEqual(error.upstream_status, expected_upstream)

    def test_missing_prompt_is_rejected_locally(self):
        with self.assertRaises(GatewayError) as context:
            upstream.build_image_plan("gpt-image-1", {"prompt": ""})
        self.assertEqual(context.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
