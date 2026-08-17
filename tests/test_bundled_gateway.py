"""Regression tests for the repository-owned gateway runtime."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
import unittest
from unittest.mock import patch

from grok_account_manager.api.services import relay


class BundledGatewayTests(unittest.TestCase):
    def test_gateway_source_is_inside_this_repository(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.assertEqual(relay.BUNDLED_GATEWAY_DIR, project_root / "gateway")
        self.assertTrue((relay.BUNDLED_GATEWAY_DIR / "backend" / "go.mod").is_file())
        self.assertTrue((relay.BUNDLED_GATEWAY_DIR / "Dockerfile").is_file())
        # Local gateway fixes append a descriptive suffix to the vendored
        # upstream commit; keep validating the immutable 40-character base.
        self.assertRegex(relay._gateway_revision(), r"^[0-9a-f]{40}(?:-[A-Za-z0-9._-]+)?$")
        dockerfile = (relay.BUNDLED_GATEWAY_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("FROM grok2api", dockerfile)
        self.assertIn('org.opencontainers.image.title="grok-account-manager-gateway"', dockerfile)

    def test_relay_config_has_no_external_source_path(self) -> None:
        self.assertNotIn("grok2api_path", {field.name for field in fields(relay.RelayConfig)})
        self.assertNotIn("grok2apiPath", relay.RelayConfig.__annotations__)

    def test_container_command_uses_project_owned_name_and_image(self) -> None:
        with patch.object(relay, "_proxy_endpoint_reachable", return_value=True):
            command = relay._grok2api_v2_command(relay.RelayConfig(), Path("/tmp/config.yaml"))
        self.assertIn("grok-account-manager-gateway:local", command)
        self.assertIn("grok-account-manager-gateway-43871", command)
        self.assertNotIn("grok-account-manager-grok2api:local", command)
        self.assertIn("host.docker.internal:host-gateway", command)
        self.assertIn("HTTP_PROXY=http://host.docker.internal:7890", command)
        self.assertIn("NO_PROXY=127.0.0.1,localhost,host.docker.internal", command)
        self.assertIn("no_proxy=127.0.0.1,localhost,host.docker.internal", command)

    def test_host_proxy_is_rewritten_for_container_without_leaking_credentials(self) -> None:
        self.assertEqual(
            relay._container_proxy_url("http://127.0.0.1:7890"),
            "http://host.docker.internal:7890",
        )
        self.assertEqual(
            relay._container_proxy_url("socks5://user:secret@localhost:1080"),
            "socks5://user:secret@host.docker.internal:1080",
        )
        self.assertEqual(
            relay._container_proxy_url("http://[::1]:7890"),
            "http://host.docker.internal:7890",
        )
        self.assertNotIn("secret", relay._mask_proxy_url("socks5://user:secret@127.0.0.1:1080"))

    def test_empty_environment_proxy_disables_default(self) -> None:
        with patch.dict(relay.os.environ, {relay.GATEWAY_PROXY_ENV: ""}, clear=False):
            self.assertEqual(relay._normalize_gateway_proxy(relay.os.environ[relay.GATEWAY_PROXY_ENV]), "")


if __name__ == "__main__":
    unittest.main()
