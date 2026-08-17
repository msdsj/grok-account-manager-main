"""Regression tests for the repository-owned gateway runtime."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
import unittest

from grok_account_manager.api.services import relay


class BundledGatewayTests(unittest.TestCase):
    def test_gateway_source_is_inside_this_repository(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.assertEqual(relay.BUNDLED_GATEWAY_DIR, project_root / "gateway")
        self.assertTrue((relay.BUNDLED_GATEWAY_DIR / "backend" / "go.mod").is_file())
        self.assertTrue((relay.BUNDLED_GATEWAY_DIR / "Dockerfile").is_file())
        self.assertRegex(relay._gateway_revision(), r"^[0-9a-f]{40}$")
        dockerfile = (relay.BUNDLED_GATEWAY_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("FROM grok2api", dockerfile)
        self.assertIn('org.opencontainers.image.title="grok-account-manager-gateway"', dockerfile)

    def test_relay_config_has_no_external_source_path(self) -> None:
        self.assertNotIn("grok2api_path", {field.name for field in fields(relay.RelayConfig)})
        self.assertNotIn("grok2apiPath", relay.RelayConfig.__annotations__)

    def test_container_command_uses_project_owned_name_and_image(self) -> None:
        command = relay._grok2api_v2_command(relay.RelayConfig(), Path("/tmp/config.yaml"))
        self.assertIn("grok-account-manager-gateway:local", command)
        self.assertIn("grok-account-manager-gateway-43871", command)
        self.assertNotIn("grok-account-manager-grok2api:local", command)


if __name__ == "__main__":
    unittest.main()
