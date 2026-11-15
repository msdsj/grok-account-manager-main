"""SQLite store for accounts, quotas and operation logs."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .paths import OUTPUT_DIR


load_dotenv()

_LOG_WRITE_LOCK = threading.RLock()


def _resolve_db_path() -> Path:
    raw = str(
        os.environ.get("GROK_ACCOUNT_MANAGER_DB_PATH")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()
    if raw.startswith("sqlite:///"):
        raw = raw[len("sqlite:///") :]
    if raw and not raw.startswith(("postgres://", "postgresql://", "mysql://", "mariadb://")):
        return Path(raw).expanduser()
    return OUTPUT_DIR / "grok-account-manager.db"


DB_PATH = _resolve_db_path()


def _database_sidecar_paths() -> tuple[Path, Path]:
    return (
        DB_PATH.with_name(f"{DB_PATH.name}-wal"),
        DB_PATH.with_name(f"{DB_PATH.name}-shm"),
    )


def _secure_database_files() -> None:
    for path in (DB_PATH, *_database_sidecar_paths()):
        try:
            path.chmod(0o600)
        except FileNotFoundError:
            continue


def _prepare_private_database_file() -> None:
    file_descriptor = os.open(DB_PATH, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(file_descriptor)
    DB_PATH.chmod(0o600)


def _connect() -> sqlite3.Connection:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _prepare_private_database_file()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _secure_database_files()
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        try:
            _secure_database_files()
        finally:
            conn.close()
        _secure_database_files()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def init_db() -> None:
    with _connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                export_key TEXT PRIMARY KEY,
                account_json TEXT NOT NULL,
                credential_json TEXT NOT NULL,
                file_name TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL DEFAULT '',
                item_index INTEGER NOT NULL DEFAULT 0,
                email TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0,
                deleted_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_test_results (
                export_key TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                tested_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(export_key) REFERENCES accounts(export_key) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_deleted_at ON accounts(deleted_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email)")
        _ensure_column(conn, "accounts", "tags_json", "tags_json TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "accounts", "provider", "provider TEXT NOT NULL DEFAULT 'grok'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS operation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL DEFAULT 'admin',
                action TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT '',
                target_id TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}',
                result TEXT NOT NULL DEFAULT 'success',
                created_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_operation_logs_created ON operation_logs(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_operation_logs_action ON operation_logs(action)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                stage TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                worker INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job_logs_job ON job_logs(job_id, created_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS system_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL DEFAULT 'info',
                source TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_system_logs_created ON system_logs(created_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_runs (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                total INTEGER NOT NULL DEFAULT 0,
                concurrency INTEGER NOT NULL DEFAULT 0,
                config_json TEXT NOT NULL DEFAULT '{}',
                started_at INTEGER NOT NULL DEFAULT 0,
                finished_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_registration_results(status)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                key_hash TEXT NOT NULL UNIQUE,
                key_prefix TEXT NOT NULL DEFAULT '',
                scopes TEXT NOT NULL DEFAULT 'chat,image',
                models_json TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL DEFAULT 0,
                last_used_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_catalog (
                public_model TEXT PRIMARY KEY,
                provider TEXT NOT NULL DEFAULT 'grok',
                upstream_model TEXT NOT NULL DEFAULT '',
                capability TEXT NOT NULL DEFAULT 'chat',
                enabled INTEGER NOT NULL DEFAULT 1,
                weight INTEGER NOT NULL DEFAULT 1,
                config_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gateway_requests (
                id TEXT PRIMARY KEY,
                client_key_id TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                capability TEXT NOT NULL DEFAULT '',
                account_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'success',
                http_status INTEGER NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                stream INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                request_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gateway_requests_created ON gateway_requests(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gateway_requests_provider ON gateway_requests(provider, created_at)")


def _provider_from_file_name(file_name: str) -> str:
    name = str(file_name or "").lower()
    if "chatgpt" in name:
        return "chatgpt"
    if "kiro" in name:
        return "kiro"
    if "trae" in name:
        return "trae"
    return "grok"


def cleanup_removed_providers() -> int:
    """Soft-delete accounts that belong to providers removed from the project."""
    init_db()
    now = int(time.time() * 1000)
    with _connection() as conn:
        cursor = conn.execute(
            """
            UPDATE accounts SET deleted_at = ?, updated_at = ?
            WHERE deleted_at IS NULL AND (provider = 'trae' OR file_name LIKE '%trae%')
            """,
            (now, now),
        )
    return int(cursor.rowcount or 0)


def upsert_account(account: dict[str, Any], credential: dict[str, Any], file_path: Path, item_index: int) -> None:
    init_db()
    export_key = str(account.get("exportKey") or "").strip()
    if not export_key:
        return
    now = int(time.time() * 1000)
    provider = str(account.get("provider") or _provider_from_file_name(file_path.name) or "grok").strip().lower()
    with _connection() as conn:
        existing = conn.execute(
            "SELECT deleted_at, tags_json FROM accounts WHERE export_key = ?",
            (export_key,),
        ).fetchone()
        if existing and existing["deleted_at"] is not None:
            return
        if account.get("tags") is not None:
            tags = [str(tag).strip() for tag in account.get("tags") or [] if str(tag).strip()]
            tags_json = json.dumps(sorted(set(tags)), ensure_ascii=False)
        elif existing:
            tags_json = existing["tags_json"] or "[]"
        else:
            tags_json = "[]"
        conn.execute(
            """
            INSERT INTO accounts (
                export_key, account_json, credential_json, file_name, file_path,
                item_index, email, provider, tags_json, created_at, updated_at, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(export_key) DO UPDATE SET
                account_json = excluded.account_json,
                credential_json = excluded.credential_json,
                file_name = excluded.file_name,
                file_path = excluded.file_path,
                item_index = excluded.item_index,
                email = excluded.email,
                provider = excluded.provider,
                tags_json = excluded.tags_json,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (
                export_key,
                json.dumps(account, ensure_ascii=False),
                json.dumps(credential, ensure_ascii=False),
                file_path.name,
                str(file_path),
                item_index,
                str(account.get("email") or ""),
                provider,
                tags_json,
                int(account.get("createdAt") or 0),
                now,
            ),
        )


