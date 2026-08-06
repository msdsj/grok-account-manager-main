"""Registration job orchestration for the FastAPI backend."""

from __future__ import annotations

import json
import os
import queue
import random
import threading
import time
import traceback
import uuid

from ...core.browser import DrissionBrowserSession, build_chromium_options
from ...grok.oauth_exchange import CloudflareBlockedError
from ...mail.sources import build_mailbox_source
from ...providers.grok import GrokProvider
from ...sinks.json_credential import JsonCredentialSink
from ...sinks.txt_file import TxtFileSink
from ..config import (
    CREDENTIALS_DIR,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_OAUTH_CONCURRENCY,
    DEFAULT_OAUTH_ACCESS_DENIED_CIRCUIT_THRESHOLD,
    OAUTH_ROUND_TIMEOUT_SECONDS,
    REGISTRATION_ROUND_TIMEOUT_SECONDS,
    ROUND_PACING_MAX_SECONDS,
    ROUND_PACING_MIN_SECONDS,
    TXT_OUTPUT,
    WORKER_START_STAGGER_MAX_SECONDS,
    WORKER_START_STAGGER_MIN_SECONDS,
)
from ..utils import json_default, now_ms, safe_int
from .accounts import invalidate_accounts_cache
from .pending import OAUTH_PENDING, PERSISTENCE_FAILED, PendingResultStore


def _fingerprint_summary(session: DrissionBrowserSession) -> str:
    isolation = str(getattr(session, "isolation_summary", "") or "").strip()
    identity = getattr(session, "identity", None)
    if identity is None:
        return f"（{isolation}）" if isolation else ""
    return (
        f"（{isolation}；指纹 ID {identity.canvas_seed:08x}：{identity.gpu_renderer}，"
        f"{identity.hardware_concurrency} 核，{identity.device_memory}GB 内存）"
    )


def _result_has_refresh_token(result: dict) -> bool:
    credential = result.get("full_credential")
    return isinstance(credential, dict) and bool(
        str(credential.get("refresh_token") or "").strip()
    )


def _is_oauth_access_denied(error: str) -> bool:
    normalized = " ".join(str(error or "").strip().lower().split())
    return (
        "access_denied" in normalized
        or "access denied" in normalized
        or "授权已被拒绝" in normalized
    )


class CombinedStopEvent:
    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)

    def set(self) -> None:
        """Propagate an internal stop request to the whole job and current round."""
        for event in self._events:
            event.set()


