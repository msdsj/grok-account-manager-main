"""Local grok2api gateway manager.

The official container is preferred, pinned to the digest returned by Docker,
and smoke-tested against isolated temporary state before it can see the real
gateway database.  The source vendored under ``gateway/`` remains the offline
fallback when the official image cannot be pulled or validated.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, replace
import base64
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import load_dotenv

from ...core.browser import PROJECT_ROOT


# RelayManager is constructed during module import, before the FastAPI app
# factory runs its usual dotenv initialization.
load_dotenv()


OUTPUT_DIR = PROJECT_ROOT / "output"
CONFIG_PATH = OUTPUT_DIR / "relay-config.json"
LOG_PATH = OUTPUT_DIR / "grok2api-relay.log"
V2_DATA_DIR = OUTPUT_DIR / "grok2api-v2-data"
V2_CONFIG_PATH = OUTPUT_DIR / "grok2api-v2-config.yaml"
V2_OFFICIAL_CONFIG_PATH = OUTPUT_DIR / "grok2api-v2-official-config.yaml"
V2_BACKUP_DIR = OUTPUT_DIR / "grok2api-v2-backups"
V2_VERIFIED_DIGEST_PATH = OUTPUT_DIR / "grok2api-v2-verified-digest"
OFFICIAL_V2_IMAGE = "ghcr.io/chenyme/grok2api:latest"
BUNDLED_V2_IMAGE = "grok-account-manager-gateway:local"
V2_IMAGE_REVISION_LABEL = "io.grok-account-manager.gateway-revision"
V2_ADMIN_USERNAME = "grok-account-manager"
DEFAULT_RELAY_PORT = 43871
LEGACY_RELAY_PORT = 8000
RELAY_CONTAINER_PORT = 8000
BUNDLED_GATEWAY_DIR = PROJECT_ROOT / "gateway"
GATEWAY_REVISION_PATH = BUNDLED_GATEWAY_DIR / "UPSTREAM_REVISION"
GATEWAY_PROXY_ENV = "GROK_ACCOUNT_MANAGER_GATEWAY_PROXY"
GATEWAY_IMAGE_ENV = "GROK_ACCOUNT_MANAGER_GATEWAY_IMAGE"
DEFAULT_GATEWAY_PROXY = "http://127.0.0.1:7890"
HOST_PROXY_NODE_NAME = "本机VPN-Web"

CHAT_MODEL_MARKERS = (
    "reasoning",
    "non-reasoning",
    "fast",
    "auto",
    "expert",
    "heavy",
    "beta",
    "multi-agent",
)


@dataclass
class RelayConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_RELAY_PORT
    api_key: str = "local-grok-api-key"
    admin_key: str = "grok2api"
    gateway_proxy: str = DEFAULT_GATEWAY_PROXY
    gateway_proxy_applied: str = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True)
class _RuntimeImage:
    reference: str
    source: str
    acquisition: str
    requested_reference: str = ""
    digest: str = ""
    fallback_reason: str = ""
    config_path: str = ""


@dataclass(frozen=True)
class _DatabaseBackup:
    path: Path
    original_modes: dict[str, int]


class RelayManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._config = self._load_config()
        self._runtime_image = _RuntimeImage(
            reference=BUNDLED_V2_IMAGE,
            source="bundled",
            acquisition="pending",
            requested_reference=_requested_gateway_image(),
            config_path=str(V2_CONFIG_PATH),
        )

    def _gateway_dir(self) -> Path:
        return BUNDLED_GATEWAY_DIR

    def snapshot(self) -> dict:
        config = self._config
        image = self._runtime_image
        status = self.status()
        public_base_url = os.environ.get("GROK_ACCOUNT_MANAGER_PUBLIC_BASE_URL", "http://127.0.0.1:43187")
        return {
            **status,
            "config": {
                "source": image.source,
                "sourcePath": str(BUNDLED_GATEWAY_DIR),
                "sourceRevision": _gateway_revision(),
                "imageRequested": image.requested_reference,
                "image": image.reference,
                "imageSource": image.source,
                "imageAcquisition": image.acquisition,
                "imageDigest": image.digest,
                "fallbackReason": image.fallback_reason,
                "runtimeConfigPath": image.config_path,
                "host": config.host,
                "port": config.port,
                "baseUrl": config.base_url,
                "publicBaseUrl": public_base_url,
                "apiKey": config.api_key,
                "apiKeyMasked": _mask_secret(config.api_key),
                "adminKey": config.admin_key,
                "adminKeyMasked": _mask_secret(config.admin_key),
                "gatewayProxy": _mask_proxy_url(config.gateway_proxy),
                "gatewayProxyConfigured": bool(config.gateway_proxy),
                "dataDir": str(V2_DATA_DIR),
                "logPath": str(LOG_PATH),
                "engine": "grok2api-docker",
            },
        }

    def update_config(self, patch: dict[str, Any]) -> dict:
        with self._lock:
            current = asdict(self._config)
            if "host" in patch:
                current["host"] = str(patch.get("host") or "127.0.0.1").strip() or "127.0.0.1"
            if "port" in patch:
                current["port"] = _safe_port(patch.get("port"), DEFAULT_RELAY_PORT)
            if "apiKey" in patch:
                current["api_key"] = str(patch.get("apiKey") or "").strip()
            if "adminKey" in patch:
                current["admin_key"] = str(patch.get("adminKey") or "").strip()
            if "gatewayProxy" in patch:
                current["gateway_proxy"] = _normalize_gateway_proxy(patch.get("gatewayProxy"))
                current["gateway_proxy_applied"] = ""
            if not current["api_key"]:
                raise ValueError("API 秘钥不能为空")
            if not current["admin_key"]:
                raise ValueError("管理秘钥不能为空")
            self._config = RelayConfig(**current)
            self._save_config(self._config)

        if self.is_running():
            self.apply_remote_config()
        return self.snapshot()

    def is_running(self) -> bool:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return True
            if self._process is not None and self._process.poll() is not None:
                self._process = None
        return self._healthcheck()

    def status(self) -> dict:
        process_running = False
        return_code = None
        with self._lock:
            if self._process is not None:
                return_code = self._process.poll()
                process_running = return_code is None
                if return_code is not None:
                    self._process = None
        health_ok = self._healthcheck()
        return {
            "running": process_running or health_ok,
            "managed": process_running,
            "returnCode": return_code,
            "healthy": health_ok,
            "lastLog": _tail(LOG_PATH),
        }

    def start(self, *, force_update: bool = False) -> dict:
        with self._start_lock:
            return self._start_serialized(force_update=force_update)

    def _start_serialized(self, *, force_update: bool) -> dict:
        if self.is_running():
            if not force_update:
                self.apply_remote_config()
                return self.snapshot()
        self._stop_runtime()

        gateway_dir = self._gateway_dir()
        runtime_image = self._ensure_v2_runtime(gateway_dir)
        backup: _DatabaseBackup | None = None

        if runtime_image.source in {"official", "override"}:
            try:
                _write_official_v2_config(
                    V2_CONFIG_PATH,
                    V2_OFFICIAL_CONFIG_PATH,
                    admin_password=self._config.admin_key,
                )
                runtime_image = replace(runtime_image, config_path=str(V2_OFFICIAL_CONFIG_PATH))
                self._runtime_image = runtime_image
                if _verified_image_digest() != runtime_image.digest:
                    backup = _backup_v2_database(runtime_image.digest)
            except Exception as error:
                return self._start_bundled_fallback(gateway_dir, runtime_image, error, backup)

        try:
            self._launch_runtime(runtime_image, gateway_dir)
            self._wait_for_runtime()
            if runtime_image.source in {"official", "override"}:
                self._v2_admin_headers()
            self.apply_remote_config()
        except Exception as error:
            if runtime_image.source in {"official", "override"}:
                return self._start_bundled_fallback(gateway_dir, runtime_image, error, backup)
            raise

        if runtime_image.source in {"official", "override"}:
            _write_private_text(V2_VERIFIED_DIGEST_PATH, f"{runtime_image.digest}\n")
        return self.snapshot()

    def _launch_runtime(self, runtime_image: _RuntimeImage, gateway_dir: Path) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        _ensure_v2_data_dir()
        env = os.environ.copy()
        env.update(
            {
                "SERVER_HOST": self._config.host,
                "SERVER_PORT": str(RELAY_CONTAINER_PORT),
                "SERVER_WORKERS": "1",
                "GROK_APP_API_KEY": self._config.api_key,
                "GROK_APP_APP_KEY": self._config.admin_key,
                "GROK_APP_APP_URL": self._config.base_url,
                "GROK_ACCOUNT_REFRESH_ENABLED": "true",
            }
        )
        log_file = LOG_PATH.open("a", encoding="utf-8")
        log_file.write(
            f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] starting gateway "
            f"({runtime_image.source}: {runtime_image.reference})\n"
        )
        log_file.flush()
        command = _grok2api_v2_command(
            self._config,
            Path(runtime_image.config_path or V2_CONFIG_PATH),
            image=runtime_image.reference,
        )
        try:
            with self._lock:
                self._process = subprocess.Popen(
                    command,
                    cwd=str(gateway_dir),
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        finally:
            log_file.close()

    def _wait_for_runtime(self) -> None:
        deadline = time.time() + 20
        while time.time() < deadline:
            if self._healthcheck():
                return
            with self._lock:
                if self._process is not None and self._process.poll() is not None:
                    raise RuntimeError(f"网关启动失败，退出码 {self._process.returncode}。请查看 {LOG_PATH}")
            time.sleep(0.5)
        raise TimeoutError(f"网关启动超时。请查看 {LOG_PATH}")

    def _start_bundled_fallback(
        self,
        gateway_dir: Path,
        failed_image: _RuntimeImage,
        error: Exception,
        backup: _DatabaseBackup | None,
    ) -> dict:
        self._stop_runtime()
        if backup is not None:
            try:
                _restore_v2_database(backup)
            except Exception as restore_error:
                raise RuntimeError(
                    f"官方镜像失败（{_runtime_error_text(error)}），且数据库恢复失败："
                    f"{_runtime_error_text(restore_error)}"
                ) from restore_error
        reasons = [failed_image.fallback_reason] if failed_image.fallback_reason else []
        reasons.append(f"官方镜像正式启动或握手失败：{_runtime_error_text(error)}")
        bundled = self._ensure_bundled_runtime(
            gateway_dir,
            reasons,
            failed_image.requested_reference or _requested_gateway_image(),
        )
        self._runtime_image = bundled
        self._launch_runtime(bundled, gateway_dir)
        self._wait_for_runtime()
        self.apply_remote_config()
        return self.snapshot()

    def stop(self) -> dict:
        with self._start_lock:
            self._stop_runtime()
            return self.snapshot()

    def _stop_runtime(self) -> None:
        with self._lock:
            proc = self._process
            self._process = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if shutil.which("docker"):
            subprocess.run(
                ["docker", "rm", "--force", _relay_container_name(self._config)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    def apply_remote_config(self, clearance: dict[str, str] | None = None) -> dict:
        del clearance
        result = self._ensure_v2_client_key()
        self._ensure_gateway_proxy_node()
        return result

    def sync_accounts(self, credentials: list[dict], *, refresh_existing: bool = False) -> dict:
        del refresh_existing
        return self._v2_import_accounts(credentials)

    def replace_accounts(self, credentials: list[dict], *, pool: str = "basic", prune_unlisted: bool = True) -> dict:
        """Replace the relay runtime pool with the current project account tokens."""
        del pool, prune_unlisted
        return self._v2_import_accounts(credentials)

    def list_models(self) -> dict:
        if not self.is_running():
            self.start()
        self._ensure_v2_client_key()
        cfg = self._config
        response = requests.get(
            f"{cfg.base_url}/v1/models",
            headers=_api_headers(cfg),
            timeout=20,
        )
        _raise_response_error(response)
        return response.json()

    def send_chat_completion(self, *, model: str, messages: list[dict], timeout: int = 180) -> dict:
        if not self.is_running():
            self.start()
        self._ensure_v2_client_key()
        cfg = self._config
        response = requests.post(
            f"{cfg.base_url}/v1/chat/completions",
            headers=_api_headers(cfg),
            json={
                "model": model,
                "stream": False,
                "messages": messages,
            },
            timeout=timeout,
        )
        _raise_response_error(response)
        payload = response.json()
        text = _extract_chat_text(payload) if isinstance(payload, dict) else ""
        return {
            "model": model,
            "message": {
                "role": "assistant",
                "content": text or "模型已响应，但没有返回文本内容",
            },
            "raw": payload,
        }

    def generate_image(self, *, model: str, prompt: str, n: int, size: str, timeout: int = 180) -> dict:
        if not self.is_running():
            self.start()
        self._ensure_v2_client_key()
        cfg = self._config
        response = requests.post(
            f"{cfg.base_url}/v1/images/generations",
            headers=_api_headers(cfg),
            json={
                "model": model,
                "prompt": prompt,
                "n": n,
                "size": size or "1024x1024",
                "response_format": "url",
            },
            timeout=timeout,
        )
        _raise_response_error(response)
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            raise RuntimeError("grok2api 图片接口没有返回图片数据")
        return {"model": model, "data": data, "raw": payload}

    def proxy_request(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        timeout: int = 180,
    ) -> requests.Response:
        if not self.is_running():
            self.start()
        # The admin API (/api/admin/v1/*) authenticates via the JWT the frontend's own
        # login flow obtains, not the OpenAI-compatible client key below — injecting the
        # client key there would just be a harmless-looking but wrong Authorization header.
        is_admin_api = path.startswith("/api/admin/v1")
        if not is_admin_api:
            self._ensure_v2_client_key()
        cfg = self._config
        target_url = f"{cfg.base_url}{path}"
        if query:
            target_url = f"{target_url}?{query}"

        forward_headers = _forward_headers(headers or {})
        if not is_admin_api and "authorization" not in {key.lower() for key in forward_headers}:
            forward_headers["Authorization"] = f"Bearer {cfg.api_key}"

        return requests.request(
            method=method,
            url=target_url,
            headers=forward_headers,
            data=body,
            timeout=timeout,
            stream=False,
        )

    def probe_models(self, probe_chat: bool = True) -> dict:
        models_payload = self.list_models()
        models = models_payload.get("data") if isinstance(models_payload, dict) else []
        if not isinstance(models, list):
            models = []
        results = []
        for model in models:
            model_info = model if isinstance(model, dict) else {}
            model_id = str(model_info.get("id") or "")
            if not model_id:
                continue
            capability = _model_capability(model_info, model_id)
            result = {
                "id": model_id,
                "name": model_info.get("name") or model_id,
                "capability": capability,
                "status": "listed",
                "message": "已在 /v1/models 返回",
            }
            if probe_chat and capability == "chat":
                result.update(self._probe_chat_model(model_id))
            elif capability in {"image", "image_edit", "video"}:
                result["message"] = "已识别媒体模型，未执行消耗额度的生成测试"
            results.append(result)
        return {"models": results, "count": len(results)}

    def _probe_chat_model(self, model_id: str) -> dict:
        cfg = self._config
        try:
            response = requests.post(
                f"{cfg.base_url}/v1/chat/completions",
                headers=_api_headers(cfg),
                json={
                    "model": model_id,
                    "stream": False,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "max_tokens": 8,
                },
                timeout=90,
            )
            if response.ok:
                data = response.json()
                text = _extract_chat_text(data)
                return {"status": "ok", "message": text or "调用成功"}
            return {
                "status": "error",
                "message": _response_error_text(response),
            }
        except Exception as error:
            return {"status": "error", "message": str(error)}

    def _ensure_v2_runtime(self, gateway_dir: Path) -> _RuntimeImage:
        if not shutil.which("docker"):
            raise RuntimeError("本地网关需要 Docker Desktop，但未找到 docker 命令")

        docker_info = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if docker_info.returncode != 0:
            raise RuntimeError("本地网关需要 Docker Desktop，请先启动 Docker Desktop 后再启动中转")

        self._ensure_v2_config(gateway_dir)
        fallback_reasons: list[str] = []
        requested_image = _requested_gateway_image()

        with LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pulling official gateway image "
                f"({requested_image})\n"
            )
            log_file.flush()
            pull = subprocess.run(
                ["docker", "pull", requested_image],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                check=False,
            )

        acquisition = "pulled" if pull.returncode == 0 else "cache"
        if pull.returncode != 0:
            fallback_reasons.append(f"官方镜像拉取失败（退出码 {pull.returncode}）")

        official_digest = _resolved_image_digest(requested_image)
        if not official_digest:
            if pull.returncode == 0:
                fallback_reasons.append("官方镜像未提供可验证的 RepoDigest")
            else:
                fallback_reasons.append("本地没有可用的官方镜像缓存")
        else:
            try:
                _smoke_test_official_image(official_digest)
            except Exception as error:
                fallback_reasons.append(f"官方镜像隔离探针失败：{_runtime_error_text(error)}")
            else:
                selected = _RuntimeImage(
                    reference=official_digest,
                    source="official" if requested_image == OFFICIAL_V2_IMAGE else "override",
                    acquisition=acquisition,
                    requested_reference=requested_image,
                    digest=official_digest,
                    fallback_reason="；".join(fallback_reasons),
                    config_path=str(V2_OFFICIAL_CONFIG_PATH),
                )
                self._runtime_image = selected
                return selected

        selected = self._ensure_bundled_runtime(gateway_dir, fallback_reasons, requested_image)
        self._runtime_image = selected
        return selected

    def _ensure_bundled_runtime(
        self,
        gateway_dir: Path,
        fallback_reasons: list[str],
        requested_image: str,
    ) -> _RuntimeImage:
        if not (gateway_dir / "backend" / "go.mod").exists() or not (gateway_dir / "Dockerfile").exists():
            raise ValueError(f"内置网关文件不完整：{gateway_dir}。请重新拉取本项目代码。")

        source_revision = _gateway_revision()
        if _image_revision() == source_revision:
            return _RuntimeImage(
                reference=BUNDLED_V2_IMAGE,
                source="bundled",
                acquisition="cache",
                requested_reference=requested_image,
                fallback_reason="；".join(fallback_reasons),
                config_path=str(V2_CONFIG_PATH),
            )

        with LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] building bundled gateway image ({source_revision})\n")
            log_file.flush()
            build = subprocess.run(
                [
                    "docker",
                    "build",
                    "--build-arg",
                    f"GATEWAY_SOURCE_REVISION={source_revision}",
                    "--tag",
                    BUNDLED_V2_IMAGE,
                    str(gateway_dir),
                ],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if build.returncode != 0:
            raise RuntimeError(f"内置网关镜像构建失败，退出码 {build.returncode}。请查看 {LOG_PATH}")
        return _RuntimeImage(
            reference=BUNDLED_V2_IMAGE,
            source="bundled",
            acquisition="built",
            requested_reference=requested_image,
            fallback_reason="；".join(fallback_reasons),
            config_path=str(V2_CONFIG_PATH),
        )

    def _ensure_v2_config(self, gateway_dir: Path) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        _ensure_v2_data_dir()
        if V2_CONFIG_PATH.exists():
            try:
                config_text = V2_CONFIG_PATH.read_text(encoding="utf-8")
            except OSError as error:
                raise RuntimeError(f"无法读取已有新版 grok2api 配置：{error}") from error
            migrated = _rewrite_relay_listen(config_text)
            if migrated != config_text:
                _write_private_text(V2_CONFIG_PATH, migrated)
            return

        if self._config.admin_key == "grok2api":
            self._config.admin_key = secrets.token_urlsafe(24)
            self._save_config(self._config)

        _write_private_text(V2_CONFIG_PATH, _render_v2_config(gateway_dir, self._config.admin_key))

    def _v2_admin_headers(self) -> dict[str, str]:
        cfg = self._config
        response = requests.post(
            f"{cfg.base_url}/api/admin/v1/auth/login",
            json={"username": V2_ADMIN_USERNAME, "password": cfg.admin_key},
            timeout=20,
        )
        _raise_response_error(response)
        access_token = _admin_access_token(response.json())
        if not access_token:
            raise RuntimeError("新版 grok2api 管理员登录没有返回 access token")
        return {"Authorization": f"Bearer {access_token}"}

    def _ensure_v2_client_key(self) -> dict:
        if self._config.api_key.startswith("g2a_"):
            return {"managedApiKey": True}
        response = requests.post(
            f"{self._config.base_url}/api/admin/v1/client-keys",
            headers=self._v2_admin_headers(),
            # Media routes may require Super/paid accounts. The local gateway
            # must not silently create a key that excludes those accounts.
            json={
                "name": "grok-account-manager",
                "enabled": True,
                "providerScope": ["all"],
                "tierScope": ["all"],
            },
            timeout=20,
        )
        _raise_response_error(response)
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        api_key = str(data.get("secret") or "").strip() if isinstance(data, dict) else ""
        if not api_key:
            raise RuntimeError("新版 grok2api 创建客户端 Key 没有返回 secret")
        self._config.api_key = api_key
        self._save_config(self._config)
        return {"managedApiKey": True}

    def _ensure_gateway_proxy_node(self) -> None:
        """Keep the project-owned Web egress node aligned with the host VPN.

        The gateway runs in a container, so a host-local endpoint must be
        rewritten to ``host.docker.internal`` before it is stored in the
        gateway database. This is deliberately best-effort: a machine without
        a running VPN should still be able to start the local gateway and use
        its normal direct fallback.
        """

        configured_proxy = _normalize_gateway_proxy(self._config.gateway_proxy)
        try:
            headers = self._v2_admin_headers()
            response = requests.get(
                f"{self._config.base_url}/api/admin/v1/egress-nodes",
                headers=headers,
                params={"scope": "grok_web"},
                timeout=20,
            )
            _raise_response_error(response)
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            items = data.get("items") if isinstance(data, dict) else []
            if not isinstance(items, list):
                items = []
            node = next(
                (
                    item
                    for item in items
                    if isinstance(item, dict)
                    and str(item.get("name") or "").strip() == HOST_PROXY_NODE_NAME
                    and str(item.get("scope") or "") == "grok_web"
                ),
                None,
            )

            if not configured_proxy:
                if node and (bool(node.get("enabled")) or bool(node.get("proxyConfigured"))):
                    self._update_gateway_proxy_node(headers, node, enabled=False, clear_proxy=True)
                if self._config.gateway_proxy_applied:
                    self._config.gateway_proxy_applied = ""
                    self._save_config(self._config)
                return

            if _is_local_proxy(configured_proxy) and not _proxy_endpoint_reachable(configured_proxy):
                if node and (bool(node.get("enabled")) or bool(node.get("proxyConfigured"))):
                    self._update_gateway_proxy_node(headers, node, enabled=False, clear_proxy=True)
                if self._config.gateway_proxy_applied:
                    self._config.gateway_proxy_applied = ""
                    self._save_config(self._config)
                with LOG_PATH.open("a", encoding="utf-8") as log_file:
                    log_file.write(
                        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] host VPN proxy unavailable "
                        f"({_mask_proxy_url(configured_proxy)}); using direct fallback\n"
                    )
                return

            container_proxy = _container_proxy_url(configured_proxy)
            needs_update = (
                node is None
                or not bool(node.get("enabled"))
                or not bool(node.get("proxyConfigured"))
                or self._config.gateway_proxy_applied != configured_proxy
            )
            if needs_update:
                if node is None:
                    request = {
                        "name": HOST_PROXY_NODE_NAME,
                        "scope": "grok_web",
                        "enabled": True,
                        "proxyPool": False,
                        "proxyURL": container_proxy,
                        "accountCapacity": 0,
                    }
                    create = requests.post(
                        f"{self._config.base_url}/api/admin/v1/egress-nodes",
                        headers=headers,
                        json=request,
                        timeout=20,
                    )
                    _raise_response_error(create)
                else:
                    self._update_gateway_proxy_node(headers, node, enabled=True, proxy_url=container_proxy)

            if self._config.gateway_proxy_applied != configured_proxy:
                self._config.gateway_proxy_applied = configured_proxy
                self._save_config(self._config)
        except Exception as error:
            # Proxy setup must not make the local API unavailable. The masked
            # endpoint is enough to diagnose a typo without leaking credentials.
            masked = _mask_proxy_url(configured_proxy)
            with LOG_PATH.open("a", encoding="utf-8") as log_file:
                log_file.write(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] host VPN proxy setup skipped "
                    f"({masked}): {type(error).__name__}: {error}\n"
                )

    def _update_gateway_proxy_node(
        self,
        headers: dict[str, str],
        node: dict[str, Any],
        *,
        enabled: bool,
        proxy_url: str | None = None,
        clear_proxy: bool = False,
    ) -> None:
        node_id = str(node.get("id") or "").strip()
        if not node_id:
            raise RuntimeError("本机 VPN 出口节点缺少 ID")
        request: dict[str, Any] = {
            "name": HOST_PROXY_NODE_NAME,
            "scope": "grok_web",
            "enabled": enabled,
            "proxyPool": False,
            "accountCapacity": int(node.get("accountCapacity") or 0),
        }
        if clear_proxy:
            request["clearProxyURL"] = True
        elif proxy_url:
            request["proxyURL"] = proxy_url
        response = requests.put(
            f"{self._config.base_url}/api/admin/v1/egress-nodes/{node_id}",
            headers=headers,
            json=request,
            timeout=20,
        )
        _raise_response_error(response)

    def _v2_import_accounts(self, credentials: list[dict]) -> dict:
        if not self.is_running():
            self.start()
        self._ensure_v2_client_key()
        web_accounts = _credentials_to_v2_web_accounts(credentials)
        console_accounts = _credentials_to_v2_console_accounts(credentials)
        build_accounts = _credentials_to_v2_build_accounts(credentials)
        if not web_accounts and not console_accounts and not build_accounts:
            raise ValueError("没有找到可同步的 Grok Web、Grok Console 或 Grok Build 凭据")
        results: dict[str, dict] = {}
        if web_accounts:
            document = {"provider": "grok_web", "accounts": web_accounts}
            results["web"] = self._v2_upload_accounts(
                "/api/admin/v1/accounts/web/import",
                "grok-account-manager-web.json",
                document,
            )
        if console_accounts:
            document = {"provider": "grok_console", "accounts": console_accounts}
            results["console"] = self._v2_upload_accounts(
                "/api/admin/v1/accounts/console/import",
                "grok-account-manager-console.json",
                document,
            )
        if build_accounts:
            results["build"] = self._v2_upload_accounts(
                "/api/admin/v1/accounts/import",
                "grok-account-manager-build.json",
                {"accounts": build_accounts},
            )
        return {
            "requested": len(web_accounts) + len(console_accounts) + len(build_accounts),
            "result": results,
            "counts": {
                "web": len(web_accounts),
                "console": len(console_accounts),
                "build": len(build_accounts),
            },
        }

    def _v2_upload_accounts(self, path: str, filename: str, document: dict) -> dict:
        headers = self._v2_admin_headers()
        headers["Accept"] = "text/event-stream, application/json"
        response = requests.post(
            f"{self._config.base_url}{path}",
            headers=headers,
            files={"file": (filename, json.dumps(document, ensure_ascii=False).encode("utf-8"), "application/json")},
            timeout=180,
        )
        _raise_response_error(response)
        return _parse_import_response(response)

    def _healthcheck(self) -> bool:
        try:
            response = requests.get(f"{self._config.base_url}/healthz", timeout=2)
            return response.ok
        except Exception:
            return False

    def _load_config(self) -> RelayConfig:
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                configured_port = _safe_port(data.get("port"), DEFAULT_RELAY_PORT)
                if configured_port == LEGACY_RELAY_PORT:
                    configured_port = DEFAULT_RELAY_PORT
                if GATEWAY_PROXY_ENV in os.environ:
                    gateway_proxy_value = os.environ.get(GATEWAY_PROXY_ENV, "")
                elif "gateway_proxy" in data:
                    gateway_proxy_value = data.get("gateway_proxy")
                elif "gatewayProxy" in data:
                    gateway_proxy_value = data.get("gatewayProxy")
                else:
                    gateway_proxy_value = DEFAULT_GATEWAY_PROXY
                config = RelayConfig(
                    host=str(data.get("host") or "127.0.0.1"),
                    port=configured_port,
                    api_key=str(data.get("api_key") or data.get("apiKey") or "local-grok-api-key"),
                    admin_key=str(data.get("admin_key") or data.get("adminKey") or "grok2api"),
                    gateway_proxy=_normalize_gateway_proxy(gateway_proxy_value),
                    gateway_proxy_applied=_normalize_gateway_proxy(data.get("gateway_proxy_applied") or ""),
                )
                # Older local runs stored an external checkout path in this
                # file. Rewriting through the project-owned dataclass removes
                # that stale cross-project setting on the next startup.
                if "grok2api_path" in data or "gateway_proxy" not in data:
                    self._save_config(config)
                return config
            except Exception:
                pass
        gateway_proxy_value = (
            os.environ.get(GATEWAY_PROXY_ENV, "")
            if GATEWAY_PROXY_ENV in os.environ
            else DEFAULT_GATEWAY_PROXY
        )
        return RelayConfig(gateway_proxy=_normalize_gateway_proxy(gateway_proxy_value))

    def _save_config(self, config: RelayConfig) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        file_descriptor = os.open(
            CONFIG_PATH,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            CONFIG_PATH.chmod(0o600)
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                file_descriptor = -1
                json.dump(asdict(config), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)


def _ensure_v2_data_dir() -> None:
    V2_DATA_DIR.mkdir(parents=True, exist_ok=True)
    # grok2api's container runs as a fixed non-root uid (via su-exec), which won't
    # match this host account's uid on the bind-mounted volume, so the directory
    # needs to stay world-writable for the container process to create/open its
    # sqlite database and media files under it.
    os.chmod(V2_DATA_DIR, 0o777)


def _verified_image_digest() -> str:
    try:
        digest = V2_VERIFIED_DIGEST_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return digest if _valid_digest_reference(digest) else ""


def _database_state_paths() -> dict[str, Path]:
    return {
        "backend.db": V2_DATA_DIR / "backend.db",
        "backend.db-wal": V2_DATA_DIR / "backend.db-wal",
        "backend.db-shm": V2_DATA_DIR / "backend.db-shm",
    }


def _backup_v2_database(digest: str) -> _DatabaseBackup:
    _ensure_v2_data_dir()
    if V2_BACKUP_DIR.is_symlink():
        raise RuntimeError(f"数据库备份目录不能是符号链接：{V2_BACKUP_DIR}")
    V2_BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(V2_BACKUP_DIR, 0o700)
    digest_suffix = digest.rsplit(":", 1)[-1][:12] if digest else "unknown"
    backup_path = Path(
        tempfile.mkdtemp(
            prefix=f"{time.strftime('%Y%m%d-%H%M%S')}-{digest_suffix}-",
            dir=V2_BACKUP_DIR,
        )
    )
    os.chmod(backup_path, 0o700)
    original_modes: dict[str, int] = {}
    for name, source in _database_state_paths().items():
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"数据库备份源不是安全的普通文件：{source}")
        original_modes[name] = stat.S_IMODE(source.stat().st_mode)
        destination = backup_path / name
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
    _write_private_text(
        backup_path / "manifest.json",
        json.dumps(
            {
                "digest": digest,
                "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "files": original_modes,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    return _DatabaseBackup(path=backup_path, original_modes=original_modes)


def _restore_v2_database(backup: _DatabaseBackup) -> None:
    _ensure_v2_data_dir()
    state_paths = _database_state_paths()
    for name in backup.original_modes:
        source = backup.path / name
        if name not in state_paths or source.is_symlink() or not source.is_file():
            raise RuntimeError(f"数据库备份文件缺失或不安全：{source}")
    for target in state_paths.values():
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            raise RuntimeError(f"数据库恢复目标不是普通文件：{target}")
    for name, mode in backup.original_modes.items():
        source = backup.path / name
        destination = state_paths[name]
        shutil.copyfile(source, destination)
        os.chmod(destination, mode)


def _safe_port(value: Any, default: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        port = default
    return max(1, min(65535, port))


def _normalize_gateway_proxy(value: Any) -> str:
    """Normalize a user-entered gateway proxy without exposing credentials."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("网关代理地址格式无效")
    try:
        if parsed.port is None or not 1 <= parsed.port <= 65535:
            raise ValueError("网关代理端口必须是 1-65535")
    except ValueError as error:
        raise ValueError("网关代理端口无效") from error
    return urlunsplit(parsed)