def update_account_credential(export_key: str, account: dict[str, Any], credential: dict[str, Any]) -> None:
    init_db()
    now = int(time.time() * 1000)
    with _connection() as conn:
        conn.execute(
            """
            UPDATE accounts
            SET account_json = ?, credential_json = ?, email = ?, created_at = ?, updated_at = ?
            WHERE export_key = ? AND deleted_at IS NULL
            """,
            (
                json.dumps(account, ensure_ascii=False),
                json.dumps(credential, ensure_ascii=False),
                str(account.get("email") or ""),
                int(account.get("createdAt") or 0),
                now,
                export_key,
            ),
        )


def list_accounts() -> list[dict[str, Any]]:
    init_db()
    with _connection() as conn:
        rows = conn.execute(
            """
            SELECT export_key, account_json, provider, tags_json
            FROM accounts
            WHERE deleted_at IS NULL AND file_name NOT LIKE '%trae%'
            ORDER BY updated_at DESC, created_at DESC
            """
        ).fetchall()
    accounts: list[dict[str, Any]] = []
    for row in rows:
        try:
            account = json.loads(row["account_json"])
        except Exception:
            continue
        if not isinstance(account, dict):
            continue
        account["exportKey"] = row["export_key"]
        account["provider"] = account.get("provider") or row["provider"] or "grok"
        account["tags"] = json.loads(row["tags_json"] or "[]")
        accounts.append(account)
    return accounts


def list_account_test_results() -> dict[str, dict[str, Any]]:
    init_db()
    with _connection() as conn:
        rows = conn.execute("SELECT export_key, result_json FROM account_test_results").fetchall()
    results: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            result = json.loads(row["result_json"])
        except Exception:
            continue
        if isinstance(result, dict):
            results[row["export_key"]] = result
    return results


