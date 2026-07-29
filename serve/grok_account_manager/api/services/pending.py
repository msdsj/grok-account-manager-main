"""Durable storage for registration results awaiting final persistence."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..config import OUTPUT_DIR


OAUTH_PENDING = "oauth_pending"
PERSISTENCE_FAILED = "persistence_failed"
PENDING_RESULTS_PATH = OUTPUT_DIR / "pending-registration-results.json"
_VALID_STATUSES = {OAUTH_PENDING, PERSISTENCE_FAILED}


class PendingResultStore:
    """A small atomic JSON store containing sensitive in-flight results."""

    def __init__(self, path: str | os.PathLike[str] = PENDING_RESULTS_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        if self.path.exists():
            self.path.chmod(0o600)

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            records = self._load_locked()
            if status is not None:
                records = [record for record in records if record.get("status") == status]
            return deepcopy(records)

    def upsert(
        self,
        record_id: str,
        *,
        status: str,
        provider_name: str,
        result: dict[str, Any],
        write_txt: bool,
        oauth_required: bool,
        job_id: str = "",
        worker_index: int = 0,
        round_index: int = 0,
        error: str = "",
    ) -> dict[str, Any]:
        if status not in _VALID_STATUSES:
            raise ValueError(f"unsupported pending result status: {status}")
        clean_id = str(record_id or "").strip()
        if not clean_id:
            raise ValueError("pending result id is required")
        if not isinstance(result, dict):
            raise TypeError("pending result must be a dict")

        with self._lock:
            records = self._load_locked()
            existing = next((record for record in records if record.get("id") == clean_id), None)
            timestamp = int(time.time() * 1000)
            if existing is None:
                existing = {
                    "id": clean_id,
                    "created_at": timestamp,
                    "attempts": 0,
                }
                records.append(existing)
            existing.update(
                {
                    "status": status,
                    "provider_name": str(provider_name or "grok"),
                    "result": deepcopy(result),
                    "write_txt": bool(write_txt),
                    "oauth_required": bool(oauth_required),
                    "job_id": str(job_id or existing.get("job_id") or ""),
                    "worker_index": int(worker_index or existing.get("worker_index") or 0),
                    "round_index": int(round_index or existing.get("round_index") or 0),
                    "error": str(error or ""),
                    "updated_at": timestamp,
                }
            )
            self._write_locked(records)
            return deepcopy(existing)

    def note_error(self, record_id: str, error: str, *, increment_attempts: bool = False) -> bool:
        with self._lock:
            records = self._load_locked()
            record = next((item for item in records if item.get("id") == record_id), None)
            if record is None:
                return False
            record["error"] = str(error or "")
            record["updated_at"] = int(time.time() * 1000)
            if increment_attempts:
                record["attempts"] = int(record.get("attempts") or 0) + 1
            self._write_locked(records)
            return True

    def remove(self, record_id: str) -> bool:
        with self._lock:
            records = self._load_locked()
            remaining = [record for record in records if record.get("id") != record_id]
            if len(remaining) == len(records):
                return False
            self._write_locked(remaining)
            return True

    def _load_locked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        raw = self.path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"pending result store is corrupted: {self.path}") from error
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise RuntimeError(f"pending result store must contain a JSON list: {self.path}")
        return data

    def _write_locked(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                os.chmod(handle.name, 0o600)
                json.dump(records, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            temp_path.replace(self.path)
            self.path.chmod(0o600)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)