def _container_proxy_url(value: str) -> str:
    """Rewrite host-loopback proxy URLs for use from inside Docker."""

    normalized = _normalize_gateway_proxy(value)
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        return normalized
    host = "host.docker.internal"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    if parsed.username is not None:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo += f":{parsed.password}"
        host = f"{userinfo}@{host}"
    return urlunsplit(parsed._replace(netloc=host))


def _is_local_proxy(value: str) -> bool:
    try:
        hostname = (urlsplit(_normalize_gateway_proxy(value)).hostname or "").lower()
    except ValueError:
        return False
    return hostname in {"127.0.0.1", "localhost", "::1"}


def _proxy_endpoint_reachable(value: str) -> bool:
    """Check only whether the local proxy port is listening.

    This deliberately does not perform an external request or send proxy
    credentials. The gateway's own egress probe remains authoritative for
    protocol and upstream reachability.
    """

    try:
        parsed = urlsplit(_normalize_gateway_proxy(value))
        hostname = parsed.hostname or ""
        port = parsed.port
        if not hostname or port is None:
            return False
        with socket.create_connection((hostname, port), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


def _mask_proxy_url(value: str) -> str:
    """Return a display-safe proxy URL with userinfo and host partially hidden."""

    normalized = str(value or "").strip()
    if not normalized:
        return "直连"
    try:
        parsed = urlsplit(normalized if "://" in normalized else f"http://{normalized}")
        hostname = parsed.hostname or "?"
        if hostname.count(".") == 3:
            parts = hostname.split(".")
            display_host = f"{parts[0]}.***.***.{parts[-1]}"
        elif len(hostname) <= 3:
            display_host = "***"
        else:
            display_host = f"{hostname[:2]}***{hostname[-2:]}"
        if ":" in hostname and not hostname.startswith("["):
            display_host = "[IPv6]"
        if parsed.port is not None:
            display_host = f"{display_host}:{parsed.port}"
        return f"{parsed.scheme}://{display_host}"
    except (ValueError, TypeError):
        return "代理（配置无效）"


def _render_v2_config(gateway_dir: Path, admin_password: str) -> str:
    template_path = gateway_dir / "config.example.yaml"
    try:
        config_text = template_path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"无法读取内置网关配置模板：{error}") from error
    config_text = _rewrite_relay_listen(config_text)
    replacements = {
        'jwtSecret: "replace-with-at-least-32-characters"': f'jwtSecret: "{secrets.token_hex(32)}"',
        'credentialEncryptionKey: "replace-with-base64-key"': (
            f'credentialEncryptionKey: "{base64.b64encode(secrets.token_bytes(32)).decode("ascii")}"'
        ),
        'username: "admin"': f'username: "{V2_ADMIN_USERNAME}"',
        'password: "replace-with-a-strong-password"': f'password: "{admin_password}"',
    }
    for old, new in replacements.items():
        config_text = config_text.replace(old, new)
    return config_text


