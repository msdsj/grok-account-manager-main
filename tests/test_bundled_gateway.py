"""Regression tests for official-image selection and the bundled fallback."""

from __future__ import annotations

from dataclasses import fields
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from grok_account_manager.api.services import relay


OFFICIAL_DIGEST = f"ghcr.io/chenyme/grok2api@sha256:{'a' * 64}"


def _manager() -> relay.RelayManager:
    manager = object.__new__(relay.RelayManager)
    manager._lock = threading.RLock()
    manager._start_lock = threading.Lock()
    manager._process = None
    manager._config = relay.RelayConfig()
    manager._runtime_image = relay._RuntimeImage(
        reference=relay.BUNDLED_V2_IMAGE,
        source="bundled",
        acquisition="pending",
        requested_reference=relay.OFFICIAL_V2_IMAGE,
    )
    return manager


def _completed(args: list[str], returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")


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

    def test_bundled_compose_cannot_overwrite_the_official_candidate_tag(self) -> None:
        compose = (relay.BUNDLED_GATEWAY_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("GROK_ACCOUNT_MANAGER_BUNDLED_GATEWAY_IMAGE", compose)
        self.assertNotIn('image: "${GROK_ACCOUNT_MANAGER_GATEWAY_IMAGE', compose)

    def test_relay_config_has_no_external_source_path(self) -> None:
        self.assertNotIn("grok2api_path", {field.name for field in fields(relay.RelayConfig)})
        self.assertNotIn("grok2apiPath", relay.RelayConfig.__annotations__)

    def test_container_command_uses_selected_digest_and_image_defaults(self) -> None:
        with patch.object(relay, "_proxy_endpoint_reachable", return_value=True):
            command = relay._grok2api_v2_command(
                relay.RelayConfig(),
                Path("/tmp/config.yaml"),
                image=OFFICIAL_DIGEST,
            )
        self.assertEqual(command[-1], OFFICIAL_DIGEST)
        self.assertIn("grok-account-manager-gateway-43871", command)
        self.assertIn("127.0.0.1:43871:8000", command)
        self.assertIn("host.docker.internal:host-gateway", command)
        self.assertIn("HTTP_PROXY=http://host.docker.internal:7890", command)
        self.assertIn("NO_PROXY=127.0.0.1,localhost,host.docker.internal", command)
        self.assertIn("no_proxy=127.0.0.1,localhost,host.docker.internal", command)
        self.assertNotIn("/app/grok2api", command)
        self.assertEqual(command[command.index("--pull") + 1], "never")

    def test_runtime_prefers_pulled_official_digest(self) -> None:
        manager = _manager()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(relay, "LOG_PATH", Path(temp_dir) / "relay.log"),
            patch.object(relay.shutil, "which", return_value="/usr/bin/docker"),
            patch.object(manager, "_ensure_v2_config"),
            patch.object(relay, "_resolved_image_digest", return_value=OFFICIAL_DIGEST),
            patch.object(relay, "_smoke_test_official_image") as smoke,
            patch.object(
                relay.subprocess,
                "run",
                side_effect=[
                    _completed(["docker", "info"]),
                    _completed(["docker", "pull", relay.OFFICIAL_V2_IMAGE]),
                ],
            ) as run,
        ):
            selected = manager._ensure_v2_runtime(relay.BUNDLED_GATEWAY_DIR)

        self.assertEqual(selected.reference, OFFICIAL_DIGEST)
        self.assertEqual(selected.source, "official")
        self.assertEqual(selected.acquisition, "pulled")
        self.assertEqual(selected.digest, OFFICIAL_DIGEST)
        self.assertEqual(selected.fallback_reason, "")
        smoke.assert_called_once_with(OFFICIAL_DIGEST)
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "pull", relay.OFFICIAL_V2_IMAGE])

    def test_pull_failure_uses_validated_cached_digest(self) -> None:
        manager = _manager()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(relay, "LOG_PATH", Path(temp_dir) / "relay.log"),
            patch.object(relay.shutil, "which", return_value="/usr/bin/docker"),
            patch.object(manager, "_ensure_v2_config"),
            patch.object(relay, "_resolved_image_digest", return_value=OFFICIAL_DIGEST),
            patch.object(relay, "_smoke_test_official_image") as smoke,
            patch.object(
                relay.subprocess,
                "run",
                side_effect=[
                    _completed(["docker", "info"]),
                    _completed(["docker", "pull", relay.OFFICIAL_V2_IMAGE], returncode=1),
                ],
            ),
        ):
            selected = manager._ensure_v2_runtime(relay.BUNDLED_GATEWAY_DIR)

        self.assertEqual(selected.reference, OFFICIAL_DIGEST)
        self.assertEqual(selected.acquisition, "cache")
        self.assertIn("拉取失败", selected.fallback_reason)
        smoke.assert_called_once_with(OFFICIAL_DIGEST)

    def test_missing_official_cache_falls_back_to_bundled_cache(self) -> None:
        manager = _manager()
        revision = "b" * 40
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(relay, "LOG_PATH", Path(temp_dir) / "relay.log"),
            patch.object(relay.shutil, "which", return_value="/usr/bin/docker"),
            patch.object(manager, "_ensure_v2_config"),
            patch.object(relay, "_resolved_image_digest", return_value=""),
            patch.object(relay, "_gateway_revision", return_value=revision),
            patch.object(relay, "_image_revision", return_value=revision),
            patch.object(relay, "_smoke_test_official_image") as smoke,
            patch.object(
                relay.subprocess,
                "run",
                side_effect=[
                    _completed(["docker", "info"]),
                    _completed(["docker", "pull", relay.OFFICIAL_V2_IMAGE], returncode=1),
                ],
            ),
        ):
            selected = manager._ensure_v2_runtime(relay.BUNDLED_GATEWAY_DIR)

        self.assertEqual(selected.reference, relay.BUNDLED_V2_IMAGE)
        self.assertEqual(selected.source, "bundled")
        self.assertEqual(selected.acquisition, "cache")
        self.assertIn("本地没有可用的官方镜像缓存", selected.fallback_reason)
        smoke.assert_not_called()

    def test_failed_smoke_probe_builds_bundled_image(self) -> None:
        manager = _manager()
        revision = "c" * 40
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(relay, "LOG_PATH", Path(temp_dir) / "relay.log"),
            patch.object(relay.shutil, "which", return_value="/usr/bin/docker"),
            patch.object(manager, "_ensure_v2_config"),
            patch.object(relay, "_resolved_image_digest", return_value=OFFICIAL_DIGEST),
            patch.object(relay, "_gateway_revision", return_value=revision),
            patch.object(relay, "_image_revision", return_value=""),
            patch.object(relay, "_smoke_test_official_image", side_effect=RuntimeError("登录 API 不兼容")),
            patch.object(
                relay.subprocess,
                "run",
                side_effect=[
                    _completed(["docker", "info"]),
                    _completed(["docker", "pull", relay.OFFICIAL_V2_IMAGE]),
                    _completed(["docker", "build"]),
                ],
            ) as run,
        ):
            selected = manager._ensure_v2_runtime(relay.BUNDLED_GATEWAY_DIR)

        self.assertEqual(selected.reference, relay.BUNDLED_V2_IMAGE)
        self.assertEqual(selected.acquisition, "built")
        self.assertIn("隔离探针失败", selected.fallback_reason)
        build_command = run.call_args_list[2].args[0]
        self.assertEqual(build_command[:2], ["docker", "build"])
        self.assertIn(relay.BUNDLED_V2_IMAGE, build_command)

    def test_snapshot_reports_requested_digest_source_and_fallback(self) -> None:
        manager = _manager()
        manager._runtime_image = relay._RuntimeImage(
            reference=OFFICIAL_DIGEST,
            source="official",
            acquisition="cache",
            requested_reference=relay.OFFICIAL_V2_IMAGE,
            digest=OFFICIAL_DIGEST,
            fallback_reason="pull failed; validated cache",
        )
        with patch.object(
            manager,
            "status",
            return_value={"running": False, "managed": False, "healthy": False},
        ):
            config = manager.snapshot()["config"]

        self.assertEqual(config["imageRequested"], relay.OFFICIAL_V2_IMAGE)
        self.assertEqual(config["image"], OFFICIAL_DIGEST)
        self.assertEqual(config["imageDigest"], OFFICIAL_DIGEST)
        self.assertEqual(config["imageSource"], "official")
        self.assertEqual(config["source"], "official")
        self.assertEqual(config["fallbackReason"], "pull failed; validated cache")

    def test_gateway_image_environment_override_is_reported(self) -> None:
        override = "ghcr.io/chenyme/grok2api:v3.2.0"
        with patch.dict(relay.os.environ, {relay.GATEWAY_IMAGE_ENV: override}):
            self.assertEqual(relay._requested_gateway_image(), override)

    def test_explicit_digest_is_accepted_after_local_inspect(self) -> None:
        with patch.object(
            relay.subprocess,
            "run",
            return_value=_completed(["docker", "image", "inspect"], stdout="null\n"),
        ):
            self.assertEqual(relay._resolved_image_digest(OFFICIAL_DIGEST), OFFICIAL_DIGEST)

    def test_smoke_config_uses_only_stable_upstream_sections(self) -> None:
        config = relay._render_smoke_config("temporary-password")
        self.assertIn('listen: "0.0.0.0:8000"', config)
        self.assertIn("bootstrapAdmin:", config)
        self.assertIn("database:\n  driver: sqlite", config)
        self.assertIn("media:\n  driver: local", config)
        self.assertNotIn("qualityGuard", config)
        self.assertNotIn("autoAssign", config)

    @unittest.skipUnless(os.name == "posix", "private mode assertions require POSIX")
    def test_official_config_preserves_secrets_and_strips_local_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "bundled.yaml"
            destination = root / "official.yaml"
            source.write_text(
                "secrets:\n"
                '  jwtSecret: "existing-jwt-secret-that-is-long-enough"\n'
                '  credentialEncryptionKey: "existing-base64-encryption-key="\n'
                "database:\n"
                "  driver: sqlite\n"
                "qualityGuard:\n"
                "  enabled: true\n"
                "routing:\n"
                "  autoAssignMaxNodeShare: 4\n",
                encoding="utf-8",
            )

            relay._write_official_v2_config(
                source,
                destination,
                admin_password="existing-admin-password",
            )

            rendered = destination.read_text(encoding="utf-8")
            self.assertIn("existing-jwt-secret-that-is-long-enough", rendered)
            self.assertIn("existing-base64-encryption-key=", rendered)
            self.assertIn("existing-admin-password", rendered)
            self.assertNotIn("qualityGuard", rendered)
            self.assertNotIn("autoAssign", rendered)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "private mode assertions require POSIX")
    def test_database_backup_is_private_and_restore_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            backup_dir = root / "backups"
            data_dir.mkdir()
            database = data_dir / "backend.db"
            wal = data_dir / "backend.db-wal"
            database.write_bytes(b"database-before")
            wal.write_bytes(b"wal-before")
            database.chmod(0o640)
            wal.chmod(0o620)
            with (
                patch.object(relay, "V2_DATA_DIR", data_dir),
                patch.object(relay, "V2_BACKUP_DIR", backup_dir),
            ):
                backup = relay._backup_v2_database(OFFICIAL_DIGEST)
                database.write_bytes(b"database-after")
                wal.unlink()
                (data_dir / "backend.db-shm").write_bytes(b"new-shm")
                relay._restore_v2_database(backup)

            self.assertEqual(stat.S_IMODE(backup.path.stat().st_mode), 0o700)
            for item in backup.path.iterdir():
                self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o600)
            self.assertEqual(database.read_bytes(), b"database-before")
            self.assertEqual(wal.read_bytes(), b"wal-before")
            self.assertFalse((data_dir / "backend.db-shm").exists())
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(wal.stat().st_mode), 0o620)

    def test_official_start_failure_restores_backup_and_starts_bundled(self) -> None:
        manager = _manager()
        official = relay._RuntimeImage(
            reference=OFFICIAL_DIGEST,
            source="official",
            acquisition="pulled",
            requested_reference=relay.OFFICIAL_V2_IMAGE,
            digest=OFFICIAL_DIGEST,
            config_path=str(relay.V2_OFFICIAL_CONFIG_PATH),
        )
        bundled = relay._RuntimeImage(
            reference=relay.BUNDLED_V2_IMAGE,
            source="bundled",
            acquisition="cache",
            requested_reference=relay.OFFICIAL_V2_IMAGE,
            config_path=str(relay.V2_CONFIG_PATH),
        )
        backup = relay._DatabaseBackup(Path("/tmp/backup"), {"backend.db": 0o600})
        with (
            patch.object(manager, "is_running", return_value=False),
            patch.object(manager, "_stop_runtime") as stop,
            patch.object(manager, "_ensure_v2_runtime", return_value=official),
            patch.object(relay, "_write_official_v2_config"),
            patch.object(relay, "_verified_image_digest", return_value=""),
            patch.object(relay, "_backup_v2_database", return_value=backup),
            patch.object(manager, "_launch_runtime") as launch,
            patch.object(manager, "_wait_for_runtime", side_effect=[RuntimeError("migration failed"), None]),
            patch.object(relay, "_restore_v2_database") as restore,
            patch.object(manager, "_ensure_bundled_runtime", return_value=bundled),
            patch.object(manager, "apply_remote_config"),
            patch.object(manager, "snapshot", return_value={"running": True}) as snapshot,
        ):
            result = manager.start()

        self.assertEqual(result, {"running": True})
        self.assertEqual(stop.call_count, 2)
        restore.assert_called_once_with(backup)
        self.assertEqual(launch.call_args_list[0].args[0], official)
        self.assertEqual(launch.call_args_list[1].args[0], bundled)
        snapshot.assert_called_once()

    def test_force_update_restarts_an_existing_healthy_runtime(self) -> None:
        manager = _manager()
        bundled = relay._RuntimeImage(
            reference=relay.BUNDLED_V2_IMAGE,
            source="bundled",
            acquisition="cache",
            requested_reference=relay.OFFICIAL_V2_IMAGE,
            config_path=str(relay.V2_CONFIG_PATH),
        )
        with (
            patch.object(manager, "is_running", return_value=True),
            patch.object(manager, "_stop_runtime") as stop,
            patch.object(manager, "_ensure_v2_runtime", return_value=bundled) as ensure,
            patch.object(manager, "_launch_runtime"),
            patch.object(manager, "_wait_for_runtime"),
            patch.object(manager, "apply_remote_config"),
            patch.object(manager, "snapshot", return_value={"running": True}),
        ):
            manager.start(force_update=True)

        stop.assert_called_once()
        ensure.assert_called_once_with(relay.BUNDLED_GATEWAY_DIR)

    def test_api_autostart_uses_the_explicit_update_boundary(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        app_source = (project_root / "serve" / "grok_account_manager" / "api" / "app.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("RELAY_MANAGER.start(force_update=True)", app_source)

    def test_start_selection_is_serialized(self) -> None:
        manager = _manager()
        entered = threading.Event()
        release = threading.Event()

        def serialized(*, force_update: bool) -> dict:
            del force_update
            entered.set()
            release.wait(timeout=2)
            return {"running": True}

        with patch.object(manager, "_start_serialized", side_effect=serialized) as start_serialized:
            first = threading.Thread(target=manager.start)
            second = threading.Thread(target=manager.start)
            first.start()
            self.assertTrue(entered.wait(timeout=1))
            second.start()
            time.sleep(0.05)
            self.assertEqual(start_serialized.call_count, 1)
            release.set()
            first.join(timeout=1)
            second.join(timeout=1)

        self.assertEqual(start_serialized.call_count, 2)

    def test_stop_removes_untracked_project_container(self) -> None:
        manager = _manager()
        with (
            patch.object(relay.shutil, "which", return_value="/usr/bin/docker"),
            patch.object(relay.subprocess, "run", return_value=_completed(["docker", "rm"])) as run,
        ):
            manager._stop_runtime()

        self.assertEqual(
            run.call_args.args[0],
            ["docker", "rm", "--force", "grok-account-manager-gateway-43871"],
        )

    def test_client_key_uses_explicit_all_scopes(self) -> None:
        manager = _manager()
        response = Mock(ok=True)
        response.json.return_value = {"data": {"secret": "g2a_generated"}}
        with (
            patch.object(manager, "_v2_admin_headers", return_value={"Authorization": "Bearer admin"}),
            patch.object(manager, "_save_config"),
            patch.object(relay.requests, "post", return_value=response) as post,
        ):
            manager._ensure_v2_client_key()

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["providerScope"], ["all"])
        self.assertEqual(payload["tierScope"], ["all"])
        self.assertNotIn("accountPool", payload)

    def test_import_response_supports_json_and_rejects_empty_sse(self) -> None:
        response = Mock(
            headers={"Content-Type": "application/json"},
            text='{"data":{"created":1}}',
        )
        response.json.return_value = {"data": {"created": 1}}
        self.assertEqual(relay._parse_import_response(response), {"created": 1})
        with self.assertRaisesRegex(RuntimeError, "未返回完成结果"):
            relay._parse_sse_result(": heartbeat\n\n")

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
