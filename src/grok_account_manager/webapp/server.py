"""本地  Web 控制台：注册任务 API + 静态前端文件服务。"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import queue
import threading
import time
import traceback
import uuid
from urllib.parse import urlparse

from dotenv import load_dotenv

from ..providers.grok import GrokProvider
from ..mail.sources import build_mailbox_source
from ..core.browser import (
    DrissionBrowserSession,
    PROJECT_ROOT,
    build_chromium_options,
    ensure_stable_python_runtime,
    warn_runtime_compatibility,
)
from ..sinks.json_credential import JsonCredentialSink
from ..sinks.cpa_credential import build_cpa_download
from ..sinks.txt_file import TxtFileSink
from ..grok.account_tester import test_grok_account
from .relay import RELAY_MANAGER

OUTPUT_DIR = PROJECT_ROOT / "output"
CREDENTIALS_DIR = OUTPUT_DIR / "credentials"
TXT_OUTPUT = OUTPUT_DIR / "sso.txt"
ACCOUNT_TEST_RESULTS_PATH = OUTPUT_DIR / "account-test-results.json"
WEB_DIST_DIR = PROJECT_ROOT / "web" / "dist"
DEFAULT_MAX_CONCURRENCY = 20
ROUND_TIMEOUT_SECONDS = 120
_ACCOUNTS_CACHE_LOCK = threading.RLock()
_ACCOUNTS_CACHE_SIGNATURE: tuple[tuple[str, int, int], ...] | None = None
_ACCOUNTS_CACHE_DATA: list[dict] = []


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8") or "{}")


def _safe_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


class _CombinedStopEvent:
    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


def _format_created_at(value) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    # GrokAccount JSON 用毫秒；兼容少数秒级时间戳。
    seconds = timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))


def _account_export_key(file_path: Path, item_index: int) -> str:
    return f"{file_path.name}:{item_index}"


def _account_from_credential(credential: dict, file_path: Path, item_index: int = 0) -> dict:
    email = str(credential.get("email") or "").strip()
    first_name = str(credential.get("first_name") or "").strip()
    last_name = str(credential.get("last_name") or "").strip()
    refresh_token = str(credential.get("refresh_token") or "").strip()
    access_token = str(credential.get("access_token") or "").strip()
    created_at = credential.get("created_at")
    account_id = str(credential.get("id") or "").strip() or file_path.stem

    quota = credential.get("quota") or {}
    frequent_usage = quota.get("frequentUsage")
    frequent_limit = quota.get("frequentLimit")
    occasional_usage = quota.get("occasionalUsage")
    occasional_limit = quota.get("occasionalLimit")
    weekly_used = quota.get("weeklyUsed")
    weekly_total = quota.get("weeklyTotal")
    weekly_pct = quota.get("weeklyLimitPercent")

    return {
        "id": account_id,
        "exportKey": _account_export_key(file_path, item_index),
        "email": email or "unknown",
        "displayName": " ".join(part for part in [first_name, last_name] if part).strip(),
        "authMode": credential.get("auth_mode") or "oauth",
        "planType": credential.get("plan_type") or "",
        "userId": credential.get("user_id") or "",
        "createdAt": created_at or 0,
        "createdAtLabel": _format_created_at(created_at),
        "hasRefreshToken": bool(refresh_token),
        "hasAccessToken": bool(access_token),
        "fileName": file_path.name,
        "filePath": str(file_path),
        "quota": {
            "frequentUsage": frequent_usage,
            "frequentLimit": frequent_limit,
            "occasionalUsage": occasional_usage,
            "occasionalLimit": occasional_limit,
            "weeklyUsed": weekly_used,
            "weeklyTotal": weekly_total,
            "weeklyLimitPercent": weekly_pct,
        },
        "usageUpdatedAt": credential.get("usage_updated_at") or 0,
    }


def _load_account_test_results() -> dict[str, dict]:
    try:
        data = json.loads(ACCOUNT_TEST_RESULTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, list):
        return {}
    results: dict[str, dict] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        export_key = str(item.get("exportKey") or "").strip()
        if export_key:
            results[export_key] = item
    return results


def _save_account_test_results(results: list[dict]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNT_TEST_RESULTS_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _merge_account_test_result(account: dict, test_results: dict[str, dict]) -> dict:
    result = test_results.get(str(account.get("exportKey") or ""))
    if not result:
        return account
    account["availability"] = {
        "category": result.get("category") or "unavailable",
        "baseAvailable": bool(result.get("baseAvailable")),
        "grok45Available": bool(result.get("grok45Available")),
        "baseModel": result.get("baseModel"),
        "grok45Model": result.get("grok45Model"),
        "latencyMs": result.get("latencyMs"),
        "error": result.get("error"),
        "testedAt": result.get("testedAt"),
    }
    return account


def list_accounts() -> list[dict]:
    global _ACCOUNTS_CACHE_SIGNATURE, _ACCOUNTS_CACHE_DATA

    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    files: list[tuple[Path, int, int]] = []
    for file_path in CREDENTIALS_DIR.glob("*.json"):
        try:
            stat = file_path.stat()
            files.append((file_path, stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue

    test_result_sig = ("account-test-results.json", 0, 0)
    try:
        stat = ACCOUNT_TEST_RESULTS_PATH.stat()
        test_result_sig = ("account-test-results.json", stat.st_mtime_ns, stat.st_size)
    except OSError:
        pass

    files.sort(key=lambda item: item[1], reverse=True)
    signature = tuple((file_path.name, modified_at, size) for file_path, modified_at, size in files) + (test_result_sig,)
    with _ACCOUNTS_CACHE_LOCK:
        if signature == _ACCOUNTS_CACHE_SIGNATURE:
            return [dict(account) for account in _ACCOUNTS_CACHE_DATA]

    accounts: list[dict] = []
    test_results = _load_account_test_results()
    for file_path, _, _ in files:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else [data]
            for index, item in enumerate(items):
                if isinstance(item, dict):
                    accounts.append(_merge_account_test_result(_account_from_credential(item, file_path, index), test_results))
        except Exception as error:
            accounts.append(
                {
                    "id": file_path.stem,
                    "exportKey": _account_export_key(file_path, 0),
                    "email": "读取失败",
                    "displayName": "",
                    "authMode": "",
                    "planType": "",
                    "userId": "",
                    "createdAt": 0,
                    "createdAtLabel": "",
                    "hasRefreshToken": False,
                    "hasAccessToken": False,
                    "fileName": file_path.name,
                    "filePath": str(file_path),
                    "error": str(error),
                }
            )
    with _ACCOUNTS_CACHE_LOCK:
        _ACCOUNTS_CACHE_SIGNATURE = signature
        _ACCOUNTS_CACHE_DATA = [dict(account) for account in accounts]
    return accounts


def _selected_credential_refs(export_keys: list[str]) -> list[tuple[Path, int, dict, list, bool]]:
    selected_by_file: dict[str, set[int]] = {}
    for key in export_keys:
        raw_key = str(key or "").strip()
        if ":" not in raw_key:
            continue
        file_name, index_text = raw_key.rsplit(":", 1)
        safe_file_name = Path(file_name).name
        if safe_file_name != file_name:
            continue
        try:
            item_index = int(index_text)
        except ValueError:
            continue
        if item_index < 0:
            continue
        selected_by_file.setdefault(safe_file_name, set()).add(item_index)

    refs: list[tuple[Path, int, dict, list, bool]] = []
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    for file_name, indexes in selected_by_file.items():
        file_path = (CREDENTIALS_DIR / file_name).resolve()
        if CREDENTIALS_DIR.resolve() != file_path.parent or not file_path.exists():
            continue
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        is_list = isinstance(data, list)
        items = data if is_list else [data]
        for index, item in enumerate(items):
            if index in indexes and isinstance(item, dict):
                refs.append((file_path, index, item, items, is_list))
    return refs


def export_credentials(export_keys: list[str]) -> list[dict]:
    selected_by_file: dict[str, set[int]] = {}
    for key in export_keys:
        raw_key = str(key or "").strip()
        if ":" not in raw_key:
            continue
        file_name, index_text = raw_key.rsplit(":", 1)
        safe_file_name = Path(file_name).name
        if safe_file_name != file_name:
            continue
        try:
            item_index = int(index_text)
        except ValueError:
            continue
        if item_index < 0:
            continue
        selected_by_file.setdefault(safe_file_name, set()).add(item_index)

    if not selected_by_file:
        raise ValueError("请选择要导出的账号")

    exported: list[dict] = []
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    file_paths = []
    for file_name in selected_by_file:
        file_path = (CREDENTIALS_DIR / file_name).resolve()
        if CREDENTIALS_DIR.resolve() == file_path.parent and file_path.exists():
            file_paths.append(file_path)

    for file_path in sorted(file_paths, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        items = data if isinstance(data, list) else [data]
        for index, item in enumerate(items):
            if index in selected_by_file.get(file_path.name, set()) and isinstance(item, dict):
                exported.append(item)

    if not exported:
        raise ValueError("没有找到可导出的账号 JSON")
    return exported


def test_selected_accounts(export_keys: list[str], timeout: int = 120) -> dict:
    global _ACCOUNTS_CACHE_SIGNATURE
    refs = _selected_credential_refs(export_keys)
    if not refs:
        raise ValueError("请选择要测试的账号")

    previous_results = _load_account_test_results()
    merged_results = {key: dict(value) for key, value in previous_results.items()}
    results: list[dict] = []
    for file_path, index, credential, items, is_list in refs:
        export_key = _account_export_key(file_path, index)
        result, updated = test_grok_account(dict(credential), timeout=timeout)
        result["exportKey"] = export_key
        result["id"] = str(credential.get("id") or "").strip() or file_path.stem
        result["fileName"] = file_path.name
        results.append(result)
        merged_results[export_key] = result

        if updated != credential:
            items[index] = updated
            out = items if is_list else items[0]
            file_path.write_text(
                json.dumps(out, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    _save_account_test_results(
        sorted(merged_results.values(), key=lambda item: int(item.get("testedAt") or 0), reverse=True)
    )
    with _ACCOUNTS_CACHE_LOCK:
        _ACCOUNTS_CACHE_SIGNATURE = None
    return {"results": results, "accounts": list_accounts()}


def export_all_credentials() -> list[dict]:
    exported: list[dict] = []
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    for file_path in sorted(CREDENTIALS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        exported.extend(item for item in items if isinstance(item, dict))
    return exported


class RegistrationJobManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._job: dict | None = None
        self._sessions: set[DrissionBrowserSession] = set()
        self._mail_source = None

    def snapshot(self) -> dict | None:
        with self._lock:
            if self._job is None:
                return None
            return json.loads(json.dumps(self._job, default=_json_default))

    def start(
        self,
        total: int,
        concurrency: int,
        oauth_exchange: bool,
        email_source: str = "duckmail",
        outlook_data: str = "",
        outlook_accounts_file: str = "",
        google_data: str = "",
        google_accounts_file: str = "",
    ) -> dict:
        total = _safe_int(total, default=1, minimum=1, maximum=10_000)
        requested_concurrency = _safe_int(concurrency, default=1, minimum=1, maximum=20)
        max_concurrency = _safe_int(
            os.environ.get("GROK_ACCOUNT_MANAGER_MAX_CONCURRENCY"),
            default=DEFAULT_MAX_CONCURRENCY,
            minimum=1,
            maximum=20,
        )
        concurrency = min(requested_concurrency, max_concurrency)
        concurrency = min(concurrency, total)
        mail_source = build_mailbox_source(
            email_source=email_source,
            outlook_data=outlook_data,
            outlook_file=outlook_accounts_file,
            google_data=google_data,
            google_file=google_accounts_file,
        )

        with self._lock:
            if self._job and self._job.get("status") in {"running", "stopping"}:
                raise RuntimeError("已有注册任务正在运行")

            self._stop_event = threading.Event()
            self._mail_source = mail_source
            self._job = {
                "id": uuid.uuid4().hex,
                "status": "running",
                "total": total,
                "concurrency": concurrency,
                "oauthExchange": bool(oauth_exchange),
                "emailSource": getattr(mail_source, "name", email_source),
                "outlookAccountCount": getattr(mail_source, "count", 0) if getattr(mail_source, "name", "") == "outlook" else 0,
                "googleAccountCount": getattr(mail_source, "count", 0) if getattr(mail_source, "name", "") in {"gmail", "google"} else 0,
                "issued": 0,
                "completed": 0,
                "failed": 0,
                "workerErrors": 0,
                "active": 0,
                "roundTimeoutSeconds": ROUND_TIMEOUT_SECONDS,
                "failedAccounts": [],
                "startedAt": _now_ms(),
                "finishedAt": None,
                "events": [],
            }
            self._event_locked("info", f"任务启动：总数 {total}，并发 {concurrency}")
            if concurrency < requested_concurrency:
                self._event_locked(
                    "warning",
                    f"为降低本机压力，并发已从 {requested_concurrency} 限制为 {concurrency}",
                )

            self._thread = threading.Thread(
                target=self._run_job,
                args=(self._job["id"],),
                daemon=True,
            )
            self._thread.start()
            return self.snapshot() or {}

    def stop(self) -> dict | None:
        with self._lock:
            sessions_to_close: list[DrissionBrowserSession] = []
            if self._job and self._job.get("status") in {"running", "stopping"}:
                self._job["status"] = "stopping"
                self._event_locked("warning", "已请求停止，正在关闭所有浏览器进程")
                self._stop_event.set()
                sessions_to_close = list(self._sessions)
            snapshot = self.snapshot()
        if sessions_to_close:
            self._close_sessions(sessions_to_close)
        return snapshot

    def _close_sessions(self, sessions: list[DrissionBrowserSession]) -> None:
        def _close_one(session: DrissionBrowserSession) -> None:
            try:
                session.stop()
            except Exception:
                pass

        threads = [threading.Thread(target=_close_one, args=(session,), daemon=True) for session in sessions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

    def _register_session(self, session: DrissionBrowserSession) -> None:
        with self._lock:
            self._sessions.add(session)

    def _unregister_session(self, session: DrissionBrowserSession) -> None:
        with self._lock:
            self._sessions.discard(session)

    def _event_locked(self, level: str, message: str, **extra) -> None:
        if self._job is None:
            return
        events = self._job.setdefault("events", [])
        events.append(
            {
                "id": uuid.uuid4().hex,
                "time": _now_ms(),
                "level": level,
                "message": message,
                **extra,
            }
        )
        del events[:-240]

    def _event(self, level: str, message: str, **extra) -> None:
        with self._lock:
            self._event_locked(level, message, **extra)

    def _next_round(self) -> int | None:
        with self._lock:
            if self._job is None:
                return None
            if self._job.get("status") != "running":
                return None
            if self._stop_event.is_set():
                return None
            if self._job["issued"] >= self._job["total"]:
                return None
            self._job["issued"] += 1
            self._job["active"] += 1
            return int(self._job["issued"])

    def _round_finished(self, ok: bool, cancelled: bool = False) -> None:
        with self._lock:
            if self._job is None:
                return
            self._job["active"] = max(0, int(self._job["active"]) - 1)
            if cancelled:
                return
            if ok:
                self._job["completed"] += 1
            else:
                self._job["failed"] += 1

    def _worker_start_failed(self) -> None:
        with self._lock:
            if self._job is not None:
                self._job["workerErrors"] += 1

    def _record_failed_account(
        self,
        *,
        email: str,
        round_index: int,
        worker_index: int,
        stage: str,
        reason: str,
        timed_out: bool = False,
    ) -> None:
        with self._lock:
            if self._job is None:
                return
            failures = self._job.setdefault("failedAccounts", [])
            failures.append(
                {
                    "id": uuid.uuid4().hex,
                    "time": _now_ms(),
                    "email": email or "unknown",
                    "round": round_index,
                    "worker": worker_index,
                    "stage": stage or "unknown",
                    "reason": reason,
                    "timedOut": bool(timed_out),
                }
            )
            del failures[:-500]

    def _persist_result(self, provider_name: str, result: dict) -> None:
        txt_sink = TxtFileSink(TXT_OUTPUT)
        json_sink = JsonCredentialSink(str(CREDENTIALS_DIR))
        txt_sink.push(provider_name, result)
        json_sink.push(provider_name, result)
        json_sink.flush()

    def _create_provider(self, oauth_exchange: bool, stop_event) -> GrokProvider:
        provider = GrokProvider()
        provider.enable_oauth_exchange = oauth_exchange
        provider.stop_event = stop_event
        provider.mail_source = self._mail_source
        return provider

    def _start_worker_session(self, worker_index: int, chrome_lang: str = "zh-CN") -> DrissionBrowserSession:
        session = DrissionBrowserSession(
            build_chromium_options(chrome_lang, headless=False, window_index=worker_index)
        )
        self._register_session(session)
        session.start()
        return session

    def _run_round_with_timeout(
        self,
        provider: GrokProvider,
        session: DrissionBrowserSession,
        timeout_seconds: int,
    ) -> tuple[str, dict | BaseException | None]:
        result_queue: queue.Queue[tuple[str, dict | BaseException | None]] = queue.Queue(maxsize=1)

        def _target() -> None:
            try:
                result_queue.put(("ok", provider.run_round(session)))
            except BaseException as error:
                result_queue.put(("error", error))

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout=timeout_seconds)
        if thread.is_alive():
            return "timeout", None
        try:
            return result_queue.get_nowait()
        except queue.Empty:
            return "error", RuntimeError("注册线程结束但没有返回结果")

    def _run_worker(self, worker_index: int, oauth_exchange: bool) -> None:
        session: DrissionBrowserSession | None = None
        try:
            if self._stop_event.is_set():
                return
            session = self._start_worker_session(worker_index)
            if self._stop_event.is_set():
                session.stop()
                self._unregister_session(session)
                return
        except Exception as error:
            self._worker_start_failed()
            if self._stop_event.is_set():
                self._event("warning", f"Worker {worker_index} 已停止启动", worker=worker_index)
            else:
                self._event("error", f"Worker {worker_index} 浏览器启动失败：{error}", worker=worker_index)
            return

        try:
            self._event("info", f"Worker {worker_index} 浏览器已启动", worker=worker_index)
            while True:
                if self._stop_event.is_set():
                    self._event("info", f"Worker {worker_index} 收到停止信号，退出", worker=worker_index)
                    break

                round_index = self._next_round()
                if round_index is None:
                    break

                self._event(
                    "info",
                    f"开始第 {round_index} 轮注册",
                    worker=worker_index,
                    round=round_index,
                )
                ok = False
                cancelled = False
                round_stop_event = threading.Event()
                provider = self._create_provider(
                    oauth_exchange,
                    _CombinedStopEvent(self._stop_event, round_stop_event),
                )
                try:
                    if session is None:
                        session = self._start_worker_session(worker_index, provider.chrome_lang)
                        self._event("info", f"Worker {worker_index} 浏览器已重新启动", worker=worker_index)
                    status, payload = self._run_round_with_timeout(
                        provider,
                        session,
                        ROUND_TIMEOUT_SECONDS,
                    )
                    if status == "timeout":
                        round_stop_event.set()
                        email = str(getattr(provider, "current_email", "") or "")
                        stage = str(getattr(provider, "current_stage", "") or "timeout")
                        reason = f"注册超过 {ROUND_TIMEOUT_SECONDS} 秒，已跳过该账号"
                        self._record_failed_account(
                            email=email,
                            round_index=round_index,
                            worker_index=worker_index,
                            stage=stage,
                            reason=reason,
                            timed_out=True,
                        )
                        self._event(
                            "error",
                            f"第 {round_index} 轮超时：{reason}",
                            worker=worker_index,
                            round=round_index,
                            email=email,
                            stage=stage,
                        )
                        try:
                            session.stop()
                        except Exception:
                            pass
                        self._unregister_session(session)
                        session = None
                        continue
                    if status == "error":
                        error = payload if isinstance(payload, BaseException) else RuntimeError(str(payload))
                        raise error
                    result = payload if isinstance(payload, dict) else {}
                    if self._stop_event.is_set():
                        cancelled = True
                        self._event(
                            "warning",
                            f"第 {round_index} 轮在完成后被停止",
                            worker=worker_index,
                            round=round_index,
                        )
                    else:
                        self._persist_result(provider.name, result)
                        ok = True
                        email = str(result.get("email") or "")
                        self._event(
                            "success",
                            f"第 {round_index} 轮注册完成：{email}",
                            worker=worker_index,
                            round=round_index,
                            email=email,
                        )
                except RuntimeError as error:
                    if "stopped" in str(error).lower() or self._stop_event.is_set():
                        cancelled = True
                        self._event(
                            "warning",
                            f"第 {round_index} 轮已被停止",
                            worker=worker_index,
                            round=round_index,
                        )
                    else:
                        email = str(getattr(provider, "current_email", "") or "")
                        stage = str(getattr(provider, "current_stage", "") or "runtime_error")
                        self._record_failed_account(
                            email=email,
                            round_index=round_index,
                            worker_index=worker_index,
                            stage=stage,
                            reason=str(error),
                        )
                        self._event(
                            "error",
                            f"第 {round_index} 轮失败：{error}",
                            worker=worker_index,
                            round=round_index,
                            email=email,
                            stage=stage,
                        )
                        traceback.print_exc()
                except Exception as error:
                    if self._stop_event.is_set():
                        cancelled = True
                        self._event(
                            "warning",
                            f"第 {round_index} 轮已被停止",
                            worker=worker_index,
                            round=round_index,
                        )
                    else:
                        email = str(getattr(provider, "current_email", "") or "")
                        stage = str(getattr(provider, "current_stage", "") or error.__class__.__name__)
                        self._record_failed_account(
                            email=email,
                            round_index=round_index,
                            worker_index=worker_index,
                            stage=stage,
                            reason=str(error),
                        )
                        self._event(
                            "error",
                            f"第 {round_index} 轮失败：{error}",
                            worker=worker_index,
                            round=round_index,
                            email=email,
                            stage=stage,
                        )
                        traceback.print_exc()
                finally:
                    self._round_finished(ok, cancelled=cancelled)
                    if self._stop_event.is_set():
                        break
                    with self._lock:
                        has_more = self._job is not None and self._job["issued"] < self._job["total"]
                    if not has_more:
                        break
                    try:
                        if session is None:
                            continue
                        session.restart()
                    except Exception as error:
                        self._event("warning", f"Worker {worker_index} 浏览器重启失败：{error}", worker=worker_index)
                        try:
                            session.stop()
                        except Exception:
                            pass
                        self._unregister_session(session)
                        session = None
        finally:
            if session is not None:
                try:
                    session.stop()
                except Exception:
                    pass
                self._unregister_session(session)
            self._event("info", f"Worker {worker_index} 已退出", worker=worker_index)

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            if self._job is None or self._job.get("id") != job_id:
                return
            concurrency = int(self._job["concurrency"])
            oauth_exchange = bool(self._job["oauthExchange"])

        workers = [
            threading.Thread(
                target=self._run_worker,
                args=(index + 1, oauth_exchange),
                daemon=True,
            )
            for index in range(concurrency)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        with self._lock:
            if self._job is None or self._job.get("id") != job_id:
                return
            if self._job.get("status") == "stopping":
                self._job["status"] = "stopped"
                self._event_locked("warning", "任务已停止")
            elif self._job.get("failed", 0) > 0 or self._job.get("workerErrors", 0) > 0:
                self._job["status"] = "completed_with_errors"
                self._event_locked("warning", "任务完成，但存在失败轮次")
            else:
                self._job["status"] = "completed"
                self._event_locked("success", "任务已全部完成")
            self._job["finishedAt"] = _now_ms()
            self._mail_source = None


def refresh_account_quota(account_id: str) -> dict:
    """用 access_token 刷新指定账号的额度信息并写回文件。"""
    global _ACCOUNTS_CACHE_SIGNATURE
    from ..grok.client import fetch_complete_credential

    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    for file_path in CREDENTIALS_DIR.glob("*.json"):
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "").strip() != account_id:
                continue
            access_token = str(item.get("access_token") or "").strip()
            if not access_token:
                raise ValueError("该账号没有 access_token，无法刷新额度")
            email = str(item.get("email") or "").strip()
            updated = fetch_complete_credential(
                email=email,
                sso_token=access_token,
                profile=None,
                oauth_tokens=None,
            )
            for keep_key in ("id", "created_at", "refresh_token", "id_token",
                             "expires_at", "expires_at_raw", "auth_raw"):
                if item.get(keep_key) is not None:
                    updated[keep_key] = item[keep_key]
            items[index] = updated
            out = items if isinstance(data, list) else items[0]
            file_path.write_text(
                json.dumps(out, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            with _ACCOUNTS_CACHE_LOCK:
                _ACCOUNTS_CACHE_SIGNATURE = None
            return {"account": _account_from_credential(updated, file_path, index)}
    raise ValueError(f"未找到 accountId={account_id} 的账号")

JOB_MANAGER = RegistrationJobManager()





class WebHandler(BaseHTTPRequestHandler):
    server_version = "GrokAccountManagerWeb/0.1"

    def do_OPTIONS(self) -> None:
        self._send_empty(204)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if self._maybe_proxy_relay(parsed, body=b""):
            return
        try:
            if parsed.path == "/api/accounts":
                self._send_json({"accounts": list_accounts()})
                return
            if parsed.path in {"/api/state", "/api/jobs/current"}:
                self._send_json({
                    "job": JOB_MANAGER.snapshot(),
                    "accounts": list_accounts(),
                    "relay": RELAY_MANAGER.snapshot(),
                })
                return
            if parsed.path == "/api/relay":
                self._send_json({"relay": RELAY_MANAGER.snapshot()})
                return
        except Exception as error:
            self._send_json({"error": str(error)}, status=500)
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/v1/") or parsed.path.startswith("/admin/api/"):
            body = self._read_raw_body()
            if self._maybe_proxy_relay(parsed, body=body):
                return
        try:
            if parsed.path == "/api/register":
                body = _read_json_body(self)
                job = JOB_MANAGER.start(
                    total=body.get("total", 1),
                    concurrency=body.get("concurrency", 1),
                    oauth_exchange=bool(body.get("oauthExchange", True)),
                    email_source=str(body.get("emailSource") or "duckmail"),
                    outlook_data=str(body.get("outlookData") or ""),
                    outlook_accounts_file=str(body.get("outlookAccountsFile") or ""),
                    google_data=str(body.get("googleData") or ""),
                    google_accounts_file=str(body.get("googleAccountsFile") or ""),
                )
                self._send_json({"job": job})
                return
            if parsed.path == "/api/register/stop":
                self._send_json({"job": JOB_MANAGER.stop()})
                return
            if parsed.path == "/api/accounts/export":
                body = _read_json_body(self)
                accounts = export_credentials(body.get("exportKeys") or [])
                self._send_download_json(
                    accounts,
                    filename=f"msdsj-grok-credentials-{time.strftime('%Y%m%d-%H%M%S')}.json",
                )
                return
            if parsed.path == "/api/accounts/export-cpa":
                body = _read_json_body(self)
                accounts = export_credentials(body.get("exportKeys") or [])
                raw, filename, content_type = build_cpa_download(accounts)
                self._send_download(raw, filename=filename, content_type=content_type)
                return
            if parsed.path == "/api/accounts/refresh-quota":
                body = _read_json_body(self)
                account_id = str(body.get("accountId") or "").strip()
                if not account_id:
                    self._send_json({"error": "accountId 必填"}, status=400)
                    return
                result = refresh_account_quota(account_id)
                self._send_json(result)
                return
            if parsed.path == "/api/accounts/test-batch":
                body = _read_json_body(self)
                timeout = _safe_int(body.get("timeout"), default=120, minimum=5, maximum=120)
                result = test_selected_accounts(body.get("exportKeys") or [], timeout=timeout)
                self._send_json(result)
                return
            if parsed.path == "/api/relay/config":
                body = _read_json_body(self)
                self._send_json({"relay": RELAY_MANAGER.update_config(body)})
                return
            if parsed.path == "/api/relay/start":
                self._send_json({"relay": RELAY_MANAGER.start()})
                return
            if parsed.path == "/api/relay/stop":
                self._send_json({"relay": RELAY_MANAGER.stop()})
                return
            if parsed.path == "/api/relay/sync-accounts":
                body = _read_json_body(self)
                export_keys = body.get("exportKeys") or []
                credentials = export_credentials(export_keys) if export_keys else export_all_credentials()
                result = RELAY_MANAGER.sync_accounts(credentials)
                self._send_json({"sync": result, "relay": RELAY_MANAGER.snapshot()})
                return
            if parsed.path == "/api/relay/models":
                body = _read_json_body(self)
                result = RELAY_MANAGER.probe_models(probe_chat=bool(body.get("probeChat", True)))
                self._send_json({"result": result, "relay": RELAY_MANAGER.snapshot()})
                return
            self._send_json({"error": "not found"}, status=404)
        except Exception as error:
            self._send_json({"error": str(error)}, status=400)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        body = self._read_raw_body()
        if self._maybe_proxy_relay(parsed, body=body):
            return
        self._send_json({"error": "not found"}, status=404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        body = self._read_raw_body()
        if self._maybe_proxy_relay(parsed, body=body):
            return
        self._send_json({"error": "not found"}, status=404)

    def _read_raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _maybe_proxy_relay(self, parsed, body: bytes) -> bool:
        if not (parsed.path.startswith("/v1/") or parsed.path.startswith("/admin/api/")):
            return False
        try:
            response = RELAY_MANAGER.proxy_request(
                self.command,
                parsed.path,
                query=parsed.query,
                headers={key: value for key, value in self.headers.items()},
                body=body,
            )
            self._send_proxy_response(response)
        except Exception as error:
            self._send_json({"error": str(error)}, status=502)
        return True

    def _send_proxy_response(self, response) -> None:
        raw = response.content or b""
        self.send_response(response.status_code)
        self._send_cors_headers()
        blocked_headers = {
            "content-length",
            "transfer-encoding",
            "connection",
            "content-encoding",
        }
        for key, value in response.headers.items():
            if key.lower() in blocked_headers:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        self._send_cors_headers()
        self.end_headers()

    def _send_json(self, payload: dict, status: int = 200) -> None:
        try:
            raw = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        except Exception as serialize_error:
            raw = json.dumps({"error": str(serialize_error)}).encode("utf-8")
            status = 500
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_download_json(self, payload, filename: str, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default).encode("utf-8")
        self._send_download(
            raw,
            filename=filename,
            content_type="application/json; charset=utf-8",
            status=status,
        )

    def _send_download(
        self,
        raw: bytes,
        filename: str,
        content_type: str,
        status: int = 200,
    ) -> None:
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Expose-Headers", "Content-Disposition")

    def _serve_static(self, request_path: str) -> None:
        if not WEB_DIST_DIR.exists():
            self._send_json(
                {
                    "error": "web/dist 不存在。开发模式请运行 npm run dev；生产模式先运行 npm run build。"
                },
                status=404,
            )
            return

        relative = request_path.lstrip("/") or "index.html"
        file_path = (WEB_DIST_DIR / relative).resolve()
        if WEB_DIST_DIR.resolve() not in file_path.parents and file_path != WEB_DIST_DIR.resolve():
            self._send_json({"error": "invalid path"}, status=400)
            return
        if not file_path.exists() or file_path.is_dir():
            file_path = WEB_DIST_DIR / "index.html"
        raw = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args) -> None:
        return


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    ensure_stable_python_runtime()
    warn_runtime_compatibility()
    load_dotenv()
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), WebHandler)
    print(f"[*] MSDSJ grok-account-manager API 已启动: http://{host}:{port}")
    print(f"[*] React 开发模式请在 web/ 下运行: npm run dev")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Info] 收到中断信号，停止 Web 服务。")
    finally:
        JOB_MANAGER.stop()
        server.server_close()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
