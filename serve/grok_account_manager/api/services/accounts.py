"""Account credential storage, export, quota refresh, and health checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

from ...grok.account_tester import send_grok_chat, test_grok_account
from ...sinks.json_credential import _write_credentials_atomic
from ..config import ACCOUNT_TEST_RESULTS_PATH, CREDENTIALS_DIR, OUTPUT_DIR, TXT_OUTPUT
from . import database as account_db
from .relay import RELAY_MANAGER

_ACCOUNTS_CACHE_LOCK = threading.RLock()
_CREDENTIAL_FILE_LOCK = threading.RLock()
_ACCOUNTS_CACHE_SIGNATURE: tuple[tuple[str, int, int], ...] | None = None
_ACCOUNTS_CACHE_DATA: list[dict] = []


def invalidate_accounts_cache() -> None:
    global _ACCOUNTS_CACHE_SIGNATURE
    with _ACCOUNTS_CACHE_LOCK:
        _ACCOUNTS_CACHE_SIGNATURE = None


def _format_created_at(value) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    seconds = timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))


def _token_expired(value) -> bool:
    try:
        expires_at = int(value or 0)
    except (TypeError, ValueError):
        return False
    return expires_at > 0 and expires_at <= int(time.time()) + 60


def account_export_key(file_path: Path, item_index: int) -> str:
    return f"{file_path.name}:{item_index}"


def account_from_credential(credential: dict, file_path: Path, item_index: int = 0) -> dict:
    email = str(credential.get("email") or "").strip()
    first_name = str(credential.get("first_name") or "").strip()
    last_name = str(credential.get("last_name") or "").strip()
    refresh_token = str(credential.get("refresh_token") or "").strip()
    access_token = str(credential.get("access_token") or "").strip()
    oauth_status = str(credential.get("oauth_exchange_status") or "").strip()
    if refresh_token:
        oauth_status = "ready"
    elif not oauth_status:
        oauth_status = "unknown"
    created_at = credential.get("created_at")
    account_id = str(credential.get("id") or "").strip() or file_path.stem
    has_web_access = bool(str(credential.get("sso_token") or "").strip())
    has_build_access = bool(refresh_token or str(credential.get("auth_mode") or "").lower() in {"oauth", "oidc"})
    providers = [provider for provider, enabled in (("build", has_build_access), ("web", has_web_access)) if enabled]

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
        "exportKey": account_export_key(file_path, item_index),
        "email": email or "unknown",
        "displayName": " ".join(part for part in [first_name, last_name] if part).strip(),
        "authMode": credential.get("auth_mode") or "oauth",
        "providers": providers,
        "buildAvailable": has_build_access,
        "webAvailable": has_web_access,
        "planType": credential.get("plan_type") or "",
        "hasGrokCodeAccess": credential.get("has_grok_code_access"),
        "userId": credential.get("user_id") or "",
        "createdAt": created_at or 0,
        "createdAtLabel": _format_created_at(created_at),
        "hasRefreshToken": bool(refresh_token),
        "hasAccessToken": bool(access_token),
        "oauthStatus": oauth_status,
        "oauthError": str(credential.get("oauth_exchange_error") or "").strip(),
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


def _sync_legacy_test_results_to_db() -> None:
    for result in _load_account_test_results().values():
        account_db.upsert_account_test_result(result)


def _save_account_test_results(results: list[dict]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNT_TEST_RESULTS_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    account_db.save_account_test_results(results)


def _merge_account_test_result(account: dict, test_results: dict[str, dict]) -> dict:
    result = test_results.get(str(account.get("exportKey") or ""))
    if not result:
        return account
    account["availability"] = {
        "category": result.get("category") or "unavailable",
        "baseAvailable": bool(result.get("baseAvailable")),
        "chatAvailable": bool(result.get("chatAvailable") or result.get("baseAvailable")),
        "cli45Available": bool(result.get("cli45Available") or result.get("grok45Available")),
        "grok45Available": bool(result.get("grok45Available")),
        "imageAvailable": bool(result.get("imageAvailable")),
        "baseModel": result.get("baseModel"),
        "chatModel": result.get("chatModel") or result.get("baseModel"),
        "cli45Model": result.get("cli45Model") or result.get("grok45Model"),
        "grok45Model": result.get("grok45Model"),
        "imageModel": result.get("imageModel"),
        "imageSource": result.get("imageSource"),
        "latencyMs": result.get("latencyMs"),
        "error": result.get("error"),
        "testedAt": result.get("testedAt"),
    }
    return account


def _sync_credential_files_to_db(files: list[tuple[Path, int, int]]) -> tuple[set[str], bool]:
    valid_keys: set[str] = set()
    complete = True
    for file_path, _, _ in files:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else [data]
            for index, item in enumerate(items):
                if isinstance(item, dict):
                    valid_keys.add(account_export_key(file_path, index))
                    account = account_from_credential(item, file_path, index)
                    account_db.upsert_account(account, item, file_path, index)
        except Exception:
            complete = False
    return valid_keys, complete


def _credential_identity_matches(
    credential: dict,
    emails: set[str],
    user_ids: set[str],
) -> bool:
    email = str(credential.get("email") or "").strip().lower()
    user_id = str(credential.get("user_id") or "").strip()
    return (bool(email) and email in emails) or (bool(user_id) and user_id in user_ids)


def _credential_token_values(credential: dict) -> set[str]:
    values: set[str] = set()
    for key in ("sso_token", "sso", "credential", "cookie"):
        value = str(credential.get(key) or "").strip()
        if value:
            values.add(value[4:] if value.startswith("sso=") else value)
    return values


def _write_private_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        path.chmod(0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write("".join(lines))
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def delete_local_credentials_for_upstream_accounts(upstream_accounts: list[dict]) -> dict:
    """Permanently remove local credentials represented by grok2api accounts."""
    emails = {
        str(item.get("email") or "").strip().lower()
        for item in upstream_accounts
        if isinstance(item, dict) and str(item.get("email") or "").strip()
    }
    user_ids = {
        str(item.get("userId") or item.get("user_id") or "").strip()
        for item in upstream_accounts
        if isinstance(item, dict) and str(item.get("userId") or item.get("user_id") or "").strip()
    }
    if not emails and not user_ids:
        return {"removed": 0, "matched": 0}

    removed_tokens: set[str] = set()
    removed_count = 0
    matched_keys: set[str] = set()
    with _CREDENTIAL_FILE_LOCK:
        CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        for file_path in sorted(CREDENTIALS_DIR.glob("*.json")):
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            is_list = isinstance(data, list)
            items = data if is_list else [data]
            kept: list[dict] = []
            changed = False
            for index, item in enumerate(items):
                if isinstance(item, dict) and _credential_identity_matches(item, emails, user_ids):
                    removed_count += 1
                    matched_keys.add(account_export_key(file_path, index))
                    removed_tokens.update(_credential_token_values(item))
                    changed = True
                    continue
                if isinstance(item, dict):
                    kept.append(item)
            if changed:
                _write_credentials_atomic(file_path, kept)

        if removed_tokens and TXT_OUTPUT.exists():
            try:
                lines = TXT_OUTPUT.read_text(encoding="utf-8").splitlines(keepends=True)
                kept_lines = [
                    line
                    for line in lines
                    if line.rstrip("\r\n").removeprefix("sso=") not in removed_tokens
                ]
                if kept_lines != lines:
                    _write_private_lines(TXT_OUTPUT, kept_lines)
            except OSError:
                pass

        files: list[tuple[Path, int, int]] = []
        for file_path in CREDENTIALS_DIR.glob("*.json"):
            try:
                stat = file_path.stat()
            except OSError:
                continue
            files.append((file_path, stat.st_mtime_ns, stat.st_size))
        valid_keys, complete = _sync_credential_files_to_db(files)
        if complete:
            account_db.reconcile_file_backed_accounts(valid_keys)
        elif matched_keys:
            account_db.hard_delete_accounts(matched_keys)
        invalidate_accounts_cache()

    return {"removed": removed_count, "matched": len(emails | user_ids)}


def _write_credential_update_to_file(file_path: Path, index: int, updated: dict) -> None:
    try:
        if not file_path.exists():
            return
        data = json.loads(file_path.read_text(encoding="utf-8"))
        is_list = isinstance(data, list)
        items = data if is_list else [data]
        if index < 0 or index >= len(items):
            return
        if not isinstance(items[index], dict):
            return
        items[index] = updated
        out = items if is_list else items[0]
        file_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return


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
    db_sig = ("grok-account-manager.db", 0, 0)
    try:
        stat = account_db.DB_PATH.stat()
        db_sig = ("grok-account-manager.db", stat.st_mtime_ns, stat.st_size)
    except OSError:
        pass

    files.sort(key=lambda item: item[1], reverse=True)
    signature = tuple((file_path.name, modified_at, size) for file_path, modified_at, size in files) + (
        test_result_sig,
        db_sig,
    )
    with _ACCOUNTS_CACHE_LOCK:
        if signature == _ACCOUNTS_CACHE_SIGNATURE:
            return [dict(account) for account in _ACCOUNTS_CACHE_DATA]

    with _CREDENTIAL_FILE_LOCK:
        valid_keys, complete = _sync_credential_files_to_db(files)
        if complete:
            account_db.reconcile_file_backed_accounts(valid_keys)
    _sync_legacy_test_results_to_db()

    test_results = account_db.list_account_test_results()
    accounts = [_merge_account_test_result(account, test_results) for account in account_db.list_accounts()]
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
    list_accounts()
    selected = [str(key or "").strip() for key in export_keys if str(key or "").strip()]
    if not selected:
        raise ValueError("请选择要导出的账号")
    exported = account_db.export_credentials(selected)
    if not exported:
        raise ValueError("没有找到可导出的账号 JSON")
    return exported


def _persist_updated_ref(ref: dict, updated: dict) -> dict:
    file_path = Path(str(ref.get("file_path") or ref.get("account", {}).get("filePath") or "account.json"))
    index = int(ref.get("item_index") or 0)
    export_key = str(ref.get("export_key") or "")
    account = account_from_credential(updated, file_path, index)
    if export_key:
        account["exportKey"] = export_key
        account_db.update_account_credential(export_key, account, updated)
    _write_credential_update_to_file(file_path, index, updated)
    invalidate_accounts_cache()
    return _merge_account_test_result(account, account_db.list_account_test_results())


def _mark_account_capability(ref: dict, updates: dict) -> None:
    export_key = str(ref.get("export_key") or "")
    if not export_key:
        return
    credential = ref.get("credential") or {}
    previous = account_db.list_account_test_results().get(export_key, {})
    now_ms = int(time.time() * 1000)
    result = {
        "email": credential.get("email") or previous.get("email") or "unknown",
        "baseAvailable": bool(previous.get("baseAvailable")),
        "chatAvailable": bool(previous.get("chatAvailable") or previous.get("baseAvailable")),
        "cli45Available": bool(previous.get("cli45Available") or previous.get("grok45Available")),
        "grok45Available": bool(previous.get("grok45Available")),
        "imageAvailable": bool(previous.get("imageAvailable")),
        "category": previous.get("category") or "unavailable",
        "baseModel": previous.get("baseModel"),
        "chatModel": previous.get("chatModel") or previous.get("baseModel"),
        "cli45Model": previous.get("cli45Model") or previous.get("grok45Model"),
        "grok45Model": previous.get("grok45Model"),
        "imageModel": previous.get("imageModel"),
        "imageSource": previous.get("imageSource"),
        "latencyMs": previous.get("latencyMs"),
        "error": None,
        "testedAt": now_ms,
        "exportKey": export_key,
        "id": str(credential.get("id") or "").strip() or Path(str(ref.get("file_path") or "account.json")).stem,
        "fileName": Path(str(ref.get("file_path") or "account.json")).name,
    }
    result.update(updates)
    if result.get("cli45Available"):
        result["category"] = "cli-4.5"
    elif result.get("chatAvailable") and result.get("imageAvailable"):
        result["category"] = "chat-image"
    elif result.get("chatAvailable") or result.get("baseAvailable"):
        result["category"] = "base-only"
    elif result.get("imageAvailable"):
        result["category"] = "image-only"
    else:
        result["category"] = "unavailable"
    result["testedAt"] = now_ms
    account_db.upsert_account_test_result(result)
    invalidate_accounts_cache()


def _is_cli_chat_model(model: str) -> bool:
    return str(model or "").strip().lower() in {"grok-4.5", "grok4.5", "grok-45"}


def _is_no_available_account_error(error: Exception) -> bool:
    text = str(error).lower()
    return "no available accounts" in text or "no available account" in text


def _relay_chat_fallback_model(model: str) -> str | None:
    normalized = str(model or "").strip().lower()
    if normalized in {
        "grok-4.20-auto",
        "grok-4.20-expert",
        "grok-4.20-0309",
        "grok-4.20-0309-reasoning",
        "grok-4.3-beta",
    }:
        return "grok-4.20-fast"
    return None


def _relay_image_fallback_model(model: str) -> str | None:
    normalized = str(model or "").strip().lower()
    if normalized in {"grok-imagine-image-quality", "grok-imagine-image-pro"}:
        return "grok-imagine-image"
    return None


def _image_error_message(error: Exception) -> str:
    text = str(error).strip() or "未知错误"
    if "internal server error" in text.lower():
        return "图片生成失败：grok2api 上游返回内部错误，请检查账号状态或代理"
    return f"图片生成失败：{text}"


def _relay_messages(messages: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip() or "user"
        if role not in {"system", "user", "assistant", "developer", "tool"}:
            role = "user"
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or ""))
                else:
                    parts.append(str(item or ""))
            text = " ".join(part.strip() for part in parts if part.strip())
        else:
            text = str(content or "").strip()
        if text:
            normalized.append({"role": role, "content": text})
    return normalized or [{"role": "user", "content": "Reply with OK."}]


def _sync_ref_to_relay(ref: dict) -> None:
    try:
        RELAY_MANAGER.sync_accounts([ref["credential"]], refresh_existing=True)
    except Exception as error:
        raise ValueError(f"同步账号到内置网关失败：{error}") from error


def _sync_project_pool_to_relay() -> dict:
    # Reconcile the file-backed local index before exporting, otherwise deleted
    # records from old credential copies can be imported into grok2api again.
    list_accounts()
    credentials = account_db.export_credentials()
    try:
        return RELAY_MANAGER.replace_accounts(credentials, pool="basic", prune_unlisted=True)
    except Exception as error:
        raise ValueError(f"同步账号池到内置网关失败：{error}") from error


def test_selected_accounts(export_keys: list[str], timeout: int = 180) -> dict:
    list_accounts()
    refs = account_db.get_credential_refs(export_keys)
    if not refs:
        raise ValueError("请选择要测试的账号")

    previous_results = account_db.list_account_test_results()
    merged_results = {key: dict(value) for key, value in previous_results.items()}
    results: list[dict] = []
    for ref in refs:
        export_key = ref["export_key"]
        credential = ref["credential"]
        file_path = Path(str(ref.get("file_path") or ref.get("account", {}).get("filePath") or "account.json"))
        index = int(ref.get("item_index") or 0)
        result, updated = test_grok_account(dict(credential), timeout=timeout)
        result["exportKey"] = export_key
        result["id"] = str(credential.get("id") or "").strip() or file_path.stem
        result["fileName"] = file_path.name
        results.append(result)
        merged_results[export_key] = result
        account_db.upsert_account_test_result(result)

        if updated != credential:
            _persist_updated_ref(ref, updated)

    _save_account_test_results(
        sorted(merged_results.values(), key=lambda item: int(item.get("testedAt") or 0), reverse=True)
    )
    invalidate_accounts_cache()
    return {"results": results, "accounts": list_accounts()}


def test_account_chat(export_key: str, model: str, messages: list[dict], timeout: int = 180) -> dict:
    list_accounts()
    refs = account_db.get_credential_refs([export_key])
    if not refs:
        raise ValueError("请选择要测试的账号")
    ref = refs[0]
    model = str(model or "").strip() or "grok-4.5"
    started = time.monotonic()

    if _is_cli_chat_model(model):
        result, updated = send_grok_chat(ref["credential"], model="grok-4.5", messages=messages, timeout=timeout)
        account_view = ref.get("account")
        if updated != ref["credential"]:
            account_view = _persist_updated_ref(ref, updated)
            ref["credential"] = updated
            ref["account"] = account_view
        _mark_account_capability(
            ref,
            {
                "baseAvailable": True,
                "chatAvailable": True,
                "cli45Available": True,
                "grok45Available": True,
                "baseModel": result["model"],
                "chatModel": result["model"],
                "cli45Model": result["model"],
                "grok45Model": result["model"],
                "latencyMs": int((time.monotonic() - started) * 1000),
            },
        )
        return {
            "model": result["model"],
            "message": {"role": "assistant", "content": result["content"]},
            "account": account_view,
            "accounts": list_accounts(),
        }

    _sync_project_pool_to_relay()
    relay_messages = _relay_messages(messages)
    fallback_from = None
    try:
        result = RELAY_MANAGER.send_chat_completion(
            model=model,
            messages=relay_messages,
            timeout=timeout,
        )
    except Exception as error:
        fallback_model = _relay_chat_fallback_model(model)
        if not fallback_model or not _is_no_available_account_error(error):
            raise
        fallback_from = model
        result = RELAY_MANAGER.send_chat_completion(
            model=fallback_model,
            messages=relay_messages,
            timeout=timeout,
        )
    _mark_account_capability(
        ref,
        {
            "baseAvailable": True,
            "chatAvailable": True,
            "baseModel": result["model"],
            "chatModel": result["model"],
            "latencyMs": int((time.monotonic() - started) * 1000),
        },
    )
    return {
        "model": result["model"],
        "fallbackFrom": fallback_from,
        "message": result["message"],
        "account": ref.get("account"),
        "accounts": list_accounts(),
    }


def test_account_image(export_key: str, model: str, prompt: str, n: int, size: str, timeout: int = 180) -> dict:
    list_accounts()
    refs = account_db.get_credential_refs([export_key])
    if not refs:
        raise ValueError("请选择要测试的账号")
    ref = refs[0]
    started = time.monotonic()
    model = str(model or "").strip() or "grok-imagine-image"
    try:
        _sync_project_pool_to_relay()
    except Exception as error:
        message = _image_error_message(error)
        _mark_account_capability(
            ref,
            {
                "imageAvailable": False,
                "imageModel": None,
                "imageSource": None,
                "error": message,
                "latencyMs": int((time.monotonic() - started) * 1000),
            },
        )
        raise ValueError(message) from error
    fallback_from = None
    try:
        result = RELAY_MANAGER.generate_image(
            model=model,
            prompt=prompt,
            n=n,
            size=size,
            timeout=timeout,
        )
    except Exception as error:
        fallback_model = _relay_image_fallback_model(model)
        if not fallback_model or not _is_no_available_account_error(error):
            message = _image_error_message(error)
            _mark_account_capability(
                ref,
                {
                    "imageAvailable": False,
                    "imageModel": None,
                    "imageSource": None,
                    "error": message,
                    "latencyMs": int((time.monotonic() - started) * 1000),
                },
            )
            raise ValueError(message) from error
        fallback_from = model
        try:
            result = RELAY_MANAGER.generate_image(
                model=fallback_model,
                prompt=prompt,
                n=n,
                size=size,
                timeout=timeout,
            )
        except Exception as fallback_error:
            message = _image_error_message(fallback_error)
            _mark_account_capability(
                ref,
                {
                    "imageAvailable": False,
                    "imageModel": None,
                    "imageSource": None,
                    "error": message,
                    "latencyMs": int((time.monotonic() - started) * 1000),
                },
            )
            raise ValueError(message) from fallback_error
    _mark_account_capability(
        ref,
        {
            "imageAvailable": True,
            "imageModel": result["model"],
            "imageSource": "grok2api-relay",
            "latencyMs": int((time.monotonic() - started) * 1000),
        },
    )
    return {
        "model": result["model"],
        "fallbackFrom": fallback_from,
        "data": result["data"],
        "account": ref.get("account"),
        "accounts": list_accounts(),
    }


def export_all_credentials() -> list[dict]:
    list_accounts()
    return account_db.export_credentials()


def delete_accounts(export_keys: list[str]) -> dict:
    refs = account_db.get_credential_refs(export_keys)
    identities = [
        {
            "email": ref.get("credential", {}).get("email"),
            "user_id": ref.get("credential", {}).get("user_id"),
        }
        for ref in refs
    ]
    result = delete_local_credentials_for_upstream_accounts(identities)
    if not refs:
        result["removed"] = account_db.hard_delete_accounts(export_keys)
        invalidate_accounts_cache()
    return {"deleted": int(result.get("removed") or 0), "accounts": list_accounts()}


def refresh_account_quota(account_id: str) -> dict:
    """Refresh one stored account quota using its current access token."""
    from ...grok.account_tester import refresh_xai_access_token
    from ...grok.client import fetch_complete_credential

    list_accounts()
    ref = account_db.find_account_ref_by_id(account_id)
    if not ref:
        raise ValueError(f"未找到 accountId={account_id} 的账号")

    item = dict(ref["credential"])
    access_token = str(item.get("access_token") or "").strip()
    refresh_token = str(item.get("refresh_token") or "").strip()
    refreshed_tokens: dict | None = None
    if refresh_token:
        try:
            refreshed_tokens = refresh_xai_access_token(
                refresh_token,
                token_endpoint=str(item.get("token_endpoint") or ""),
                timeout=30,
            )
            item.update(refreshed_tokens)
            access_token = str(item.get("access_token") or "").strip()
        except Exception as error:
            if not access_token:
                raise ValueError(f"刷新 access_token 失败：{error}") from error
    if not access_token:
        raise ValueError("该账号没有 access_token 或可用 refresh_token，无法刷新额度")
    email = str(item.get("email") or "").strip()
    updated = fetch_complete_credential(
        email=email,
        sso_token=access_token,
        profile=None,
        oauth_tokens=refreshed_tokens,
    )
    for keep_key in (
        "id",
        "created_at",
        "refresh_token",
        "id_token",
        "expires_at",
        "expires_at_raw",
        "auth_raw",
    ):
        if item.get(keep_key) is not None and updated.get(keep_key) is None:
            updated[keep_key] = item[keep_key]
    file_path = Path(str(ref.get("file_path") or ref.get("account", {}).get("filePath") or "account.json"))
    index = int(ref.get("item_index") or 0)
    export_key = str(ref.get("export_key") or "")
    account = account_from_credential(updated, file_path, index)
    if export_key:
        account["exportKey"] = export_key
        account_db.update_account_credential(export_key, account, updated)
    _write_credential_update_to_file(file_path, index, updated)
    invalidate_accounts_cache()
    return {"account": _merge_account_test_result(account, account_db.list_account_test_results())}