def _render_smoke_config(admin_password: str) -> str:
    """Render only stable upstream fields for an isolated candidate probe."""

    encryption_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    return (
        "server:\n"
        f'  listen: "0.0.0.0:{RELAY_CONTAINER_PORT}"\n'
        "secrets:\n"
        f'  jwtSecret: "{secrets.token_hex(32)}"\n'
        f'  credentialEncryptionKey: "{encryption_key}"\n'
        "bootstrapAdmin:\n"
        f'  username: "{V2_ADMIN_USERNAME}"\n'
        f'  password: "{admin_password}"\n'
        "frontend:\n"
        '  staticPath: "./frontend/dist"\n'
        "database:\n"
        "  driver: sqlite\n"
        "  sqlite:\n"
        '    path: "./data/backend.db"\n'
        "media:\n"
        "  driver: local\n"
        "  local:\n"
        '    path: "./data/media"\n'
    )


def _write_official_v2_config(
    source_path: Path,
    destination_path: Path,
    *,
    admin_password: str,
) -> None:
    """Create a strict upstream-compatible config without rotating secrets."""

    try:
        source_text = source_path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"无法读取现有网关配置：{error}") from error
    jwt_secret = _yaml_section_scalar(source_text, "secrets", "jwtSecret")
    encryption_key = _yaml_section_scalar(source_text, "secrets", "credentialEncryptionKey")
    if not jwt_secret or jwt_secret == "replace-with-at-least-32-characters":
        raise RuntimeError("现有网关配置缺少可复用的 jwtSecret")
    if not encryption_key or encryption_key == "replace-with-base64-key":
        raise RuntimeError("现有网关配置缺少可复用的 credentialEncryptionKey")
    database_driver = _yaml_section_scalar(source_text, "database", "driver") or "sqlite"
    if database_driver != "sqlite":
        raise RuntimeError("官方镜像自动切换目前仅支持可本地备份恢复的 SQLite 数据库")

    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    config_text = (
        "server:\n"
        f'  listen: "0.0.0.0:{RELAY_CONTAINER_PORT}"\n'
        "secrets:\n"
        f"  jwtSecret: {quote(jwt_secret)}\n"
        f"  credentialEncryptionKey: {quote(encryption_key)}\n"
        "bootstrapAdmin:\n"
        f"  username: {quote(V2_ADMIN_USERNAME)}\n"
        f"  password: {quote(admin_password)}\n"
        "frontend:\n"
        '  staticPath: "./frontend/dist"\n'
        "database:\n"
        "  driver: sqlite\n"
        "  sqlite:\n"
        '    path: "./data/backend.db"\n'
        "media:\n"
        "  driver: local\n"
        "  local:\n"
        '    path: "./data/media"\n'
    )
    _write_private_text(destination_path, config_text)