class RegistrationJobManager:
    def __init__(self, pending_store: PendingResultStore | None = None) -> None:
        self._lock = threading.RLock()
        self._persist_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._job: dict | None = None
        self._sessions: set[DrissionBrowserSession] = set()
        self._mail_source = None
        self._oauth_semaphore: threading.BoundedSemaphore | None = None
        self._next_job_start_not_before = 0.0
        self._last_config: dict = {}
        self._pending_store = pending_store if pending_store is not None else PendingResultStore()
        self.pending_retry_summary = self.retry_pending_persistence()

    def retry_pending_persistence(self) -> dict[str, int]:
        """Retry only results that previously reached final persistence and failed."""
        summary = {"found": 0, "completed": 0, "failed": 0}
        try:
            records = self._pending_store.list(PERSISTENCE_FAILED)
        except Exception as error:
            print(f"[RegistrationJobManager] 读取待恢复凭证失败: {error}")
            summary["failed"] = 1
            return summary

        summary["found"] = len(records)
        for record in records:
            record_id = str(record.get("id") or "")
            result = record.get("result")
            if not record_id or not isinstance(result, dict):
                summary["failed"] += 1
                continue
            try:
                if bool(record.get("oauth_required")) and (
                    str(result.get("oauth_status") or "") != "ready"
                    or not _result_has_refresh_token(result)
                ):
                    raise ValueError("待恢复 OAuth 结果缺少有效 refresh_token")
                self._persist_result(
                    str(record.get("provider_name") or "grok"),
                    result,
                    write_txt=bool(record.get("write_txt")),
                )
            except Exception as error:
                summary["failed"] += 1
                try:
                    self._pending_store.note_error(record_id, str(error), increment_attempts=True)
                except Exception as store_error:
                    print(f"[RegistrationJobManager] 更新待恢复凭证失败: {store_error}")
            else:
                try:
                    self._pending_store.remove(record_id)
                except Exception as error:
                    summary["failed"] += 1
                    print(f"[RegistrationJobManager] 清理已恢复凭证失败: {error}")
                else:
                    summary["completed"] += 1
        return summary

    def snapshot(self) -> dict | None:
        with self._lock:
            if self._job is None:
                return None
            return json.loads(json.dumps(self._job, default=json_default))

    def retry(self) -> dict:
        """Start a single-round registration using the last saved config."""
        with self._lock:
            if not self._last_config:
                raise RuntimeError("没有可用的上次注册配置，请先运行一次注册任务")
        cfg = self._last_config
        return self.start(
            total=1,
            concurrency=1,
            oauth_exchange=cfg.get("oauth_exchange", True),
            minimize_browsers=cfg.get("minimize_browsers", True),
            email_source=cfg.get("email_source", "duckmail"),
            outlook_data=cfg.get("outlook_data", ""),
            outlook_accounts_file=cfg.get("outlook_accounts_file", ""),
            google_data=cfg.get("google_data", ""),
            google_accounts_file=cfg.get("google_accounts_file", ""),
        )

    def start(
        self,
        total: int,
        concurrency: int,
        oauth_exchange: bool,
        minimize_browsers: bool = True,
        email_source: str = "duckmail",
        outlook_data: str = "",
        outlook_accounts_file: str = "",
        google_data: str = "",
        google_accounts_file: str = "",
    ) -> dict:
        total = safe_int(total, default=1, minimum=1, maximum=10_000)
        requested_total = total
        requested_concurrency = safe_int(concurrency, default=1, minimum=1, maximum=20)
        max_concurrency = safe_int(
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

        # Outlook/Google 邮箱池里的每个账号只能注册一次；如果请求的总数超过池子大小，
        # 循环取号会导致同一个邮箱被拿去"注册"第二次，这个账号大概率直接失败。
        # 这里直接把总数限制到池子大小，而不是静默地绕回去复用同一个邮箱。
        pool_count = int(getattr(mail_source, "count", 0) or 0)
        pool_capped = bool(pool_count) and total > pool_count
        if pool_capped:
            total = pool_count
            concurrency = min(concurrency, total)

        max_oauth_concurrency = safe_int(
            os.environ.get("GROK_ACCOUNT_MANAGER_MAX_OAUTH_CONCURRENCY"),
            default=DEFAULT_MAX_OAUTH_CONCURRENCY,
            minimum=1,
            maximum=20,
        )
        oauth_concurrency = min(concurrency, max_oauth_concurrency) if oauth_exchange else 0
        round_timeout_seconds = (
            OAUTH_ROUND_TIMEOUT_SECONDS if oauth_exchange else REGISTRATION_ROUND_TIMEOUT_SECONDS
        )

        with self._lock:
            if self._job and self._job.get("status") in {"running", "stopping"}:
                raise RuntimeError("已有注册任务正在运行")

            self._last_config = {
                "oauth_exchange": bool(oauth_exchange),
                "minimize_browsers": bool(minimize_browsers),
                "email_source": email_source,
                "outlook_data": outlook_data,
                "outlook_accounts_file": outlook_accounts_file,
                "google_data": google_data,
                "google_accounts_file": google_accounts_file,
            }
            self._stop_event = threading.Event()
            self._mail_source = mail_source
            self._oauth_semaphore = (
                threading.BoundedSemaphore(oauth_concurrency) if oauth_concurrency else None
            )
            self._job = {
                "id": uuid.uuid4().hex,
                "status": "running",
                "total": total,
                "concurrency": concurrency,
                "oauthExchange": bool(oauth_exchange),
                "windowsMinimized": bool(minimize_browsers),
                "emailSource": getattr(mail_source, "name", email_source),
                "outlookAccountCount": getattr(mail_source, "count", 0)
                if getattr(mail_source, "name", "") == "outlook"
                else 0,
                "googleAccountCount": getattr(mail_source, "count", 0)
                if getattr(mail_source, "name", "") in {"gmail", "google"}
                else 0,
                "issued": 0,
                "completed": 0,
                "failed": 0,
                "registered": 0,
                "refreshTokenCompleted": 0,
                "refreshTokenFailed": 0,
                "oauthAccessDeniedStreak": 0,
                "oauthCircuitOpen": False,
                "oauthCircuitReason": "",
                "oauthCircuitThreshold": DEFAULT_OAUTH_ACCESS_DENIED_CIRCUIT_THRESHOLD,
                "workerErrors": 0,
                "active": 0,
                "oauthConcurrency": oauth_concurrency,
                "roundTimeoutSeconds": round_timeout_seconds,
                "failedAccounts": [],
                "registeredAccounts": [],
                "workers": [
                    {
                        "worker": index + 1,
                        "status": "waiting",
                        "round": None,
                        "email": "",
                        "stage": "waiting",
                        "message": "等待启动",
                        "fingerprint": "",
                        "updatedAt": now_ms(),
                    }
                    for index in range(concurrency)
                ],
                "startedAt": now_ms(),
                "finishedAt": None,
                "events": [],
            }
            self._event_locked("info", f"任务启动：总数 {total}，并发 {concurrency}")
            if oauth_exchange:
                self._event_locked(
                    "info",
                    f"refresh_token 阶段最多同时运行 {oauth_concurrency} 个窗口，"
                    "其余窗口会排队以降低同出口 IP 的 OAuth 限速风险",
                )
            if concurrency > 1:
                self._event_locked(
                    "warning",
                    "所有窗口仍共享本机网络出口；独立 profile 和指纹只能降低浏览器关联，"
                    "不能消除同一出口 IP 带来的平台风控",
                )
            if concurrency < requested_concurrency:
                self._event_locked(
                    "warning",
                    f"为降低本机压力，并发已从 {requested_concurrency} 限制为 {concurrency}",
                )
            if pool_capped:
                self._event_locked(
                    "warning",
                    f"邮箱池只有 {pool_count} 个可用账号，总数已从 {requested_total} 限制为 {pool_count}，"
                    f"避免同一个邮箱被重复用来注册导致必然失败",
                )

            self._thread = threading.Thread(
                target=self._run_job,
                args=(self._job["id"],),
                daemon=True,
            )
            self._thread.start()
            return self.snapshot() or {}

    def stop(self, *, wait: bool = False, timeout: float = 10.0) -> dict | None:
        with self._lock:
            sessions_to_close: list[DrissionBrowserSession] = []
            if self._job and self._job.get("status") in {"running", "stopping"}:
                self._job["status"] = "stopping"
                self._event_locked("warning", "已请求停止，正在关闭所有浏览器进程")
                self._stop_event.set()
                sessions_to_close = list(self._sessions)
            snapshot = self.snapshot()
            job_thread = self._thread
        if sessions_to_close:
            for session in sessions_to_close:
                try:
                    session.request_stop()
                except Exception:
                    pass
            if wait:
                self._close_sessions(sessions_to_close)
            else:
                self._close_sessions_async(sessions_to_close)
        if (
            wait
            and job_thread is not None
            and job_thread is not threading.current_thread()
            and job_thread.is_alive()
        ):
            job_thread.join(timeout=max(0.0, timeout))
            with self._lock:
                remaining_sessions = list(self._sessions)
            if remaining_sessions:
                self._close_sessions(remaining_sessions)
        return self.snapshot() if wait else snapshot

    def _close_sessions_async(self, sessions: list[DrissionBrowserSession]) -> None:
        for session in sessions:
            threading.Thread(target=session.stop, daemon=True).start()

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

    def set_windows_minimized(self, minimized: bool) -> dict:
        with self._lock:
            if self._job is None or self._job.get("status") not in {"running", "stopping"}:
                raise RuntimeError("当前没有运行中的注册任务")
            sessions = list(self._sessions)
            self._job["windowsMinimized"] = bool(minimized)

        changed = 0
        pending = 0
        errors: list[str] = []
        for session in sessions:
            try:
                if session.set_window_minimized(minimized):
                    changed += 1
                else:
                    pending += 1
            except Exception as error:
                errors.append(str(error))

        action = "最小化" if minimized else "恢复"
        level = "warning" if errors else "info"
        self._event(
            level,
            f"已请求{action}全部浏览器：立即生效 {changed}，启动中 {pending}，失败 {len(errors)}",
        )
        return {
            "job": self.snapshot(),
            "changed": changed,
            "pending": pending,
            "errors": errors,
        }

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
                "time": now_ms(),
                "level": level,
                "message": message,
                **extra,
            }
        )
        del events[:-240]

    def _event(self, level: str, message: str, **extra) -> None:
        with self._lock:
            self._event_locked(level, message, **extra)

    def _update_worker_state_locked(self, worker_index: int, **changes) -> None:
        if self._job is None:
            return
        workers = self._job.setdefault("workers", [])
        worker_state = next(
            (item for item in workers if int(item.get("worker") or 0) == worker_index),
            None,
        )
        if worker_state is None:
            worker_state = {"worker": worker_index}
            workers.append(worker_state)
        worker_state.update(changes)
        worker_state["updatedAt"] = now_ms()

    def _update_worker_state(self, worker_index: int, **changes) -> None:
        with self._lock:
            self._update_worker_state_locked(worker_index, **changes)

    def _provider_event_callback(self, worker_index: int, round_index: int):
        with self._lock:
            expected_job_id = str((self._job or {}).get("id") or "")

        def _callback(level: str, message: str, **extra) -> None:
            merged = {"worker": worker_index, "round": round_index, **extra}
            with self._lock:
                if str((self._job or {}).get("id") or "") != expected_job_id:
                    return
                worker_changes = {
                    "status": "running",
                    "round": round_index,
                    "stage": str(extra.get("stage") or "running"),
                    "message": message,
                }
                if extra.get("email"):
                    worker_changes["email"] = str(extra["email"])
                self._update_worker_state_locked(worker_index, **worker_changes)
                self._event_locked(level, message, **merged)

        return _callback

    @staticmethod
    def _pending_result_id(job_id: str, round_index: int) -> str:
        return f"{job_id}:{round_index}"

    def _record_pending_result(
        self,
        record_id: str,
        *,
        status: str,
        provider_name: str,
        result: dict,
        write_txt: bool,
        oauth_required: bool,
        job_id: str = "",
        worker_index: int = 0,
        round_index: int = 0,
        error: str = "",
    ) -> None:
        self._pending_store.upsert(
            record_id,
            status=status,
            provider_name=provider_name,
            result=result,
            write_txt=write_txt,
            oauth_required=oauth_required,
            job_id=job_id,
            worker_index=worker_index,
            round_index=round_index,
            error=error,
        )

    def _registration_checkpoint_callback(self, worker_index: int, round_index: int):
        with self._lock:
            expected_job_id = str((self._job or {}).get("id") or "")
            oauth_required = bool((self._job or {}).get("oauthExchange"))
        pending_id = self._pending_result_id(expected_job_id, round_index)

        def _callback(result: dict) -> None:
            email = str(result.get("email") or "")
            with self._lock:
                if (
                    str((self._job or {}).get("id") or "") != expected_job_id
                    or (self._job or {}).get("status") not in {"running", "stopping"}
                ):
                    return
                if oauth_required:
                    self._record_pending_result(
                        pending_id,
                        status=OAUTH_PENDING,
                        provider_name="grok",
                        result=result,
                        write_txt=True,
                        oauth_required=True,
                        job_id=expected_job_id,
                        worker_index=worker_index,
                        round_index=round_index,
                    )
                else:
                    self._record_pending_result(
                        pending_id,
                        status=PERSISTENCE_FAILED,
                        provider_name="grok",
                        result=result,
                        write_txt=True,
                        oauth_required=False,
                        job_id=expected_job_id,
                        worker_index=worker_index,
                        round_index=round_index,
                        error="等待正式持久化",
                    )
                    try:
                        self._persist_result("grok", result)
                    except Exception as error:
                        self._record_pending_result(
                            pending_id,
                            status=PERSISTENCE_FAILED,
                            provider_name="grok",
                            result=result,
                            write_txt=True,
                            oauth_required=False,
                            job_id=expected_job_id,
                            worker_index=worker_index,
                            round_index=round_index,
                            error=str(error),
                        )
                        raise
                    else:
                        try:
                            self._pending_store.remove(pending_id)
                        except Exception as error:
                            print(f"[RegistrationJobManager] 清理已保存 checkpoint 失败: {error}")
                registered_accounts = self._job.setdefault("registeredAccounts", [])
                item = next(
                    (
                        account
                        for account in registered_accounts
                        if int(account.get("round") or 0) == round_index
                    ),
                    None,
                )
                if item is None:
                    item = {
                        "id": uuid.uuid4().hex,
                        "round": round_index,
                        "worker": worker_index,
                        "email": email,
                        "registeredAt": now_ms(),
                        "oauthFinalized": False,
                    }
                    registered_accounts.append(item)
                    self._job["registered"] = int(self._job.get("registered") or 0) + 1
                item.update(
                    {
                        "email": email,
                        "oauthStatus": str(result.get("oauth_status") or "pending"),
                        "oauthError": "",
                    }
                )
                self._update_worker_state_locked(
                    worker_index,
                    status="registered",
                    round=round_index,
                    email=email,
                    stage="oauth_queue" if self._job.get("oauthExchange") else "registered",
                    message=(
                        "账号已注册，等待 refresh_token 后再保存"
                        if oauth_required
                        else "账号已注册，基础凭证已保存"
                    ),
                )
                self._event_locked(
                    "success",
                    (
                        f"第 {round_index} 轮账号已注册，等待 refresh_token：{email}"
                        if oauth_required
                        else f"第 {round_index} 轮账号已注册并保存：{email}"
                    ),
                    worker=worker_index,
                    round=round_index,
                    email=email,
                    stage="registered",
                )

        return _callback

    def _finalize_registered_account(
        self,
        *,
        worker_index: int,
        round_index: int,
        oauth_status: str,
        oauth_error: str = "",
    ) -> None:
        with self._lock:
            if self._job is None:
                return
            registered_accounts = self._job.setdefault("registeredAccounts", [])
            item = next(
                (
                    account
                    for account in registered_accounts
                    if int(account.get("round") or 0) == round_index
                ),
                None,
            )
            if item is None:
                return
            newly_finalized = not item.get("oauthFinalized")
            if newly_finalized:
                if oauth_status == "ready":
                    self._job["refreshTokenCompleted"] = (
                        int(self._job.get("refreshTokenCompleted") or 0) + 1
                    )
                    self._job["oauthAccessDeniedStreak"] = 0
                elif oauth_status == "failed":
                    self._job["refreshTokenFailed"] = (
                        int(self._job.get("refreshTokenFailed") or 0) + 1
                    )
                    if _is_oauth_access_denied(oauth_error):
                        streak = int(self._job.get("oauthAccessDeniedStreak") or 0) + 1
                        self._job["oauthAccessDeniedStreak"] = streak
                        threshold = int(
                            self._job.get("oauthCircuitThreshold")
                            or DEFAULT_OAUTH_ACCESS_DENIED_CIRCUIT_THRESHOLD
                        )
                        if streak >= threshold and not self._job.get("oauthCircuitOpen"):
                            reason = (
                                f"连续 {streak} 个账号被 xAI OAuth 服务端拒绝授权，"
                                "已停止剩余任务，避免继续消耗邮箱"
                            )
                            self._job["oauthCircuitOpen"] = True
                            self._job["oauthCircuitReason"] = reason
                            if self._job.get("status") == "running":
                                self._job["status"] = "stopping"
                            self._stop_event.set()
                            self._event_locked("warning", reason, stage="oauth_circuit_open")
                    else:
                        self._job["oauthAccessDeniedStreak"] = 0
            item.update(
                {
                    "oauthStatus": oauth_status,
                    "oauthError": oauth_error,
                    "oauthFinalized": oauth_status in {"ready", "failed", "not_requested"},
                    "updatedAt": now_ms(),
                }
            )
            worker_status = "success" if oauth_status in {"ready", "not_requested"} else "warning"
            worker_message = (
                "账号与 refresh_token 已保存"
                if oauth_status == "ready"
                else (
                    f"refresh_token 获取失败，凭证未保存：{oauth_error}"
                    if oauth_error
                    else "refresh_token 获取失败，凭证未保存"
                )
                if oauth_status == "failed"
                else "账号已保存"
            )
            self._update_worker_state_locked(
                worker_index,
                status=worker_status,
                stage="done",
                message=worker_message,
            )

    def _stagger_worker_start(self, worker_index: int) -> None:
        with self._lock:
            cross_job_delay = max(0.0, self._next_job_start_not_before - time.monotonic())
        multiplier = max(0, worker_index - 1)
        worker_delay = (
            random.uniform(
                WORKER_START_STAGGER_MIN_SECONDS * multiplier,
                WORKER_START_STAGGER_MAX_SECONDS * multiplier,
            )
            if multiplier
            else 0.0
        )
        delay = cross_job_delay + worker_delay
        if delay <= 0:
            return
        self._update_worker_state(
            worker_index,
            status="waiting",
            stage="startup_stagger",
            message=f"错峰等待 {delay:.1f}s",
        )
        self._event(
            "info",
            f"Worker {worker_index} 错峰等待 {delay:.1f}s 后启动，避免并发请求形成固定突发",
            worker=worker_index,
        )
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

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

    def _pace_before_next_round(self, worker_index: int) -> None:
        """一轮结束、下一轮开始前随机停顿一下，避免同一来源密集、匀速地发起注册请求。

        节奏本身也是一种可被风控识别的特征——真人不会以固定间隔连续注册账号。
        """
        delay = random.uniform(ROUND_PACING_MIN_SECONDS, ROUND_PACING_MAX_SECONDS)
        self._event(
            "info",
            f"Worker {worker_index} 等待 {delay:.1f}s 后开始下一轮（模拟真人节奏，降低风控概率）",
            worker=worker_index,
        )
        deadline = time.monotonic() + delay
        while True:
            if self._stop_event.is_set():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.2, remaining))

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
                    "time": now_ms(),
                    "email": email or "unknown",
                    "round": round_index,
                    "worker": worker_index,
                    "stage": stage or "unknown",
                    "reason": reason,
                    "timedOut": bool(timed_out),
                }
            )
            del failures[:-500]

    def _persist_result(self, provider_name: str, result: dict, *, write_txt: bool = True) -> None:
        with self._persist_lock:
            json_sink = JsonCredentialSink(str(CREDENTIALS_DIR))
            json_sink.push(provider_name, result)
            json_sink.flush()
            if write_txt:
                TxtFileSink(TXT_OUTPUT).push(provider_name, result)
            invalidate_accounts_cache()

    def _persist_completed_result(
        self,
        provider_name: str,
        result: dict,
        *,
        oauth_required: bool,
        pending_id: str | None = None,
    ) -> tuple[str, str, bool]:
        oauth_status = str(result.get("oauth_status") or "not_requested")
        oauth_error = str(result.get("oauth_error") or "")
        if oauth_required and (
            oauth_status != "ready" or not _result_has_refresh_token(result)
        ):
            if pending_id:
                self._pending_store.note_error(
                    pending_id,
                    oauth_error or "未获取到 refresh_token，凭证未保存",
                )
            return (
                "failed",
                oauth_error or "未获取到 refresh_token，凭证未保存",
                False,
            )

        write_txt = bool(oauth_required)
        if pending_id:
            self._record_pending_result(
                pending_id,
                status=PERSISTENCE_FAILED,
                provider_name=provider_name,
                result=result,
                write_txt=write_txt,
                oauth_required=oauth_required,
                error="等待正式持久化",
            )
        try:
            self._persist_result(
                provider_name,
                result,
                write_txt=write_txt,
            )
        except Exception as error:
            if pending_id:
                self._record_pending_result(
                    pending_id,
                    status=PERSISTENCE_FAILED,
                    provider_name=provider_name,
                    result=result,
                    write_txt=write_txt,
                    oauth_required=oauth_required,
                    error=str(error),
                )
            raise
        else:
            if pending_id:
                try:
                    self._pending_store.remove(pending_id)
                except Exception as error:
                    print(f"[RegistrationJobManager] 清理已保存凭证失败: {error}")
        return oauth_status, oauth_error, True

    def _create_provider(
        self,
        oauth_exchange: bool,
        stop_event,
        worker_index: int,
        round_index: int,
    ) -> GrokProvider:
        provider = GrokProvider()
        provider.enable_oauth_exchange = oauth_exchange
        provider.stop_event = stop_event
        provider.mail_source = self._mail_source
        provider.oauth_semaphore = self._oauth_semaphore
        provider.result_callback = self._registration_checkpoint_callback(worker_index, round_index)
        return provider

    def _start_worker_session(self, worker_index: int, chrome_lang: str = "zh-CN") -> DrissionBrowserSession:
        with self._lock:
            window_count = int((self._job or {}).get("concurrency") or 1)
            minimize_browser = bool((self._job or {}).get("windowsMinimized", True))
        session = DrissionBrowserSession(
            build_chromium_options(
                chrome_lang,
                headless=False,
                window_index=worker_index - 1,
                window_count=window_count,
                start_minimized=minimize_browser,
            )
        )
        self._register_session(session)
        try:
            with self._lock:
                minimize_browser = bool((self._job or {}).get("windowsMinimized", True))
            session.set_window_minimized(minimize_browser, apply_now=False)
            session.start()
        except Exception:
            session.stop()
            self._unregister_session(session)
            raise
        return session

    def _run_round_with_timeout(
        self,
        provider: GrokProvider,
        session: DrissionBrowserSession,
        timeout_seconds: int,
        worker_index: int,
        round_index: int,
        round_stop_event: threading.Event,
    ) -> tuple[str, dict | BaseException | None]:
        result_queue: queue.Queue[tuple[str, dict | BaseException | None]] = queue.Queue(maxsize=1)

        def _target() -> None:
            try:
                result_queue.put(("ok", provider.run_round(session)))
            except BaseException as error:
                result_queue.put(("error", error))

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        deadline = time.monotonic() + timeout_seconds
        last_stage = ""
        last_heartbeat = 0.0
        while thread.is_alive():
            if self._stop_event.is_set():
                round_stop_event.set()
                # A browser/CDP call can remain blocked after its process is killed.
                # Do not keep the job in "stopping" until the full round timeout.
                return "stopped", None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=min(0.5, remaining))
            stage = str(getattr(provider, "current_stage", "") or "")
            email = str(getattr(provider, "current_email", "") or "")
            now = time.monotonic()
            if stage and stage != last_stage:
                self._event(
                    "info",
                    f"第 {round_index} 轮当前阶段：{stage}",
                    worker=worker_index,
                    round=round_index,
                    email=email,
                    stage=stage,
                )
                last_stage = stage
                last_heartbeat = now
            elif stage and now - last_heartbeat >= 15:
                self._event(
                    "info",
                    f"第 {round_index} 轮仍在 {stage}，剩余约 {max(0, int(deadline - now))} 秒",
                    worker=worker_index,
                    round=round_index,
                    email=email,
                    stage=stage,
                )
                last_heartbeat = now
        if thread.is_alive():
            round_stop_event.set()
            thread.join(timeout=3)
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
            self._stagger_worker_start(worker_index)
            if self._stop_event.is_set():
                return
            self._update_worker_state(
                worker_index,
                status="starting",
                stage="browser_start",
                message="正在启动独立浏览器",
            )
            session = self._start_worker_session(worker_index)
            if self._stop_event.is_set():
                session.stop()
                self._unregister_session(session)
                return
        except Exception as error:
            self._worker_start_failed()
            self._update_worker_state(
                worker_index,
                status="error",
                stage="browser_start",
                message=str(error),
            )
            if self._stop_event.is_set():
                self._event("warning", f"Worker {worker_index} 已停止启动", worker=worker_index)
            else:
                self._event("error", f"Worker {worker_index} 浏览器启动失败：{error}", worker=worker_index)
            return

        try:
            fingerprint_summary = _fingerprint_summary(session)
            self._update_worker_state(
                worker_index,
                status="ready",
                stage="browser_ready",
                message="独立浏览器已启动",
                fingerprint=fingerprint_summary.strip("（）"),
            )
            self._event(
                "info",
                f"Worker {worker_index} 浏览器已启动{fingerprint_summary}",
                worker=worker_index,
            )
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
                self._update_worker_state(
                    worker_index,
                    status="running",
                    round=round_index,
                    email="",
                    stage="starting",
                    message=f"正在执行第 {round_index} 轮",
                )
                ok = False
                cancelled = False
                round_stop_event = threading.Event()
                with self._lock:
                    round_job_id = str((self._job or {}).get("id") or "")
                pending_id = self._pending_result_id(round_job_id, round_index)
                provider = self._create_provider(
                    oauth_exchange,
                    CombinedStopEvent(self._stop_event, round_stop_event),
                    worker_index,
                    round_index,
                )
                provider.event_callback = self._provider_event_callback(worker_index, round_index)
                try:
                    if session is None:
                        session = self._start_worker_session(worker_index, provider.chrome_lang)
                        self._event(
                            "info",
                            f"Worker {worker_index} 浏览器已重新启动{_fingerprint_summary(session)}",
                            worker=worker_index,
                        )
                    with self._lock:
                        round_timeout_seconds = int(
                            (self._job or {}).get(
                                "roundTimeoutSeconds",
                                OAUTH_ROUND_TIMEOUT_SECONDS
                                if oauth_exchange
                                else REGISTRATION_ROUND_TIMEOUT_SECONDS,
                            )
                        )
                    status, payload = self._run_round_with_timeout(
                        provider,
                        session,
                        round_timeout_seconds,
                        worker_index,
                        round_index,
                        round_stop_event,
                    )
                    if status == "stopped":
                        cancelled = True
                        self._event(
                            "warning",
                            f"第 {round_index} 轮已被停止",
                            worker=worker_index,
                            round=round_index,
                            email=str(getattr(provider, "current_email", "") or ""),
                            stage=str(getattr(provider, "current_stage", "") or "stopped"),
                        )
                        break
                    if status == "timeout":
                        round_stop_event.set()
                        email = str(getattr(provider, "current_email", "") or "")
                        stage = str(getattr(provider, "current_stage", "") or "timeout")
                        registered = bool(getattr(provider, "registration_succeeded", False))
                        if registered:
                            ok = True
                            reason = (
                                f"账号已注册，但后续阶段超过 {round_timeout_seconds} 秒；"
                                "未获取 refresh_token，SSO checkpoint 已保留"
                                if oauth_exchange
                                else (
                                    f"账号已注册，但后续阶段超过 {round_timeout_seconds} 秒；"
                                    "结果已保存或进入恢复队列"
                                )
                            )
                            self._finalize_registered_account(
                                worker_index=worker_index,
                                round_index=round_index,
                                oauth_status="failed" if oauth_exchange else "not_requested",
                                oauth_error=reason if oauth_exchange else "",
                            )
                        else:
                            reason = f"注册超过 {round_timeout_seconds} 秒，已跳过该账号"
                        self._record_failed_account(
                            email=email,
                            round_index=round_index,
                            worker_index=worker_index,
                            stage=stage,
                            reason=reason,
                            timed_out=True,
                        )
                        self._event(
                            "warning" if registered else "error",
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
                        registered = bool(getattr(provider, "registration_succeeded", False))
                        cancelled = not registered
                        ok = registered
                        if registered:
                            (
                                result_oauth_status,
                                result_oauth_error,
                                result_saved,
                            ) = self._persist_completed_result(
                                provider.name,
                                result,
                                oauth_required=oauth_exchange,
                                pending_id=pending_id,
                            )
                            self._finalize_registered_account(
                                worker_index=worker_index,
                                round_index=round_index,
                                oauth_status=result_oauth_status,
                                oauth_error=result_oauth_error,
                            )
                        self._event(
                            "warning",
                            (
                                f"第 {round_index} 轮账号{'已保存' if result_saved else '未保存'}，后续处理被停止"
                                if registered
                                else f"第 {round_index} 轮在完成前被停止"
                            ),
                            worker=worker_index,
                            round=round_index,
                        )
                    else:
                        oauth_status, oauth_error, result_saved = self._persist_completed_result(
                            provider.name,
                            result,
                            oauth_required=oauth_exchange,
                            pending_id=pending_id,
                        )
                        self._finalize_registered_account(
                            worker_index=worker_index,
                            round_index=round_index,
                            oauth_status=oauth_status,
                            oauth_error=oauth_error,
                        )
                        ok = True
                        email = str(result.get("email") or "")
                        self._event(
                            "warning" if oauth_status == "failed" else "success",
                            (
                                f"第 {round_index} 轮 refresh_token 获取失败，凭证未保存：{email}"
                                if oauth_status == "failed"
                                else f"第 {round_index} 轮注册完成：{email}"
                            ),
                            worker=worker_index,
                            round=round_index,
                            email=email,
                        )
                except RuntimeError as error:
                    registered = bool(getattr(provider, "registration_succeeded", False))
                    email = str(getattr(provider, "current_email", "") or "")
                    stage = str(getattr(provider, "current_stage", "") or "runtime_error")
                    if isinstance(error, CloudflareBlockedError):
                        self._event(
                            "error",
                            f"检测到 Cloudflare 封禁，已自动取消任务：{error}",
                            worker=worker_index,
                            round=round_index,
                            email=email,
                            stage=stage,
                        )
                        # Closing all sessions here prevents another worker from
                        # waiting on a browser page after the shared stop event is set.
                        self.stop(wait=False)
                    if "stopped" in str(error).lower() or self._stop_event.is_set():
                        cancelled = not registered
                        ok = registered
                        if registered:
                            self._finalize_registered_account(
                                worker_index=worker_index,
                                round_index=round_index,
                                oauth_status="failed" if oauth_exchange else "not_requested",
                                oauth_error="任务停止时 refresh_token 尚未完成" if oauth_exchange else "",
                            )
                        self._event(
                            "warning",
                            (
                                (
                                    f"第 {round_index} 轮账号已注册但未取得 RT，凭证未保存，后续处理已停止"
                                    if oauth_exchange
                                    else f"第 {round_index} 轮账号已注册，结果已保存或进入恢复队列"
                                )
                                if registered
                                else f"第 {round_index} 轮已被停止"
                            ),
                            worker=worker_index,
                            round=round_index,
                        )
                    else:
                        if registered:
                            ok = True
                            self._finalize_registered_account(
                                worker_index=worker_index,
                                round_index=round_index,
                                oauth_status="failed" if oauth_exchange else "not_requested",
                                oauth_error=str(error) if oauth_exchange else "",
                            )
                        self._record_failed_account(
                            email=email,
                            round_index=round_index,
                            worker_index=worker_index,
                            stage=stage,
                            reason=str(error),
                        )
                        self._event(
                            "warning" if registered else "error",
                            (
                                (
                                    f"第 {round_index} 轮账号已注册但未取得 RT，凭证未保存：{error}"
                                    if oauth_exchange
                                    else f"第 {round_index} 轮账号已注册，正式落盘失败并已进入恢复队列：{error}"
                                )
                                if registered
                                else f"第 {round_index} 轮失败：{error}"
                            ),
                            worker=worker_index,
                            round=round_index,
                            email=email,
                            stage=stage,
                        )
                        traceback.print_exc()
                except Exception as error:
                    registered = bool(getattr(provider, "registration_succeeded", False))
                    email = str(getattr(provider, "current_email", "") or "")
                    stage = str(getattr(provider, "current_stage", "") or error.__class__.__name__)
                    if self._stop_event.is_set():
                        cancelled = not registered
                        ok = registered
                        if registered:
                            self._finalize_registered_account(
                                worker_index=worker_index,
                                round_index=round_index,
                                oauth_status="failed" if oauth_exchange else "not_requested",
                                oauth_error="任务停止时 refresh_token 尚未完成" if oauth_exchange else "",
                            )
                        self._event(
                            "warning",
                            (
                                (
                                    f"第 {round_index} 轮账号已注册但未取得 RT，凭证未保存，后续处理已停止"
                                    if oauth_exchange
                                    else f"第 {round_index} 轮账号已注册，结果已保存或进入恢复队列"
                                )
                                if registered
                                else f"第 {round_index} 轮已被停止"
                            ),
                            worker=worker_index,
                            round=round_index,
                        )
                    else:
                        if registered:
                            ok = True
                            self._finalize_registered_account(
                                worker_index=worker_index,
                                round_index=round_index,
                                oauth_status="failed" if oauth_exchange else "not_requested",
                                oauth_error=str(error) if oauth_exchange else "",
                            )
                        self._record_failed_account(
                            email=email,
                            round_index=round_index,
                            worker_index=worker_index,
                            stage=stage,
                            reason=str(error),
                        )
                        self._event(
                            "warning" if registered else "error",
                            (
                                (
                                    f"第 {round_index} 轮账号已注册但未取得 RT，凭证未保存：{error}"
                                    if oauth_exchange
                                    else f"第 {round_index} 轮账号已注册，正式落盘失败并已进入恢复队列：{error}"
                                )
                                if registered
                                else f"第 {round_index} 轮失败：{error}"
                            ),
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
                        fingerprint_summary = _fingerprint_summary(session)
                        self._update_worker_state(
                            worker_index,
                            status="ready",
                            stage="browser_restarted",
                            message="下一轮独立浏览器已就绪",
                            fingerprint=fingerprint_summary.strip("（）"),
                        )
                        self._event(
                            "info",
                            f"Worker {worker_index} 已为下一轮重新生成指纹{fingerprint_summary}",
                            worker=worker_index,
                        )
                        self._pace_before_next_round(worker_index)
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
            with self._lock:
                worker_state = next(
                    (
                        item
                        for item in (self._job or {}).get("workers", [])
                        if int(item.get("worker") or 0) == worker_index
                    ),
                    None,
                )
                if worker_state is None or worker_state.get("status") not in {
                    "success",
                    "warning",
                    "error",
                }:
                    self._update_worker_state_locked(
                        worker_index,
                        status="stopped" if self._stop_event.is_set() else "idle",
                        stage="exited",
                        message="Worker 已退出",
                    )
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
            elif (
                self._job.get("failed", 0) > 0
                or self._job.get("workerErrors", 0) > 0
                or self._job.get("refreshTokenFailed", 0) > 0
                or bool(self._job.get("failedAccounts"))
            ):
                self._job["status"] = "completed_with_errors"
                self._event_locked("warning", "任务完成，但存在注册、refresh_token 或落盘失败")
            else:
                self._job["status"] = "completed"
                self._event_locked("success", "任务已全部完成")
            self._job["finishedAt"] = now_ms()
            registered_this_round = int(self._job.get("registered") or 0)
            self._next_job_start_not_before = time.monotonic() + random.uniform(
                ROUND_PACING_MIN_SECONDS,
                ROUND_PACING_MAX_SECONDS,
            )
            self._mail_source = None
            self._oauth_semaphore = None

        if registered_this_round > 0:
            self._sync_to_relay_async()

    def _sync_to_relay_async(self) -> None:
        """Best-effort push of newly registered accounts into the grok2api engine.

        Runs off the job thread so a slow/unavailable relay never blocks or fails
        a registration round; failures are logged and swallowed.
        """

        def _sync() -> None:
            from .accounts import _sync_project_pool_to_relay

            for attempt in range(1, 4):
                try:
                    result = _sync_project_pool_to_relay()
                    print(
                        f"[RegistrationJobManager] 已将注册账号同步到新版 grok2api："
                        f"请求 {result.get('requested', 0)} 个"
                    )
                    return
                except Exception as error:
                    if attempt < 3:
                        time.sleep(attempt * 2)
                        continue
                    print(f"[RegistrationJobManager] 自动同步账号到新版 grok2api 失败: {error}")

        threading.Thread(target=_sync, name="relay-auto-sync", daemon=True).start()


JOB_MANAGER = RegistrationJobManager()
