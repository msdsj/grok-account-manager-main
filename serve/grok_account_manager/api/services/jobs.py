"""Registration job orchestration for the FastAPI backend."""

from __future__ import annotations

import json
import os
import queue
import threading
import traceback
import uuid

from ...core.browser import DrissionBrowserSession, build_chromium_options
from ...mail.sources import build_mailbox_source
from ...providers.grok import GrokProvider
from ...sinks.json_credential import JsonCredentialSink
from ...sinks.txt_file import TxtFileSink
from ..config import (
    CREDENTIALS_DIR,
    DEFAULT_MAX_CONCURRENCY,
    ROUND_TIMEOUT_SECONDS,
    TXT_OUTPUT,
)
from ..utils import json_default, now_ms, safe_int
from .accounts import invalidate_accounts_cache


class CombinedStopEvent:
    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


class RegistrationJobManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._persist_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._job: dict | None = None
        self._sessions: set[DrissionBrowserSession] = set()
        self._mail_source = None

    def snapshot(self) -> dict | None:
        with self._lock:
            if self._job is None:
                return None
            return json.loads(json.dumps(self._job, default=json_default))

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
        total = safe_int(total, default=1, minimum=1, maximum=10_000)
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
                "outlookAccountCount": getattr(mail_source, "count", 0)
                if getattr(mail_source, "name", "") == "outlook"
                else 0,
                "googleAccountCount": getattr(mail_source, "count", 0)
                if getattr(mail_source, "name", "") in {"gmail", "google"}
                else 0,
                "issued": 0,
                "completed": 0,
                "failed": 0,
                "workerErrors": 0,
                "active": 0,
                "roundTimeoutSeconds": ROUND_TIMEOUT_SECONDS,
                "failedAccounts": [],
                "startedAt": now_ms(),
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

    def _persist_result(self, provider_name: str, result: dict) -> None:
        with self._persist_lock:
            txt_sink = TxtFileSink(TXT_OUTPUT)
            json_sink = JsonCredentialSink(str(CREDENTIALS_DIR))
            txt_sink.push(provider_name, result)
            json_sink.push(provider_name, result)
            json_sink.flush()
            invalidate_accounts_cache()

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
                    CombinedStopEvent(self._stop_event, round_stop_event),
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
            self._job["finishedAt"] = now_ms()
            self._mail_source = None


JOB_MANAGER = RegistrationJobManager()

