"""Small SQLite store for local account credentials and test results."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ..config import OUTPUT_DIR


load_dotenv()


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


def upsert_account(account: dict[str, Any], credential: dict[str, Any], file_path: Path, item_index: int) -> None:
    init_db()
    export_key = str(account.get("exportKey") or "").strip()
    if not export_key:
        return
    now = int(time.time() * 1000)
    with _connection() as conn:
        existing = conn.execute(
            "SELECT deleted_at FROM accounts WHERE export_key = ?",
            (export_key,),
        ).fetchone()
        if existing and existing["deleted_at"] is not None:
            return
        conn.execute(
            """
            INSERT INTO accounts (
                export_key, account_json, credential_json, file_name, file_path,
                item_index, email, created_at, updated_at, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(export_key) DO UPDATE SET
                account_json = excluded.account_json,
                credential_json = excluded.credential_json,
                file_name = excluded.file_name,
                file_path = excluded.file_path,
                item_index = excluded.item_index,
                email = excluded.email,
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
            SELECT account_json
            FROM accounts
            WHERE deleted_at IS NULL
            ORDER BY updated_at DESC, created_at DESC
            """
        ).fetchall()
    accounts: list[dict[str, Any]] = []
    for row in rows:
        try:
            account = json.loads(row["account_json"])
        except Exception:
            continue
        if isinstance(account, dict):
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


def get_credential_refs(export_keys: list[str]) -> list[dict[str, Any]]:
    init_db()
    keys = [str(key or "").strip() for key in export_keys if str(key or "").strip()]
    if not keys:
        return []
    placeholders = ",".join("?" for _ in keys)
    with _connection() as conn:
        rows = conn.execute(
            f"""
            SELECT export_key, credential_json, account_json, file_path, item_index
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
                    "account": account,
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
            SELECT export_key, credential_json, account_json, file_path, item_index
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
                "account": account,
                "file_path": row["file_path"],
                "item_index": int(row["item_index"] or 0),
            }
    return None


def soft_delete_accounts(export_keys: list[str]) -> int:
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


def reconcile_file_backed_accounts(export_keys: set[str]) -> int:
    """Remove database rows whose credential file no longer contains them.

    The local account database is an index over JSON credential files, not a
    second independent source of truth.  Keeping this index reconciled prevents
    deleted or renamed credential files from being exported back into the relay.
    """
    init_db()
    keys = sorted({str(key or "").strip() for key in export_keys if str(key or "").strip()})
    with _connection() as conn:
        if keys:
            placeholders = ",".join("?" for _ in keys)
            cursor = conn.execute(
                f"DELETE FROM accounts WHERE export_key NOT IN ({placeholders})",
                keys,
            )
            conn.execute(
                f"DELETE FROM account_test_results WHERE export_key NOT IN ({placeholders})",
                keys,
            )
        else:
            cursor = conn.execute("DELETE FROM accounts")
            conn.execute("DELETE FROM account_test_results")
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