def upsert_account_test_result(result: dict[str, Any]) -> None:
    init_db()
    export_key = str(result.get("exportKey") or "").strip()
    if not export_key:
        return
    now = int(time.time() * 1000)
    tested_at = int(result.get("testedAt") or now)
    with _connection() as conn:
        try:
            conn.execute(
                """
                INSERT INTO account_test_results (export_key, result_json, tested_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(export_key) DO UPDATE SET
                    result_json = excluded.result_json,
                    tested_at = excluded.tested_at,
                    updated_at = excluded.updated_at
                """,
                (export_key, json.dumps(result, ensure_ascii=False), tested_at, now),
            )
        except sqlite3.IntegrityError:
            return


def save_account_test_results(results: list[dict[str, Any]]) -> None:
    for result in results:
        upsert_account_test_result(result)


def _decorate_account_row(row: sqlite3.Row, account: dict[str, Any]) -> dict[str, Any]:
    account["exportKey"] = row["export_key"]
    account["provider"] = account.get("provider") or row["provider"] or "grok"
    try:
        account["tags"] = json.loads(row["tags_json"] or "[]")
    except Exception:
        account["tags"] = []
    return account


def get_credential_refs(export_keys: list[str]) -> list[dict[str, Any]]:
    init_db()
    keys = [str(key or "").strip() for key in export_keys if str(key or "").strip()]
    if not keys:
        return []
    placeholders = ",".join("?" for _ in keys)
    with _connection() as conn:
        rows = conn.execute(
            f"""
            SELECT export_key, credential_json, account_json, file_path, item_index, provider, tags_json
            FROM accounts
            WHERE deleted_at IS NULL AND export_key IN ({placeholders})
            """,
            keys,
        ).fetchall()
    refs = []
    by_key = {row["export_key"]: row for row in rows}
    for key in keys:
        row = by_key.get(key)
        if not row:
            continue
        try:
            credential = json.loads(row["credential_json"])
            account = json.loads(row["account_json"])
        except Exception:
            continue
        if isinstance(credential, dict) and isinstance(account, dict):
            refs.append(
                {
                    "export_key": row["export_key"],
                    "credential": credential,
                    "account": _decorate_account_row(row, account),
                    "file_path": row["file_path"],
                    "item_index": int(row["item_index"] or 0),
                }
            )
    return refs


def list_credential_refs(provider: str = "") -> list[dict[str, Any]]:
    """按 provider 列出可用账号及其凭证，供网关选号使用。"""
    init_db()
    where = ["deleted_at IS NULL", "file_name NOT LIKE '%trae%'"]
    params: list[Any] = []
    target = str(provider or "").strip().lower()
    if target:
        where.append("provider = ?")
        params.append(target)
    clause = " AND ".join(where)
    with _connection() as conn:
        rows = conn.execute(
            f"""
            SELECT export_key, credential_json, account_json, file_path, item_index, provider, tags_json
            FROM accounts
            WHERE {clause}
            ORDER BY created_at DESC, updated_at DESC
            """,
            params,
        ).fetchall()
    refs: list[dict[str, Any]] = []
    for row in rows:
        try:
            credential = json.loads(row["credential_json"])
            account = json.loads(row["account_json"])
        except Exception:
            continue
        if not isinstance(credential, dict) or not isinstance(account, dict):
            continue
        refs.append(
            {
                "export_key": row["export_key"],
                "credential": credential,
                "account": _decorate_account_row(row, account),
                "file_path": row["file_path"],
                "item_index": int(row["item_index"] or 0),
            }
        )
    return refs


def export_credentials(export_keys: list[str] | None = None) -> list[dict[str, Any]]:
    init_db()
    keys = [str(key or "").strip() for key in (export_keys or []) if str(key or "").strip()]
    params: tuple[Any, ...] = ()
    where = "deleted_at IS NULL"
    if keys:
        placeholders = ",".join("?" for _ in keys)
        where += f" AND export_key IN ({placeholders})"
        params = tuple(keys)
    with _connection() as conn:
        rows = conn.execute(
            f"SELECT export_key, credential_json FROM accounts WHERE {where} ORDER BY updated_at DESC",
            params,
        ).fetchall()
    by_key: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for row in rows:
        try:
            credential = json.loads(row["credential_json"])
        except Exception:
            continue
        if isinstance(credential, dict):
            by_key[row["export_key"]] = credential
            ordered.append(credential)
    if keys:
        return [by_key[key] for key in keys if key in by_key]
    return ordered