def _yaml_section_scalar(config_text: str, section: str, key: str) -> str:
    in_section = False
    prefix = f"{key}:"
    for line in config_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            in_section = stripped == f"{section}:"
            continue
        if not in_section or len(line) - len(line.lstrip()) != 2:
            continue
        candidate = line.strip()
        if not candidate.startswith(prefix):
            continue
        raw = candidate[len(prefix) :].strip()
        if raw.startswith('"'):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                return ""
            return str(value)
        if raw.startswith("'") and raw.endswith("'"):
            return raw[1:-1].replace("''", "'")
        return raw.split(" #", 1)[0].strip()
    return ""


def _rewrite_relay_listen(config_text: str) -> str:
    """Keep the mounted grok2api config aligned with the container port."""
    migrated = config_text.replace(
        "0.0.0.0:8000",
        f"0.0.0.0:{RELAY_CONTAINER_PORT}",
    ).replace(
        "127.0.0.1:8000",
        f"0.0.0.0:{RELAY_CONTAINER_PORT}",
    )
    lines = migrated.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.lstrip().startswith("listen:"):
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            indent = line[: len(line) - len(line.lstrip())]
            lines[index] = f'{indent}listen: "0.0.0.0:{RELAY_CONTAINER_PORT}"{ending}'
            break
    return "".join(lines)


