"""Compatibility checks for the Sub2API account-data export."""

from __future__ import annotations

import json
import unittest

from grok_account_manager.api.routers.proxy import _build_sub2api_accounts, _build_sub2api_download


class Sub2ApiExportTests(unittest.TestCase):
    def test_builds_a_native_sub2api_grok_oauth_backup(self) -> None:
        raw, filename, content_type = _build_sub2api_download(
            "grok_build",
            [
                {
                    "name": "primary",
                    "email": "user@example.com",
                    "user_id": "user-1",
                    "client_id": "client-1",
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "id_token": "id-1",
                    "expires_at": "2026-07-12T12:00:00Z",
                }
            ],
            now=1_700_000_000,
        )

        self.assertEqual(filename, "grok2api-grok-build-sub2api-20231114-221320.json")
        self.assertEqual(content_type, "application/json; charset=utf-8")
        document = json.loads(raw)
        self.assertEqual(document["type"], "sub2api-data")
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["exported_at"], "2023-11-14T22:13:20Z")
        self.assertEqual(document["proxies"], [])
        account = document["accounts"][0]
        self.assertEqual(account["platform"], "grok")
        self.assertEqual(account["type"], "oauth")
        self.assertEqual(account["concurrency"], 1)
        self.assertEqual(account["priority"], 0)
        self.assertTrue(account["auto_pause_on_expired"])
        self.assertEqual(account["credentials"]["access_token"], "access-1")
        self.assertEqual(account["credentials"]["refresh_token"], "refresh-1")
        self.assertEqual(account["credentials"]["client_id"], "client-1")
        self.assertEqual(account["credentials"]["expires_at"], "2026-07-12T12:00:00Z")
        self.assertEqual(account["credentials"]["base_url"], "https://cli-chat-proxy.grok.com/v1")

    def test_rejects_non_oauth_export_sources(self) -> None:
        with self.assertRaisesRegex(ValueError, "仅支持 Grok Build"):
            _build_sub2api_download("grok_web", [])
        with self.assertRaisesRegex(ValueError, "access_token 或 refresh_token"):
            _build_sub2api_download("grok_build", [{"access_token": "access-only"}])

    def test_builds_direct_import_requests_with_default_groups(self) -> None:
        accounts = _build_sub2api_accounts(
            "grok_build",
            [
                {
                    "email": "user@example.com",
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                }
            ],
            group_ids=[9],
        )

        self.assertEqual(accounts[0]["group_ids"], [9])
        self.assertTrue(accounts[0]["confirm_mixed_channel_risk"])
