"""Provider-specific account conversion for the grok2api relay."""

from __future__ import annotations

import unittest

from grok_account_manager.api.services.relay import (
    _credentials_to_v2_build_accounts,
    _credentials_to_v2_console_accounts,
    _credentials_to_v2_web_accounts,
)


class RelayImportTests(unittest.TestCase):
    def test_sso_is_imported_into_web_and_console_but_not_as_build_oauth(self) -> None:
        credentials = [
            {
                "email": "sso@example.com",
                "sso_token": "sso-value",
                "grok_cf_cookies": "CF_CLEARANCE=clearance-value",
                "refresh_token": "refresh-value",
                "access_token": "access-value",
            },
            {
                "email": "oauth@example.com",
                "access_token": "oauth-access",
                "refresh_token": "oauth-refresh",
                "id_token": "oauth-id",
            },
        ]

        web = _credentials_to_v2_web_accounts(credentials)
        console = _credentials_to_v2_console_accounts(credentials)
        build = _credentials_to_v2_build_accounts(credentials)

        self.assertEqual([item["sso_token"] for item in web], ["sso-value"])
        self.assertEqual([item["sso_token"] for item in console], ["sso-value"])
        self.assertEqual(console[0]["cloudflare_cookies"], "CF_CLEARANCE=clearance-value")
        self.assertEqual([item["refresh_token"] for item in build], ["refresh-value", "oauth-refresh"])
        self.assertNotIn("oauth-access", [item["sso_token"] for item in web])
        self.assertNotIn("oauth-access", [item["sso_token"] for item in console])


if __name__ == "__main__":
    unittest.main()