def _grok2api_v2_command(
    config: RelayConfig,
    config_path: Path,
    *,
    image: str = BUNDLED_V2_IMAGE,
) -> list[str]:
    container_name = _relay_container_name(config)
    command = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--name",
        container_name,
        # Docker Desktop provides this name by default; the explicit host-gateway
        # mapping also makes the same command work on Linux Docker Engine.
        "--add-host",
        "host.docker.internal:host-gateway",
        "--publish",
        f"{config.host}:{config.port}:{RELAY_CONTAINER_PORT}",
        "--health-cmd",
        f"wget -qO- http://127.0.0.1:{RELAY_CONTAINER_PORT}/healthz >/dev/null || exit 1",
        "--health-interval",
        "30s",
        "--health-timeout",
        "5s",
        "--health-retries",
        "3",
        "--volume",
        f"{config_path}:/run/grok2api/config.yaml:ro",
        "--volume",
        f"{V2_DATA_DIR}:/app/data",
    ]
    proxy = _container_proxy_url(config.gateway_proxy)
    if _is_local_proxy(config.gateway_proxy) and not _proxy_endpoint_reachable(config.gateway_proxy):
        proxy = ""
    if proxy:
        # The explicit Web node handles browser traffic. These environment
        # variables additionally cover direct Build/utility requests that use
        # Go's ProxyFromEnvironment path.
        command.extend(
            [
                "--env",
                f"HTTP_PROXY={proxy}",
                "--env",
                f"HTTPS_PROXY={proxy}",
                "--env",
                f"ALL_PROXY={proxy}",
                "--env",
                "NO_PROXY=127.0.0.1,localhost,host.docker.internal",
                "--env",
                "no_proxy=127.0.0.1,localhost,host.docker.internal",
            ]
        )
    # Both the official and bundled images provide the compatible entrypoint
    # and CMD. Keeping the image last avoids depending on an internal binary
    # path that may change in a later official release.
    command.append(image)
    return command


