"""Local Grok2API relay manager.

This module treats the sibling grok2api checkout as a local OpenAI-compatible
gateway.  The account manager keeps owning registration and credential export;
the relay manager starts/stops grok2api, imports local SSO tokens, and runs
small API probes through the configured local key.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from ...core.browser import PROJECT_ROOT


OUTPUT_DIR = PROJECT_ROOT / "output"
CONFIG_PATH = OUTPUT_DIR / "relay-config.json"
LOG_PATH = OUTPUT_DIR / "grok2api-relay.log"
DATA_DIR = OUTPUT_DIR / "grok2api-data"
LOG_DIR = OUTPUT_DIR / "grok2api-logs"
_LEGACY_GROK2API_PATH = PROJECT_ROOT.parent.parent / "未命名文件夹" / "grok2api"
_SIBLING_GROK2API_PATH = PROJECT_ROOT.parent / "grok2api"
DEFAULT_GROK2API_PATH = Path(
    os.environ.get("GROK2API_PATH")
    or os.environ.get("GROK_ACCOUNT_MANAGER_GROK2API_PATH")
    or (_LEGACY_GROK2API_PATH if _LEGACY_GROK2API_PATH.exists() else _SIBLING_GROK2API_PATH)
)
SSO_TXT_PATH = OUTPUT_DIR / "sso.txt"

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
    port: int = 8000
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

    def snapshot(self) -> dict:
        config = self._config
        status = self.status()
        public_base_url = os.environ.get("GROK_ACCOUNT_MANAGER_PUBLIC_BASE_URL", "http://127.0.0.1:8765")
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
                "dataDir": str(DATA_DIR),
                "logPath": str(LOG_PATH),
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
                current["port"] = _safe_port(patch.get("port"), 8000)
            if "apiKey" in patch:
                current["api_key"] = str(patch.get("apiKey") or "").strip()
            if "adminKey" in patch:
                current["admin_key"] = str(patch.get("adminKey") or "").strip()
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

    def start(self) -> dict:
        with self._lock:
            if self.is_running():
                self.apply_remote_config()
                return self.snapshot()

            project_dir = Path(self._config.grok2api_path).expanduser().resolve()
            if not (project_dir / "app" / "main.py").exists():
                raise ValueError(f"未找到 grok2api 项目入口：{project_dir}/app/main.py")

            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            LOG_DIR.mkdir(parents=True, exist_ok=True)

            env = os.environ.copy()
            env.update(
                {
                    "DATA_DIR": str(DATA_DIR),
                    "LOG_DIR": str(LOG_DIR),
                    "SERVER_HOST": self._config.host,
                    "SERVER_PORT": str(self._config.port),
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
            command = _grok2api_command(project_dir, self._config.host, self._config.port)
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

    def apply_remote_config(self) -> dict:
        cfg = self._config
        response = requests.post(
            f"{cfg.base_url}/admin/api/config",
            headers=_admin_headers(cfg),
            json={"app": {"api_key": cfg.api_key, "app_key": cfg.admin_key, "app_url": cfg.base_url}},
            timeout=15,
        )
        _raise_response_error(response)
        return response.json()

    def sync_accounts(self, credentials: list[dict], *, refresh_existing: bool = False) -> dict:
        cfg = self._config
        tokens = _credentials_to_tokens(credentials)
        if not tokens:
            raise ValueError("没有找到可同步的 sso/access_token")
        if not self.is_running():
            self.start()
        response = requests.post(
            f"{cfg.base_url}/admin/api/tokens/add",
            headers=_admin_headers(cfg),
            json={"tokens": tokens, "pool": "auto", "tags": ["grok-account-manager"]},
            timeout=30,
        )
        _raise_response_error(response)
        result = response.json()
        refresh_result = None
        skipped = _safe_int(result.get("skipped") if isinstance(result, dict) else 0, 0)
        if refresh_existing and skipped:
            refresh_result = self.refresh_tokens(tokens)
        self.force_sync()
        payload = {"requested": len(tokens), "result": result}
        if refresh_result is not None:
            payload["refresh"] = refresh_result
        return payload

    def replace_accounts(self, credentials: list[dict], *, pool: str = "basic", prune_unlisted: bool = True) -> dict:
        """Replace the relay runtime pool with the current project account tokens."""
        cfg = self._config
        tokens = _credentials_to_tokens(credentials)
        if not tokens:
            raise ValueError("没有找到可同步的 sso/access_token")
        if not self.is_running():
            self.start()

        token_items = [{"token": token, "tags": ["grok-account-manager"]} for token in tokens]
        response = requests.post(
            f"{cfg.base_url}/admin/api/tokens",
            headers=_admin_headers(cfg),
            json={pool: token_items},
            timeout=60,
        )
        _raise_response_error(response)
        replace_result = response.json()

        prune_result = None
        if prune_unlisted:
            active_tokens = self.list_tokens().get("tokens", [])
            stale_tokens = [
                str(item.get("token") or "").strip()
                for item in active_tokens
                if isinstance(item, dict) and str(item.get("token") or "").strip() not in tokens
            ]
            if stale_tokens:
                prune_result = self.delete_tokens(stale_tokens)

        self.force_sync()
        payload = {"requested": len(tokens), "result": replace_result}
        if prune_result is not None:
            payload["prune"] = prune_result
        return payload

    def list_tokens(self) -> dict:
        cfg = self._config
        response = requests.get(
            f"{cfg.base_url}/admin/api/tokens",
            headers=_admin_headers(cfg),
            timeout=20,
        )
        _raise_response_error(response)
        return response.json()

    def delete_tokens(self, tokens: list[str]) -> dict:
        cfg = self._config
        clean_tokens = [str(token or "").strip() for token in tokens if str(token or "").strip()]
        if not clean_tokens:
            return {"deleted": 0}
        response = requests.delete(
            f"{cfg.base_url}/admin/api/tokens",
            headers=_admin_headers(cfg),
            json=clean_tokens,
            timeout=60,
        )
        _raise_response_error(response)
        return response.json()

    def refresh_tokens(self, tokens: list[str]) -> dict:
        cfg = self._config
        clean_tokens = [str(token or "").strip() for token in tokens if str(token or "").strip()]
        if not clean_tokens:
            return {"summary": {"total": 0, "ok": 0, "fail": 0}}
        response = requests.post(
            f"{cfg.base_url}/admin/api/batch/refresh",
            headers=_admin_headers(cfg),
            json={"tokens": clean_tokens},
            timeout=120,
        )
        _raise_response_error(response)
        return response.json()

    def force_sync(self) -> dict:
        cfg = self._config
        response = requests.post(
            f"{cfg.base_url}/admin/api/sync",
            headers=_admin_headers(cfg),
            timeout=20,
        )
        _raise_response_error(response)
        return response.json()

    def list_models(self) -> dict:
        cfg = self._config
        response = requests.get(
            f"{cfg.base_url}/v1/models",
            headers=_api_headers(cfg),
            timeout=20,
        )
        _raise_response_error(response)
        return response.json()

    def send_chat_completion(self, *, model: str, messages: list[dict], timeout: int = 120) -> dict:
        if not self.is_running():
            self.start()
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

    def generate_image(self, *, model: str, prompt: str, n: int, size: str, timeout: int = 120) -> dict:
        if not self.is_running():
            self.start()
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
        timeout: int = 120,
    ) -> requests.Response:
        if not self.is_running():
            self.start()
        cfg = self._config
        target_url = f"{cfg.base_url}{path}"
        if query:
            target_url = f"{target_url}?{query}"

        forward_headers = _forward_headers(headers or {})
        if "authorization" not in {key.lower() for key in forward_headers}:
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

    def _healthcheck(self) -> bool:
        try:
            response = requests.get(f"{self._config.base_url}/health", timeout=2)
            return response.ok
        except Exception:
            return False

    def _load_config(self) -> RelayConfig:
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                return RelayConfig(
                    grok2api_path=str(data.get("grok2api_path") or data.get("grok2apiPath") or DEFAULT_GROK2API_PATH),
                    host=str(data.get("host") or "127.0.0.1"),
                    port=_safe_port(data.get("port"), 8000),
                    api_key=str(data.get("api_key") or data.get("apiKey") or "local-grok-api-key"),
                    admin_key=str(data.get("admin_key") or data.get("adminKey") or "grok2api"),
                )
            except Exception:
                pass
        return RelayConfig()

    def _save_config(self, config: RelayConfig) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(asdict(config), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _safe_port(value: Any, default: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        port = default
    return max(1, min(65535, port))


def _grok2api_command(project_dir: Path, host: str, port: int) -> list[str]:
    granian = project_dir / ".venv" / "bin" / "granian"
    base = [str(granian)] if granian.exists() else ["uv", "run", "granian"]
    return [
        *base,
        "--interface",
        "asgi",
        "--host",
        host,
        "--port",
        str(port),
        "--workers",
        "1",
        "app.main:app",
    ]


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


def _credentials_to_tokens(credentials: list[dict]) -> list[str]:
    tokens = [_credential_to_token(item) for item in credentials]
    if not any(tokens):
        tokens = _read_sso_txt_tokens()
    return [token for token in dict.fromkeys(tokens) if token]


def _read_sso_txt_tokens() -> list[str]:
    try:
        lines = SSO_TXT_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    tokens = []
    for line in lines:
        token = line.strip()
        if token.startswith("sso="):
            token = token[4:]
        if token:
            tokens.append(token)
    return tokens


def _admin_headers(config: RelayConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.admin_key}",
        "Content-Type": "application/json",
    }


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
    try:
        data = response.json()
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error)
        return str(data)
    except Exception:
        return f"HTTP {response.status_code}: {response.text[:400]}"


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