def find_account_ref_by_id(account_id: str) -> dict[str, Any] | None:
    init_db()
    target = str(account_id or "").strip()
    if not target:
        return None
    with _connection() as conn:
        rows = conn.execute(
            """
            SELECT export_key, credential_json, account_json, file_path, item_index, provider, tags_json
            FROM accounts
            WHERE deleted_at IS NULL
            """
        ).fetchall()
    for row in rows:
        try:
            credential = json.loads(row["credential_json"])
            account = json.loads(row["account_json"])
        except Exception:
            continue
        if not isinstance(credential, dict) or not isinstance(account, dict):
            continue
        if target in {
            str(account.get("id") or ""),
            str(account.get("exportKey") or ""),
            str(credential.get("id") or ""),
            str(credential.get("email") or ""),
        }:
            return {
                "export_key": row["export_key"],
                "credential": credential,
                "account": _decorate_account_row(row, account),
                "file_path": row["file_path"],
                "item_index": int(row["item_index"] or 0),
            }
    return None


def soft_delete_accounts(export_keys: list[str]) -> int:
    """Hide selected accounts without removing their audit trail."""
    init_db()
    keys = [str(key or "").strip() for key in export_keys if str(key or "").strip()]
    if not keys:
        return 0
    now = int(time.time() * 1000)
    placeholders = ",".join("?" for _ in keys)
    with _connection() as conn:
        cursor = conn.execute(
            f"UPDATE accounts SET deleted_at = ?, updated_at = ? WHERE deleted_at IS NULL AND export_key IN ({placeholders})",
            (now, now, *keys),
        )
    return int(cursor.rowcount or 0)


def hard_delete_accounts(export_keys: list[str] | set[str]) -> int:
    """Permanently remove selected local account rows and their test results."""
    init_db()
    keys = sorted({str(key or "").strip() for key in export_keys if str(key or "").strip()})
    if not keys:
        return 0
    placeholders = ",".join("?" for _ in keys)
    with _connection() as conn:
        conn.execute(
            f"DELETE FROM account_test_results WHERE export_key IN ({placeholders})",
            keys,
        )
        cursor = conn.execute(
            f"DELETE FROM accounts WHERE export_key IN ({placeholders})",
            keys,
        )
    return int(cursor.rowcount or 0)


