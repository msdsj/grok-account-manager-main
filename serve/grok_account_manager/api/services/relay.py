"""Local grok2api relay manager.

The relay is intentionally backed by the current Docker-based grok2api
checkout only.  Registration remains owned by this project, while the
upstream account pool and its encrypted database are owned by grok2api.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
import time
from typing import Any

import requests

from ...core.browser import PROJECT_ROOT


OUTPUT_DIR = PROJECT_ROOT / "output"
CONFIG_PATH = OUTPUT_DIR / "relay-config.json"
LOG_PATH = OUTPUT_DIR / "grok2api-relay.log"
V2_DATA_DIR = OUTPUT_DIR / "grok2api-v2-data"
V2_CONFIG_PATH = OUTPUT_DIR / "grok2api-v2-config.yaml"
V2_IMAGE = "grok-account-manager-grok2api:local"
V2_ADMIN_USERNAME = "grok-account-manager"
DEFAULT_RELAY_PORT = 43871
LEGACY_RELAY_PORT = 8000
RELAY_CONTAINER_PORT = 43871
DEFAULT_GROK2API_PATH = Path(
    os.environ.get("GROK2API_PATH")
    or os.environ.get("GROK_ACCOUNT_MANAGER_GROK2API_PATH")
    or (Path.home() / "Downloads" / "grok2api-main")
)

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
    grok2api_path: str = str(DEFAULT_GROK2API_PATH)
    host: str = "127.0.0.1"
    port: int = DEFAULT_RELAY_PORT
    api_key: str = "local-grok-api-key"
    admin_key: str = "grok2api"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class RelayManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._config = self._load_config()

    def _project_dir(self) -> Path:
        return Path(self._config.grok2api_path).expanduser().resolve()

    def snapshot(self) -> dict:
        config = self._config
        status = self.status()
        public_base_url = os.environ.get("GROK_ACCOUNT_MANAGER_PUBLIC_BASE_URL", "http://127.0.0.1:43187")
        return {
            **status,
            "config": {
                "grok2apiPath": config.grok2api_path,
                "host": config.host,
                "port": config.port,
                "baseUrl": config.base_url,
                "publicBaseUrl": public_base_url,
                "apiKey": config.api_key,
                "apiKeyMasked": _mask_secret(config.api_key),
                "adminKey": config.admin_key,
                "adminKeyMasked": _mask_secret(config.admin_key),
                "dataDir": str(V2_DATA_DIR),
                "logPath": str(LOG_PATH),
                "engine": "grok2api",
            },
        }

    def update_config(self, patch: dict[str, Any]) -> dict:
        with self._lock:
            current = asdict(self._config)
            if "grok2apiPath" in patch:
                current["grok2api_path"] = str(patch.get("grok2apiPath") or "").strip()
            if "host" in patch:
                current["host"] = str(patch.get("host") or "127.0.0.1").strip() or "127.0.0.1"
            if "port" in patch:
                current["port"] = _safe_port(patch.get("port"), DEFAULT_RELAY_PORT)
            if "apiKey" in patch:
                current["api_key"] = str(patch.get("apiKey") or "").strip()
            if "adminKey" in patch:
                current["admin_key"] = str(patch.get("adminKey") or "").strip()
            if not current["api_key"]:
                raise ValueError("API 秘钥不能为空")
            if not current["admin_key"]:
                raise ValueError("管理秘钥不能为空")
            project_dir = Path(current["grok2api_path"]).expanduser()
            if not (project_dir / "backend" / "go.mod").exists():
                raise ValueError("grok2api 路径必须指向当前新版项目，并包含 backend/go.mod")
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

    def start(self) -> dict:
        with self._lock:
            if self.is_running():
                self.apply_remote_config()
                return self.snapshot()

            project_dir = self._project_dir()
            self._ensure_v2_runtime(project_dir)

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
            log_file.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] starting grok2api\n")
            log_file.flush()
            command = _grok2api_v2_command(self._config, V2_CONFIG_PATH)
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=str(project_dir),
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                log_file.close()

        deadline = time.time() + 20
        while time.time() < deadline:
            if self._healthcheck():
                self.apply_remote_config()
                return self.snapshot()
            with self._lock:
                if self._process is not None and self._process.poll() is not None:
                    raise RuntimeError(f"grok2api 启动失败，退出码 {self._process.returncode}。请查看 {LOG_PATH}")
            time.sleep(0.5)
        raise TimeoutError(f"grok2api 启动超时。请查看 {LOG_PATH}")

    def stop(self) -> dict:
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
        return self.snapshot()

    def apply_remote_config(self, clearance: dict[str, str] | None = None) -> dict:
        del clearance
        return self._ensure_v2_client_key()

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

    def _ensure_v2_runtime(self, project_dir: Path) -> None:
        if not (project_dir / "backend" / "go.mod").exists():
            raise ValueError(f"未找到新版 grok2api 项目入口：{project_dir}/backend/go.mod")
        if not shutil.which("docker"):
            raise RuntimeError("新版 grok2api 需要 Docker Desktop，但未找到 docker 命令")

        docker_info = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if docker_info.returncode != 0:
            raise RuntimeError("新版 grok2api 需要 Docker Desktop，请先启动 Docker Desktop 后再启动中转")

        self._ensure_v2_config(project_dir)
        image_check = subprocess.run(
            ["docker", "image", "inspect", V2_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if image_check.returncode == 0:
            return

        with LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] building grok2api v2 image\n")
            log_file.flush()
            build = subprocess.run(
                ["docker", "build", "--tag", V2_IMAGE, str(project_dir)],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if build.returncode != 0:
            raise RuntimeError(f"新版 grok2api 镜像构建失败，退出码 {build.returncode}。请查看 {LOG_PATH}")

    def _ensure_v2_config(self, project_dir: Path) -> None:
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

        template_path = project_dir / "config.example.yaml"
        try:
            config_text = template_path.read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError(f"无法读取新版 grok2api 配置模板：{error}") from error
        config_text = _rewrite_relay_listen(config_text)
        replacements = {
            'jwtSecret: "replace-with-at-least-32-characters"': f'jwtSecret: "{secrets.token_hex(32)}"',
            'credentialEncryptionKey: "replace-with-base64-key"': (
                f'credentialEncryptionKey: "{base64.b64encode(secrets.token_bytes(32)).decode("ascii")}"'
            ),
            'username: "admin"': f'username: "{V2_ADMIN_USERNAME}"',
            'password: "replace-with-a-strong-password"': f'password: "{self._config.admin_key}"',
        }
        for old, new in replacements.items():
            config_text = config_text.replace(old, new)
        _write_private_text(V2_CONFIG_PATH, config_text)

    def _v2_admin_headers(self) -> dict[str, str]:
        cfg = self._config
        response = requests.post(
            f"{cfg.base_url}/api/admin/v1/auth/login",
            json={"username": V2_ADMIN_USERNAME, "password": cfg.admin_key},
            timeout=20,
        )
        _raise_response_error(response)
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        tokens = data.get("tokens") if isinstance(data, dict) else None
        access_token = str(tokens.get("accessToken") or "").strip() if isinstance(tokens, dict) else ""
        if not access_token:
            raise RuntimeError("新版 grok2api 管理员登录没有返回 access token")
        return {"Authorization": f"Bearer {access_token}"}

    def _ensure_v2_client_key(self) -> dict:
        if self._config.api_key.startswith("g2a_"):
            return {"managedApiKey": True}
        response = requests.post(
            f"{self._config.base_url}/api/admin/v1/client-keys",
            headers=self._v2_admin_headers(),
            json={"name": "grok-account-manager", "enabled": True, "accountPool": "free"},
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
        response = requests.post(
            f"{self._config.base_url}{path}",
            headers=self._v2_admin_headers(),
            files={"file": (filename, json.dumps(document, ensure_ascii=False).encode("utf-8"), "application/json")},
            timeout=180,
        )
        _raise_response_error(response)
        return _parse_sse_result(response.text)

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
                configured_path = Path(
                    str(data.get("grok2api_path") or data.get("grok2apiPath") or DEFAULT_GROK2API_PATH)
                ).expanduser()
                if not (configured_path / "backend" / "go.mod").exists():
                    configured_path = DEFAULT_GROK2API_PATH
                configured_port = _safe_port(data.get("port"), DEFAULT_RELAY_PORT)
                if configured_port == LEGACY_RELAY_PORT:
                    configured_port = DEFAULT_RELAY_PORT
                return RelayConfig(
                    grok2api_path=str(configured_path),
                    host=str(data.get("host") or "127.0.0.1"),
                    port=configured_port,
                    api_key=str(data.get("api_key") or data.get("apiKey") or "local-grok-api-key"),
                    admin_key=str(data.get("admin_key") or data.get("adminKey") or "grok2api"),
                )
            except Exception:
                pass
        return RelayConfig()

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


def _safe_port(value: Any, default: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        port = default
    return max(1, min(65535, port))


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


def _grok2api_v2_command(config: RelayConfig, config_path: Path) -> list[str]:
    container_name = f"grok-account-manager-grok2api-{config.port}"
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
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
        V2_IMAGE,
        "/app/grok2api",
        # grok2api's entrypoint copies /run/grok2api/config.yaml to /app/config.yaml
        # (chowned to its non-root runtime user) before dropping privileges via
        # su-exec, so the app process itself must be pointed at the copy, not the
        # read-only mount, or it may not have permission to read it.
        "--config",
        "/app/config.yaml",
        "--listen",
        f"0.0.0.0:{RELAY_CONTAINER_PORT}",
    ]


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
    return complete or {"accepted": True}


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