def _relay_container_name(config: RelayConfig) -> str:
    return f"grok-account-manager-gateway-{config.port}"


def _write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        path.chmod(0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _gateway_revision() -> str:
    """Return the vendored gateway revision used to decide whether to rebuild."""
    try:
        revision = GATEWAY_REVISION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        revision = ""
    return revision or "unknown"


def _requested_gateway_image() -> str:
    return str(os.environ.get(GATEWAY_IMAGE_ENV) or "").strip() or OFFICIAL_V2_IMAGE


def _image_repository(reference: str) -> str:
    value = str(reference or "").strip().split("@", 1)[0]
    slash_index = value.rfind("/")
    tag_index = value.rfind(":")
    if tag_index > slash_index:
        value = value[:tag_index]
    return value


def _valid_digest_reference(reference: str) -> bool:
    repository, separator, digest = str(reference or "").strip().partition("@sha256:")
    return bool(
        separator
        and repository
        and re.fullmatch(r"[0-9a-fA-F]{64}", digest)
    )


def _resolved_image_digest(image: str) -> str:
    """Resolve a locally available tag/digest to an immutable RepoDigest."""

    result = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{json .RepoDigests}}",
            image,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    if _valid_digest_reference(image):
        return image
    try:
        repo_digests = json.loads(result.stdout.strip() or "null")
    except json.JSONDecodeError:
        return ""
    if not isinstance(repo_digests, list):
        return ""
    requested_repository = _image_repository(image).casefold()
    for item in repo_digests:
        candidate = str(item or "").strip()
        if (
            _valid_digest_reference(candidate)
            and _image_repository(candidate).casefold() == requested_repository
        ):
            return candidate
    return ""


