"""JSON  格式凭证 sink - 将所有 Grok 凭证追加到单个 JSON 数组文件。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..grok.client import build_cockpit_grok_credential

if TYPE_CHECKING:
    from ..providers.base import RegistrationResult

CREDENTIALS_FILENAME = "grok_credentials.json"


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
        if "full_credential" in result:
            self._pending.append(result["full_credential"])
        else:
            credential = build_cockpit_grok_credential(
                email=result["email"],
                access_token=result["credential"],
                profile=result.get("profile"),
            )
            credential["sso_token"] = result["credential"]
            self._pending.append(credential)

    def flush(self) -> None:
        if not self._pending:
            return
        filepath = self.output_dir / CREDENTIALS_FILENAME
        pending = list(self._pending)
        try:
            existing = _load_existing_credentials(filepath)
            start_index = len(existing)
            existing.extend(pending)
            filepath.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._persist_pending_to_database(filepath, pending, start_index)
            print(f"[JsonCredentialSink] 已写入 {len(pending)} 条凭证到 {filepath}（共 {len(existing)} 条）")
        except Exception as e:
            print(f"[JsonCredentialSink] 保存凭证失败: {e}")
        finally:
            self._pending.clear()

    def _persist_pending_to_database(self, filepath: Path, pending: list[dict], start_index: int) -> None:
        try:
            from ..api.services import database as account_db
            from ..api.services.accounts import account_from_credential

            for offset, credential in enumerate(pending):
                if not isinstance(credential, dict):
                    continue
                item_index = start_index + offset
                account = account_from_credential(credential, filepath, item_index)
                account_db.upsert_account(account, credential, filepath, item_index)
        except Exception as error:
            print(f"[JsonCredentialSink] 写入账号数据库失败: {error}")
