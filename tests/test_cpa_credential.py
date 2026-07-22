from __future__ import annotations

import base64
import io
import json
import unittest
import zipfile

from grok_account_manager.sinks.cpa_credential import (
    CPA_BASE_URL,
    CPA_REDIRECT_URI,
    CPA_TOKEN_ENDPOINT,
    build_cpa_credential,
    build_cpa_download,
)


def _jwt(claims: dict) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(claims)}.signature"


class CpaCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.issued_at = 1_700_000_000
        self.expires_at = self.issued_at + 21_600
        self.access_token = _jwt(
            {
                "sub": "principal-123",
                "email": "account@example.com",
                "iat": self.issued_at,
                "exp": self.expires_at,
            }
        )
        self.id_token = _jwt(
            {
                "sub": "principal-123",
                "email": "account@example.com",
                "iat": self.issued_at,
                "exp": self.expires_at,
            }
        )
        self.source = {
            "email": "account@example.com",
            "access_token": self.access_token,
            "refresh_token": "refresh-secret",
            "id_token": self.id_token,
            "token_type": "Bearer",
            "token_endpoint": CPA_TOKEN_ENDPOINT,
            "created_at": self.issued_at * 1000,
            "sso_token": "must-not-be-exported",
        }

    def test_builds_cli_proxy_api_xai_shape(self) -> None:
        result = build_cpa_credential(self.source, now=self.issued_at)

        self.assertEqual(result["type"], "xai")
        self.assertEqual(result["auth_kind"], "oauth")
        self.assertEqual(result["access_token"], self.access_token)
        self.assertEqual(result["refresh_token"], "refresh-secret")
        self.assertEqual(result["id_token"], self.id_token)
        self.assertEqual(result["expires_in"], 21_600)
        self.assertEqual(result["expired"], "2023-11-15T04:13:20Z")
        self.assertEqual(result["last_refresh"], "2023-11-14T22:13:20Z")
        self.assertEqual(result["email"], "account@example.com")
        self.assertEqual(result["sub"], "principal-123")
        self.assertEqual(result["base_url"], CPA_BASE_URL)
        self.assertEqual(result["redirect_uri"], CPA_REDIRECT_URI)
        self.assertEqual(result["token_endpoint"], CPA_TOKEN_ENDPOINT)
        self.assertNotIn("sso_token", result)

    def test_rejects_missing_refresh_token(self) -> None:
        source = {**self.source, "refresh_token": None}
        with self.assertRaisesRegex(ValueError, "refresh_token"):
            build_cpa_credential(source, now=self.issued_at)

    def test_single_account_download_is_one_json_object(self) -> None:
        raw, filename, content_type = build_cpa_download([self.source], now=self.issued_at)

        self.assertEqual(filename, "xai-account@example.com.json")
        self.assertTrue(content_type.startswith("application/json"))
        self.assertIsInstance(json.loads(raw), dict)

    def test_multiple_accounts_download_as_individual_zip_files(self) -> None:
        raw, filename, content_type = build_cpa_download(
            [self.source, dict(self.source)],
            now=self.issued_at,
        )

        self.assertEqual(filename, "xai-cpa-credentials-20231114-221320.zip")
        self.assertEqual(content_type, "application/zip")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(
                archive.namelist(),
                ["xai-account@example.com.json", "xai-account@example.com-2.json"],
            )
            self.assertTrue(all(isinstance(json.loads(archive.read(name)), dict) for name in archive.namelist()))


if __name__ == "__main__":
    unittest.main()
