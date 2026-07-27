"""JSON  格式凭证 sink - 将所有 Grok 凭证追加到单个 JSON 数组文件。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING

from ..grok.client import build_cockpit_grok_credential

if TYPE_CHECKING:
    from ..providers.base import RegistrationResult

CREDENTIALS_FILENAME = "grok_credentials.json"


def _normalized_email(credential: dict) -> str:
    email = str(credential.get("email") or "").strip().lower()
    return "" if email in {"", "unknown", "unknown@grok.local"} else email


def _credential_token(credential: dict) -> str:
    for key in ("sso_token", "sso", "credential", "cookie"):
        value = str(credential.get(key) or "").strip()
        if value:
            return value[4:] if value.startswith("sso=") else value
    return ""


def _find_credential_index(existing: list[dict], pending: dict) -> int | None:
    email = _normalized_email(pending)
    if email:
        for index, item in enumerate(existing):
            if _normalized_email(item) == email:
                return index

    token = _credential_token(pending)
    if token:
        for index, item in enumerate(existing):
            if _credential_token(item) == token:
                return index
    return None


def _merge_credential(existing: dict, pending: dict) -> dict:
    """Upgrade an SSO checkpoint without losing fields learned by an earlier pass."""
    merged = dict(existing)
    for key, value in pending.items():
        if value not in (None, "") or key not in merged:
            merged[key] = value

    if existing.get("id"):
        merged["id"] = existing["id"]
    if existing.get("created_at"):
        merged["created_at"] = existing["created_at"]

    old_auth = existing.get("auth_raw") if isinstance(existing.get("auth_raw"), dict) else {}
    new_auth = pending.get("auth_raw") if isinstance(pending.get("auth_raw"), dict) else {}
    if old_auth or new_auth:
        merged["auth_raw"] = {**old_auth, **{key: value for key, value in new_auth.items() if value not in (None, "")}}

    # A successful retry must be able to clear the previous failure text.
    for key in ("oauth_exchange_status", "oauth_exchange_error", "credential_enrichment_error"):
        if key in pending:
            merged[key] = pending[key]
    return merged


def _write_credentials_atomic(filepath: Path, credentials: list[dict]) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=filepath.parent,
            prefix=f".{filepath.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(credentials, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(filepath)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _load_existing_credentials(filepath: Path) -> list[dict]:
    if not filepath.exists():
        return []
    raw = filepath.read_text(encoding="utf-8").strip()
    if not raw:
        filepath.write_text("[]", encoding="utf-8")
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        filepath.write_text("[]", encoding="utf-8")
        return []
    if not isinstance(data, list):
        filepath.write_text("[]", encoding="utf-8")
        return []
    return [item for item in data if isinstance(item, dict)]


class JsonCredentialSink:
    def __init__(self, output_dir: str = "output/credentials"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._pending: list[dict] = []

    def push(self, provider_name: str, result: RegistrationResult) -> None:
        oauth_status = str(result.get("oauth_status") or "not_requested")
        if oauth_status in {"pending", "failed"}:
            raise ValueError("refresh_token 尚未成功获取，拒绝写入 JSON 凭证")
        if "full_credential" in result:
            credential = dict(result["full_credential"])
        else:
            credential = build_cockpit_grok_credential(
                email=result["email"],
                access_token=result["credential"],
                profile=result.get("profile"),
            )
            credential["sso_token"] = result["credential"]
        if oauth_status == "ready" and not str(credential.get("refresh_token") or "").strip():
            raise ValueError("OAuth 状态为 ready，但完整凭证缺少 refresh_token")
        credential["oauth_exchange_status"] = oauth_status
        credential["oauth_exchange_error"] = result.get("oauth_error") or credential.get("oauth_exchange_error") or None
        credential["credential_enrichment_error"] = (
            result.get("credential_enrichment_error")
            or credential.get("credential_enrichment_error")
            or None
        )
        self._pending.append(credential)

    def flush(self) -> None:
        if not self._pending:
            return
        filepath = self.output_dir / CREDENTIALS_FILENAME
        pending = list(self._pending)
        try:
            existing = _load_existing_credentials(filepath)
            persisted: dict[int, dict] = {}
            added = 0
            updated = 0
            for credential in pending:
                item_index = _find_credential_index(existing, credential)
                if item_index is None:
                    existing.append(credential)
                    item_index = len(existing) - 1
                    added += 1
                else:
                    existing[item_index] = _merge_credential(existing[item_index], credential)
                    updated += 1
                persisted[item_index] = existing[item_index]

            _write_credentials_atomic(filepath, existing)
            self._persist_pending_to_database(filepath, sorted(persisted.items()))
            print(
                f"[JsonCredentialSink] 已保存凭证到 {filepath}"
                f"（新增 {added}，更新 {updated}，共 {len(existing)} 条）"
            )
        except Exception as e:
            print(f"[JsonCredentialSink] 保存凭证失败: {e}")
            raise
        finally:
            self._pending.clear()

    def _persist_pending_to_database(self, filepath: Path, persisted: list[tuple[int, dict]]) -> None:
        try:
            from ..api.services import database as account_db
            from ..api.services.accounts import account_from_credential

            for item_index, credential in persisted:
                if not isinstance(credential, dict):
                    continue
                account = account_from_credential(credential, filepath, item_index)
                account_db.upsert_account(account, credential, filepath, item_index)
        except Exception as error:
            print(f"[JsonCredentialSink] 写入账号数据库失败: {error}")