def list_account_tags() -> list[dict[str, Any]]:
    """Return every tag with the number of active accounts using it."""
    init_db()
    with _connection() as conn:
        rows = conn.execute(
            "SELECT tags_json FROM accounts WHERE deleted_at IS NULL"
        ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        try:
            tags = json.loads(row["tags_json"] or "[]")
        except Exception:
            continue
        for tag in tags if isinstance(tags, list) else []:
            value = str(tag or "").strip()
            if value:
                counts[value] = counts.get(value, 0) + 1
    return [{"tag": tag, "count": count} for tag, count in sorted(counts.items())]


def update_account_tags(
    export_keys: list[str],
    tags: list[str],
    mode: str = "replace",
) -> dict[str, int]:
    """Add, remove or replace tags for the selected accounts."""
    init_db()
    keys = sorted({str(key or "").strip() for key in export_keys if str(key or "").strip()})
    cleaned = []
    for tag in tags or []:
        value = str(tag or "").strip()[:32]
        if value and value not in cleaned:
            cleaned.append(value)
    cleaned = cleaned[:20]
    if not keys:
        return {"updated": 0}
    updated = 0
    with _connection() as conn:
        for export_key in keys:
            row = conn.execute(
                "SELECT tags_json FROM accounts WHERE export_key = ? AND deleted_at IS NULL",
                (export_key,),
            ).fetchone()
            if not row:
                continue
            try:
                current = json.loads(row["tags_json"] or "[]")
            except Exception:
                current = []
            current = [str(tag) for tag in current if str(tag).strip()]
            if mode == "remove":
                result = [tag for tag in current if tag not in cleaned]
            elif mode == "add":
                result = current + [tag for tag in cleaned if tag not in current]
            else:
                result = cleaned
            conn.execute(
                "UPDATE accounts SET tags_json = ?, updated_at = ? WHERE export_key = ?",
                (json.dumps(result, ensure_ascii=False), int(time.time() * 1000), export_key),
            )
            updated += 1
    return {"updated": updated}


def get_meta(key: str, default: str = "") -> str:
    init_db()
    with _connection() as conn:
        row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_meta(key: str, value: str) -> None:
    init_db()
    with _connection() as conn:
        conn.execute(
            """
            INSERT INTO app_meta(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, str(value), int(time.time() * 1000)),
        )


def insert_operation_log(
    actor: str,
    action: str,
    *,
    target_type: str = "",
    target_id: str = "",
    detail: dict[str, Any] | None = None,
    result: str = "success",
) -> int:
    init_db()
    with _LOG_WRITE_LOCK, _connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO operation_logs
                (actor, action, target_type, target_id, detail_json, result, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(actor or "admin")[:128],
                str(action or "unknown")[:128],
                str(target_type or "")[:64],
                str(target_id or "")[:256],
                json.dumps(detail or {}, ensure_ascii=False),
                str(result or "success")[:32],
                int(time.time() * 1000),
            ),
        )
    return int(cursor.lastrowid or 0)


def list_operation_logs(
    *,
    actor: str = "",
    action: str = "",
    result: str = "",
    start_at: int = 0,
    end_at: int = 0,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    init_db()
    where = ["1=1"]
    params: list[Any] = []
    if actor:
        where.append("actor = ?")
        params.append(actor)
    if action:
        where.append("action LIKE ?")
        params.append(f"%{action}%")
    if result:
        where.append("result LIKE ?")
        params.append(f"{result}%")
    if start_at:
        where.append("created_at >= ?")
        params.append(int(start_at))
    if end_at:
        where.append("created_at <= ?")
        params.append(int(end_at))
    clause = " AND ".join(where)
    with _connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS count FROM operation_logs WHERE {clause}", params).fetchone()["count"]
        rows = conn.execute(
            f"""
            SELECT id, actor, action, target_type, target_id, detail_json, result, created_at
            FROM operation_logs
            WHERE {clause}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, max(1, min(500, limit)), max(0, offset)),
        ).fetchall()
    items = []
    for row in rows:
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except Exception:
            detail = {}
        items.append(
            {
                "id": row["id"],
                "actor": row["actor"],
                "action": row["action"],
                "targetType": row["target_type"],
                "targetId": row["target_id"],
                "detail": detail,
                "result": row["result"],
                "createdAt": row["created_at"],
            }
        )
    return {"items": items, "total": total}


def insert_job_log(
    job_id: str,
    level: str,
    message: str,
    *,
    stage: str = "",
    worker: int = 0,
    payload: dict[str, Any] | None = None,
    created_at: int | None = None,
) -> None:
    init_db()
    with _LOG_WRITE_LOCK, _connection() as conn:
        conn.execute(
            """
            INSERT INTO job_logs(job_id, level, stage, message, worker, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(job_id or ""),
                str(level or "info")[:16],
                str(stage or "")[:64],
                str(message or "")[:4000],
                int(worker or 0),
                json.dumps(payload or {}, ensure_ascii=False),
                int(created_at or time.time() * 1000),
            ),
        )


def list_job_logs(job_id: str, *, limit: int = 500, offset: int = 0) -> dict[str, Any]:
    init_db()
    with _connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS count FROM job_logs WHERE job_id = ?", (job_id,)
        ).fetchone()["count"]
        rows = conn.execute(
            """
            SELECT id, level, stage, message, worker, payload_json, created_at
            FROM job_logs
            WHERE job_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (job_id, max(1, min(2000, limit)), max(0, offset)),
        ).fetchall()
    items = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        items.append(
            {
                "id": row["id"],
                "level": row["level"],
                "stage": row["stage"],
                "message": row["message"],
                "worker": row["worker"],
                "payload": payload,
                "createdAt": row["created_at"],
            }
        )
    return {"items": items, "total": total}


