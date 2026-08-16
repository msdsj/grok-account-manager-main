"""Sub2API admin client compatibility checks."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from grok_account_manager.sinks.sub2api import (
    build_sub2api_oauth_accounts,
    create_sub2api_accounts,
    parse_sub2api_group_ids,
)


class Sub2ApiSinkTests(unittest.TestCase):
    def test_parses_default_group_ids(self) -> None:
        self.assertEqual(parse_sub2api_group_ids("9, 12,9"), [9, 12])
        with self.assertRaisesRegex(ValueError, "数字 ID"):
            parse_sub2api_group_ids("grok")

    def test_builds_grok_oauth_account(self) -> None:
        accounts = build_sub2api_oauth_accounts(
            "grok_build",
            [
                {
                    "email": "user@example.com",
                    "access_token": "access",
                    "refresh_token": "refresh",
                }
            ],
            group_ids=[9],
        )

        self.assertEqual(accounts[0]["platform"], "grok")
        self.assertEqual(accounts[0]["type"], "oauth")
        self.assertEqual(accounts[0]["group_ids"], [9])

    @patch("grok_account_manager.sinks.sub2api.requests.post")
    def test_accepts_current_sub2api_response_envelope(self, post: Mock) -> None:
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = {
            "code": 0,
            "message": "success",
            "data": {
                "success": 1,
                "failed": 0,
                "results": [{"name": "primary", "success": True, "id": 9}],
            },
        }
        post.return_value = response

        result = create_sub2api_accounts(
            base_url="https://fast.example.test/",
            api_key="admin-key",
            accounts=[{"name": "primary"}],
        )

        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 0)
        post.assert_called_once_with(
            "https://fast.example.test/api/v1/admin/accounts/batch",
            headers={"x-api-key": "admin-key", "Content-Type": "application/json"},
            json={"accounts": [{"name": "primary"}]},
            timeout=30,
        )

    @patch("grok_account_manager.sinks.sub2api.requests.post")
    def test_sends_idempotency_key(self, post: Mock) -> None:
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = {
            "code": 0,
            "data": {"success": 1, "failed": 0, "results": []},
        }
        post.return_value = response

        create_sub2api_accounts(
            base_url="https://fast.example.test",
            api_key="admin-key",
            accounts=[{"name": "primary"}],
            idempotency_key="job-1-round-1",
        )

        self.assertEqual(
            post.call_args.kwargs["headers"]["Idempotency-Key"],
            "job-1-round-1",
        )


if __name__ == "__main__":
    unittest.main()