def _smoke_test_official_image(image: str) -> None:
    """Verify a candidate image without exposing the production config/data."""

    admin_password = secrets.token_urlsafe(24)
    container_name = f"grok-account-manager-smoke-{os.getpid()}-{secrets.token_hex(4)}"
    # Keep the temporary bind mounts under the project output directory, which
    # is already shared with Docker Desktop on macOS and avoids TMPDIR sharing
    # differences across hosts. TemporaryDirectory still removes it on exit.
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".grok-account-manager-smoke-", dir=OUTPUT_DIR) as temp_dir:
        root = Path(temp_dir)
        config_path = root / "config.yaml"
        data_dir = root / "data"
        data_dir.mkdir(mode=0o777)
        os.chmod(data_dir, 0o777)
        _write_private_text(config_path, _render_smoke_config(admin_password))

        started = subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--pull",
                "never",
                "--name",
                container_name,
                "--publish",
                f"127.0.0.1::{RELAY_CONTAINER_PORT}",
                "--volume",
                f"{config_path}:/run/grok2api/config.yaml:ro",
                "--volume",
                f"{data_dir}:/app/data",
                image,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if started.returncode != 0:
            raise RuntimeError(f"候选容器启动失败（退出码 {started.returncode}）")
        try:
            port = _docker_published_port(container_name, RELAY_CONTAINER_PORT)
            _verify_smoke_gateway(f"http://127.0.0.1:{port}", admin_password)
        finally:
            subprocess.run(
                ["docker", "rm", "--force", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def _docker_published_port(container_name: str, container_port: int) -> int:
    deadline = time.time() + 5
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "port", container_name, f"{container_port}/tcp"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                port_text = line.rsplit(":", 1)[-1].strip()
                if port_text.isdigit():
                    return int(port_text)
        time.sleep(0.1)
    raise RuntimeError("无法获取候选容器的隔离端口")


def _verify_smoke_gateway(base_url: str, admin_password: str) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            health = requests.get(f"{base_url}/healthz", timeout=2)
            if health.ok:
                break
        except requests.RequestException:
            pass
        time.sleep(0.25)
    else:
        raise RuntimeError("健康检查超时")

    login = requests.post(
        f"{base_url}/api/admin/v1/auth/login",
        json={"username": V2_ADMIN_USERNAME, "password": admin_password},
        timeout=5,
    )
    _raise_response_error(login)
    access_token = _admin_access_token(login.json())
    if not access_token:
        raise RuntimeError("管理员登录未返回 access token")
    headers = {"Authorization": f"Bearer {access_token}"}

    client_key = requests.post(
        f"{base_url}/api/admin/v1/client-keys",
        headers=headers,
        json={
            "name": "grok-account-manager-smoke",
            "enabled": True,
            "providerScope": ["all"],
            "tierScope": ["all"],
        },
        timeout=5,
    )
    _raise_response_error(client_key)
    client_key_data = _response_data(client_key.json())
    if not str(client_key_data.get("secret") or "").strip():
        raise RuntimeError("创建客户端 Key 未返回 secret")

    egress = requests.get(
        f"{base_url}/api/admin/v1/egress-nodes",
        headers=headers,
        params={"scope": "grok_web"},
        timeout=5,
    )
    _raise_response_error(egress)
    try:
        egress_payload = egress.json()
    except Exception as error:
        raise RuntimeError("出口节点 API 返回了无效 JSON") from error
    if not isinstance(egress_payload, dict) or (
        "data" in egress_payload and not isinstance(egress_payload.get("data"), dict)
    ):
        raise RuntimeError("出口节点 API 返回格式无效")


def _response_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data", payload)
    return data if isinstance(data, dict) else {}


def _admin_access_token(payload: Any) -> str:
    data = _response_data(payload)
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        value = tokens.get("accessToken") or tokens.get("access_token")
        if value:
            return str(value).strip()
    return str(data.get("accessToken") or data.get("access_token") or "").strip()


def _runtime_error_text(error: Exception) -> str:
    message = " ".join(str(error or type(error).__name__).split())
    return (message or type(error).__name__)[:300]


def _image_revision() -> str:
    """Read the bundled-source revision stamped into the project-owned image."""
    result = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            f'{{{{ index .Config.Labels "{V2_IMAGE_REVISION_LABEL}" }}}}',
            BUNDLED_V2_IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _mask_secret(value: str) -> str:
    text = str(value or "")
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"


def _credential_to_token(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("sso_token", "sso", "credential", "cookie"):
        value = str(item.get(key) or "").strip()
        if value:
            return value[4:] if value.startswith("sso=") else value
    auth_raw = item.get("auth_raw") if isinstance(item.get("auth_raw"), dict) else {}
    for key in ("sso_token", "sso", "cookie"):
        value = str(auth_raw.get(key) or "").strip()
        if value:
            return value[4:] if value.startswith("sso=") else value
    has_oauth_markers = bool(item.get("refresh_token") or item.get("id_token"))
    if not has_oauth_markers:
        value = str(item.get("access_token") or auth_raw.get("key") or "").strip()
        if value:
            return value[4:] if value.startswith("sso=") else value
    return ""


def _credentials_to_v2_web_accounts(credentials: list[dict]) -> list[dict[str, str]]:
    accounts: list[dict[str, str]] = []
    seen_tokens: set[str] = set()
    for item in credentials:
        if not isinstance(item, dict):
            continue
        token = _credential_to_token(item)
        if not token or token in seen_tokens:
            continue
        seen_tokens.add(token)
        tier = _credential_web_tier(item)
        account = {
            "name": str(item.get("display_name") or item.get("email") or f"Grok Web {len(accounts) + 1}"),
            "email": str(item.get("email") or ""),
            "user_id": str(item.get("user_id") or ""),
            "sso_token": token,
            "tier": tier,
        }
        cookies = str(item.get("grok_cf_cookies") or item.get("cloudflare_cookies") or "").strip()
        if cookies:
            account["cloudflare_cookies"] = cookies
        accounts.append(account)

    return accounts


def _credentials_to_v2_console_accounts(credentials: list[dict]) -> list[dict[str, str]]:
    """Convert browser SSO credentials for the separate Console account pool."""
    accounts: list[dict[str, str]] = []
    seen_tokens: set[str] = set()
    for item in credentials:
        if not isinstance(item, dict):
            continue
        token = _credential_to_token(item)
        if not token or token in seen_tokens:
            continue
        seen_tokens.add(token)
        account = {
            "name": str(item.get("display_name") or item.get("email") or f"Grok Console {len(accounts) + 1}"),
            "email": str(item.get("email") or ""),
            "user_id": str(item.get("user_id") or ""),
            "sso_token": token,
        }
        cookies = str(item.get("grok_cf_cookies") or item.get("cloudflare_cookies") or "").strip()
        if cookies:
            account["cloudflare_cookies"] = cookies
        accounts.append(account)
    return accounts


def _credentials_to_v2_build_accounts(credentials: list[dict]) -> list[dict[str, str]]:
    accounts: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in credentials:
        if not isinstance(item, dict):
            continue
        refresh_token = str(item.get("refresh_token") or "").strip()
        access_token = str(item.get("access_token") or "").strip()
        if not refresh_token or (refresh_token in seen):
            continue
        seen.add(refresh_token)
        entry = {
            "provider": "grok_build",
            "name": str(item.get("display_name") or item.get("email") or f"Grok Build {len(accounts) + 1}"),
            "email": str(item.get("email") or ""),
            "user_id": str(item.get("user_id") or ""),
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": str(item.get("id_token") or ""),
            "token_type": str(item.get("token_type") or "Bearer"),
            "client_id": str(item.get("oidc_client_id") or ""),
            "team_id": str(item.get("team_id") or ""),
        }
        expires_at = str(item.get("expires_at_raw") or item.get("expires_at") or "").strip()
        if expires_at:
            entry["expires_at"] = expires_at
        accounts.append(entry)
    return accounts


def _credential_web_tier(credential: dict) -> str:
    values = [
        credential.get("plan_type"),
        credential.get("subscription_tier"),
        (credential.get("quota") or {}).get("subscriptionTier") if isinstance(credential.get("quota"), dict) else "",
    ]
    text = " ".join(str(value or "").lower() for value in values)
    if "heavy" in text:
        return "heavy"
    if any(marker in text for marker in ("super", "premium", "pro")):
        return "super"
    return "basic"


def _parse_import_response(response: requests.Response) -> dict:
    content_type = str(response.headers.get("Content-Type") or "").lower()
    text = str(response.text or "").strip()
    if "json" in content_type or text.startswith("{"):
        try:
            payload = response.json()
        except Exception as error:
            raise RuntimeError("账号导入返回了无效 JSON") from error
        result = _response_data(payload)
        if not result:
            raise RuntimeError("账号导入未返回结果")
        return result
    return _parse_sse_result(text)


def _parse_sse_result(payload: str) -> dict:
    complete: dict = {}
    for block in payload.replace("\r\n", "\n").split("\n\n"):
        event = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        try:
            data = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if event == "error":
            message = data.get("message") if isinstance(data, dict) else "导入账号失败"
            raise RuntimeError(str(message or "导入账号失败"))
        if event == "complete" and isinstance(data, dict):
            complete = data
    if not complete:
        raise RuntimeError("账号导入未返回完成结果")
    return complete


def _api_headers(config: RelayConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }


def _forward_headers(headers: dict[str, str]) -> dict[str, str]:
    blocked = {
        "host",
        "content-length",
        "connection",
        "accept-encoding",
        "transfer-encoding",
    }
    forwarded = {}
    for key, value in headers.items():
        if key.lower() in blocked:
            continue
        forwarded[key] = value
    return forwarded


def _raise_response_error(response: requests.Response) -> None:
    if response.ok:
        return
    raise RuntimeError(_response_error_text(response))


def _response_error_text(response: requests.Response) -> str:
    status = response.status_code
    try:
        data = response.json()
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            message = str(error.get("message") or error)
            return f"grok2api 请求失败 (HTTP {status})：{message}"
        if error:
            return f"grok2api 请求失败 (HTTP {status})：{error}"
        return f"grok2api 请求失败 (HTTP {status})：{data}"
    except Exception:
        return f"grok2api 请求失败 (HTTP {status})：{response.text[:400]}"


def _tail(path: Path, max_chars: int = 3000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return text[-max_chars:]


def _model_capability(model_info: dict[str, Any], model_id: str) -> str:
    raw = str(model_info.get("capability") or model_info.get("type") or "").strip().lower()
    if raw in {"chat", "image", "image_edit", "video"}:
        return raw
    return _guess_capability(model_id)


def _guess_capability(model_id: str) -> str:
    lower = model_id.lower()
    if "image-edit" in lower:
        return "image_edit"
    if "image" in lower:
        return "image"
    if "video" in lower:
        return "video"
    if any(marker in lower for marker in CHAT_MODEL_MARKERS):
        return "chat"
    if lower.startswith("grok-"):
        return "chat"
    return "unknown"


def _extract_chat_text(payload: dict) -> str:
    try:
        choices = payload.get("choices") or []
        first = choices[0] if choices else {}
        message = first.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            return " ".join(parts).strip()
    except Exception:
        return ""
    return ""


RELAY_MANAGER = RelayManager()