def insert_system_log(
    level: str,
    source: str,
    message: str,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    init_db()
    with _LOG_WRITE_LOCK, _connection() as conn:
        conn.execute(
            """
            INSERT INTO system_logs(level, source, message, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(level or "info")[:16],
                str(source or "")[:64],
                str(message or "")[:4000],
                json.dumps(detail or {}, ensure_ascii=False),
                int(time.time() * 1000),
            ),
        )


def list_system_logs(*, level: str = "", source: str = "", limit: int = 200, offset: int = 0) -> dict[str, Any]:
    init_db()
    where = ["1=1"]
    params: list[Any] = []
    if level:
        where.append("level = ?")
        params.append(level)
    if source:
        where.append("source LIKE ?")
        params.append(f"%{source}%")
    clause = " AND ".join(where)
    with _connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS count FROM system_logs WHERE {clause}", params).fetchone()["count"]
        rows = conn.execute(
            f"""
            SELECT id, level, source, message, detail_json, created_at
            FROM system_logs WHERE {clause}
            ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
            """,
            (*params, max(1, min(1000, limit)), max(0, offset)),
        ).fetchall()
    items = []
    for row in rows:
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except Exception:
            detail = {}
        items.append(
            {
                "id": row["id"],
                "level": row["level"],
                "source": row["source"],
                "message": row["message"],
                "detail": detail,
                "createdAt": row["created_at"],
            }
        )
    return {"items": items, "total": total}


def upsert_job_run(
    job_id: str,
    *,
    provider: str = "",
    status: str = "running",
    total: int = 0,
    concurrency: int = 0,
    config: dict[str, Any] | None = None,
    started_at: int = 0,
    finished_at: int = 0,
) -> None:
    init_db()
    now = int(time.time() * 1000)
    with _LOG_WRITE_LOCK, _connection() as conn:
        if config is None:
            row = conn.execute("SELECT config_json, started_at FROM job_runs WHERE id = ?", (str(job_id or ""),)).fetchone()
            config_json = row["config_json"] if row else "{}"
            if row:
                started_at = started_at or int(row["started_at"] or 0)
        else:
            config_json = json.dumps(config, ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO job_runs
                (id, provider, status, total, concurrency, config_json, started_at, finished_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                provider = excluded.provider,
                status = excluded.status,
                total = excluded.total,
                concurrency = excluded.concurrency,
                config_json = excluded.config_json,
                finished_at = excluded.finished_at,
                updated_at = excluded.updated_at
            """,
            (
                str(job_id or ""),
                str(provider or ""),
                str(status or "running"),
                int(total or 0),
                int(concurrency or 0),
                config_json,
                int(started_at or now),
                int(finished_at or 0),
                now,
            ),
        )


def list_job_runs(*, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    init_db()
    with _connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS count FROM job_runs").fetchone()["count"]
        rows = conn.execute(
            """
            SELECT id, provider, status, total, concurrency, config_json, started_at, finished_at, updated_at
            FROM job_runs ORDER BY started_at DESC LIMIT ? OFFSET ?
            """,
            (max(1, min(500, limit)), max(0, offset)),
        ).fetchall()
    items = []
    for row in rows:
        try:
            config = json.loads(row["config_json"] or "{}")
        except Exception:
            config = {}
        items.append(
            {
                "id": row["id"],
                "provider": row["provider"],
                "status": row["status"],
                "total": row["total"],
                "concurrency": row["concurrency"],
                "config": config,
                "startedAt": row["started_at"],
                "finishedAt": row["finished_at"],
                "updatedAt": row["updated_at"],
            }
        )
    return {"items": items, "total": total}


def _client_key_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        models = json.loads(row["models_json"] or "[]")
    except Exception:
        models = []
    return {
        "id": row["id"],
        "name": row["name"],
        "keyPrefix": row["key_prefix"],
        "scopes": [part for part in str(row["scopes"] or "").split(",") if part],
        "models": models if isinstance(models, list) else [],
        "enabled": bool(row["enabled"]),
        "createdAt": row["created_at"],
        "lastUsedAt": row["last_used_at"],
    }


def count_client_keys() -> int:
    init_db()
    with _connection() as conn:
        return int(conn.execute("SELECT COUNT(*) AS count FROM client_keys").fetchone()["count"] or 0)


def list_client_keys() -> list[dict[str, Any]]:
    init_db()
    with _connection() as conn:
        rows = conn.execute(
            """
            SELECT id, name, key_prefix, scopes, models_json, enabled, created_at, last_used_at
            FROM client_keys ORDER BY created_at DESC
            """
        ).fetchall()
    return [_client_key_row(row) for row in rows]


def insert_client_key(
    key_id: str,
    name: str,
    key_hash: str,
    key_prefix: str,
    *,
    scopes: list[str],
    models: list[str] | None = None,
) -> None:
    init_db()
    with _connection() as conn:
        conn.execute(
            """
            INSERT INTO client_keys (id, name, key_hash, key_prefix, scopes, models_json, enabled, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                str(key_id),
                str(name or "")[:128],
                str(key_hash),
                str(key_prefix or "")[:24],
                ",".join(scopes),
                json.dumps(models or [], ensure_ascii=False),
                int(time.time() * 1000),
            ),
        )


def delete_client_key(key_id: str) -> int:
    init_db()
    with _connection() as conn:
        cursor = conn.execute("DELETE FROM client_keys WHERE id = ?", (str(key_id),))
    return int(cursor.rowcount or 0)


def find_client_key_by_hash(key_hash: str) -> dict[str, Any] | None:
    init_db()
    with _connection() as conn:
        row = conn.execute(
            """
            SELECT id, name, key_prefix, scopes, models_json, enabled, created_at, last_used_at
            FROM client_keys WHERE key_hash = ?
            """,
            (str(key_hash),),
        ).fetchone()
    return _client_key_row(row) if row else None


def touch_client_key(key_id: str) -> None:
    if not key_id:
        return
    init_db()
    with _connection() as conn:
        conn.execute(
            "UPDATE client_keys SET last_used_at = ? WHERE id = ?",
            (int(time.time() * 1000), str(key_id)),
        )


def list_model_catalog() -> list[dict[str, Any]]:
    init_db()
    with _connection() as conn:
        rows = conn.execute(
            """
            SELECT public_model, provider, upstream_model, capability, enabled, weight, config_json, updated_at
            FROM model_catalog ORDER BY provider, capability, public_model
            """
        ).fetchall()
    items = []
    for row in rows:
        try:
            config = json.loads(row["config_json"] or "{}")
        except Exception:
            config = {}
        items.append(
            {
                "publicModel": row["public_model"],
                "provider": row["provider"],
                "upstreamModel": row["upstream_model"],
                "capability": row["capability"],
                "enabled": bool(row["enabled"]),
                "weight": int(row["weight"] or 1),
                "config": config if isinstance(config, dict) else {},
                "updatedAt": row["updated_at"],
            }
        )
    return items


def seed_model_catalog(entries: list[dict[str, Any]]) -> None:
    """Insert missing default model mappings without overriding user edits."""
    if not entries:
        return
    init_db()
    now = int(time.time() * 1000)
    with _connection() as conn:
        for entry in entries:
            public_model = str(entry.get("publicModel") or "").strip()
            if not public_model:
                continue
            conn.execute(
                """
                INSERT INTO model_catalog
                    (public_model, provider, upstream_model, capability, enabled, weight, config_json, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, '{}', ?)
                ON CONFLICT(public_model) DO NOTHING
                """,
                (
                    public_model,
                    str(entry.get("provider") or "grok"),
                    str(entry.get("upstreamModel") or public_model),
                    str(entry.get("capability") or "chat"),
                    int(entry.get("weight") or 1),
                    now,
                ),
            )


def insert_gateway_request(
    request_id: str,
    *,
    client_key_id: str = "",
    provider: str = "",
    model: str = "",
    capability: str = "",
    account_id: str = "",
    status: str = "success",
    http_status: int = 0,
    latency_ms: int = 0,
    stream: bool = False,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    error: str = "",
    request: dict[str, Any] | None = None,
) -> None:
    init_db()
    with _LOG_WRITE_LOCK, _connection() as conn:
        conn.execute(
            """
            INSERT INTO gateway_requests
                (id, client_key_id, provider, model, capability, account_id, status, http_status,
                 latency_ms, stream, prompt_tokens, completion_tokens, error, request_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(request_id),
                str(client_key_id or "")[:64],
                str(provider or "")[:32],
                str(model or "")[:128],
                str(capability or "")[:32],
                str(account_id or "")[:128],
                str(status or "success")[:32],
                int(http_status or 0),
                int(latency_ms or 0),
                1 if stream else 0,
                int(prompt_tokens or 0),
                int(completion_tokens or 0),
                str(error or "")[:2000],
                json.dumps(request or {}, ensure_ascii=False)[:20000],
                int(time.time() * 1000),
            ),
        )


def aggregate_gateway_requests(*, start_at: int = 0, bucket_seconds: int = 3600) -> list[dict[str, Any]]:
    """按时间桶、provider、model 聚合网关请求，供仪表盘使用。"""
    init_db()
    bucket_ms = max(1, int(bucket_seconds)) * 1000
    where = "created_at >= ?" if start_at else "1=1"
    params: list[Any] = [int(start_at)] if start_at else []
    with _connection() as conn:
        rows = conn.execute(
            f"""
            SELECT (created_at / ?) * ? AS bucket,
                   provider,
                   model,
                   status,
                   COUNT(*) AS requests,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens
            FROM gateway_requests
            WHERE {where}
            GROUP BY bucket, provider, model, status
            ORDER BY bucket ASC
            """,
            (bucket_ms, bucket_ms, *params),
        ).fetchall()
    return [
        {
            "bucket": int(row["bucket"] or 0),
            "provider": str(row["provider"] or ""),
            "model": str(row["model"] or ""),
            "status": str(row["status"] or ""),
            "requests": int(row["requests"] or 0),
            "promptTokens": int(row["prompt_tokens"] or 0),
            "completionTokens": int(row["completion_tokens"] or 0),
        }
        for row in rows
    ]


def list_gateway_requests(
    *,
    provider: str = "",
    status: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    init_db()
    where = ["1=1"]
    params: list[Any] = []
    if provider:
        where.append("provider = ?")
        params.append(provider)
    if status:
        where.append("status = ?")
        params.append(status)
    clause = " AND ".join(where)
    with _connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS count FROM gateway_requests WHERE {clause}", params).fetchone()["count"]
        rows = conn.execute(
            f"""
            SELECT id, client_key_id, provider, model, capability, account_id, status, http_status,
                   latency_ms, stream, prompt_tokens, completion_tokens, error, created_at
            FROM gateway_requests
            WHERE {clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (*params, max(1, min(500, limit)), max(0, offset)),
        ).fetchall()
    items = []
    for row in rows:
        items.append(
            {
                "id": row["id"],
                "clientKeyId": row["client_key_id"],
                "provider": row["provider"],
                "model": row["model"],
                "capability": row["capability"],
                "accountId": row["account_id"],
                "status": row["status"],
                "httpStatus": row["http_status"],
                "latencyMs": row["latency_ms"],
                "stream": bool(row["stream"]),
                "promptTokens": row["prompt_tokens"],
                "completionTokens": row["completion_tokens"],
                "error": row["error"],
                "createdAt": row["created_at"],
            }
        )
    return {"items": items, "total": total}

