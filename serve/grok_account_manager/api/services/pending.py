"""Durable database storage for registration results awaiting final persistence."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from ...core import database

OAUTH_PENDING = "oauth_pending"
PERSISTENCE_FAILED = "persistence_failed"
_VALID_STATUSES = {OAUTH_PENDING, PERSISTENCE_FAILED}


class PendingResultStore:
    """SQLite-backed store for sensitive in-flight registration results."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(db_path) if db_path else database.DB_PATH
        self._lock = threading.RLock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(descriptor)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        os.chmod(self.path, 0o600)
        return conn

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_registration_results (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT '',
                    provider_name TEXT NOT NULL DEFAULT 'grok',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    write_txt INTEGER NOT NULL DEFAULT 0,
                    oauth_required INTEGER NOT NULL DEFAULT 0,
                    job_id TEXT NOT NULL DEFAULT '',
                    worker_index INTEGER NOT NULL DEFAULT 0,
                    round_index INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        try:
            result = json.loads(row["result_json"] or "{}")
        except Exception:
            result = {}
        return {
            "id": row["id"],
            "status": row["status"],
            "provider_name": row["provider_name"],
            "result": result,
            "write_txt": bool(row["write_txt"]),
            "oauth_required": bool(row["oauth_required"]),
            "job_id": row["job_id"],
            "worker_index": row["worker_index"],
            "round_index": row["round_index"],
            "error": row["error"],
            "attempts": row["attempts"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM pending_registration_results ORDER BY created_at, id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pending_registration_results WHERE status = ? ORDER BY created_at, id",
                    (status,),
                ).fetchall()
        return [deepcopy(self._record(row)) for row in rows]

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
        timestamp = int(time.time() * 1000)
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM pending_registration_results WHERE id = ?", (clean_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO pending_registration_results
                        (id, status, provider_name, result_json, write_txt, oauth_required,
                         job_id, worker_index, round_index, error, attempts, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        clean_id,
                        status,
                        str(provider_name or "grok"),
                        json.dumps(result, ensure_ascii=False),
                        int(bool(write_txt)),
                        int(bool(oauth_required)),
                        str(job_id or ""),
                        int(worker_index or 0),
                        int(round_index or 0),
                        str(error or ""),
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE pending_registration_results SET
                        status = ?, provider_name = ?, result_json = ?, write_txt = ?,
                        oauth_required = ?, job_id = ?, worker_index = ?, round_index = ?,
                        error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        str(provider_name or "grok"),
                        json.dumps(result, ensure_ascii=False),
                        int(bool(write_txt)),
                        int(bool(oauth_required)),
                        str(job_id or existing["job_id"] or ""),
                        int(worker_index or existing["worker_index"] or 0),
                        int(round_index or existing["round_index"] or 0),
                        str(error or ""),
                        timestamp,
                        clean_id,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM pending_registration_results WHERE id = ?", (clean_id,)
            ).fetchone()
        return deepcopy(self._record(row))

    def note_error(self, record_id: str, error: str, *, increment_attempts: bool = False) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_registration_results
                SET error = ?, updated_at = ?, attempts = attempts + ?
                WHERE id = ?
                """,
                (
                    str(error or ""),
                    int(time.time() * 1000),
                    1 if increment_attempts else 0,
                    str(record_id or ""),
                ),
            )
        return bool(cursor.rowcount)

    def remove(self, record_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM pending_registration_results WHERE id = ?",
                (str(record_id or ""),),
            )
        return bool(cursor.rowcount)
