"""Account list, tags, export, quota, and test endpoints."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ...sinks.cpa_credential import build_cpa_download
from ..schemas import (
    AccountChatTestRequest,
    AccountImageTestRequest,
    AccountTagsRequest,
    AccountVideoStatusRequest,
    AccountVideoTestRequest,
    DeleteAccountsRequest,
    ExportRequest,
    ImportAccountsRequest,
    RefreshQuotaRequest,
    TestBatchRequest,
)
from ..services import accounts as account_service
from ...core import audit
from ..services.accounts import (
    delete_accounts,
    export_credentials,
    import_accounts,
    refresh_account_quota,
    test_account_chat,
    test_account_image,
    test_account_video,
    test_account_video_status,
    test_selected_accounts,
)
from ...core.utils import safe_int

router = APIRouter(tags=["accounts"])


def _download(raw: bytes, filename: str, content_type: str, status_code: int = 200) -> Response:
    return Response(
        content=raw,
        status_code=status_code,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/accounts")
def list_accounts(provider: str = "", search: str = "", tag: str = "") -> dict:
    items = account_service.list_accounts()
    provider_value = str(provider or "").strip().lower()
    query = str(search or "").strip().lower()
    tag_value = str(tag or "").strip()
    if provider_value:
        items = [item for item in items if str(item.get("provider") or "").lower() == provider_value]
    if query:
        items = [
            item
            for item in items
            if query
            in " ".join(
                str(item.get(key) or "")
                for key in ("email", "displayName", "id", "exportKey", "userId")
            ).lower()
        ]
    if tag_value:
        items = [item for item in items if tag_value in (item.get("tags") or [])]
    return {"accounts": items, "total": len(items)}


@router.get("/api/accounts/tags")
def list_account_tags() -> dict:
    return {"items": account_service.list_account_tags()}


@router.put("/api/accounts/tags")
def update_account_tags(body: AccountTagsRequest, request: Request) -> dict:
    mode = str(body.mode or "replace").strip().lower()
    if mode not in {"replace", "add", "remove"}:
        raise HTTPException(status_code=400, detail="mode 必须是 replace/add/remove")
    try:
        result = account_service.update_account_tags(body.exportKeys, body.tags, mode)
    except Exception as error:
        audit.log_operation(
            audit.actor_from_request(request),
            "account.tags.update",
            target_type="account",
            target_id=",".join(body.exportKeys[:20]),
            detail={"tags": body.tags, "mode": mode},
            result=f"failed: {error}",
        )
        raise HTTPException(status_code=400, detail=str(error)) from error
    audit.log_operation(
        audit.actor_from_request(request),
        "account.tags.update",
        target_type="account",
        target_id=",".join(body.exportKeys[:20]),
        detail={"tags": body.tags, "mode": mode, "updated": result.get("updated", 0)},
    )
    return {**result, "items": account_service.list_account_tags()}


@router.post("/api/accounts/import")
def import_selected_accounts(body: ImportAccountsRequest, request: Request) -> dict:
    try:
        result = import_accounts(body.provider, body.data)
        audit.log_operation(
            audit.actor_from_request(request),
            "account.import",
            target_type="account",
            target_id=str(body.provider or ""),
            detail={"imported": result.get("imported"), "skipped": result.get("skipped")},
        )
        return result
    except Exception as error:
        audit.log_operation(
            audit.actor_from_request(request),
            "account.import",
            target_type="account",
            target_id=str(body.provider or ""),
            result=f"failed: {error}",
        )
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/api/accounts/{export_key:path}/credential")
def get_account_credential(export_key: str, request: Request) -> dict:
    try:
        result = account_service.get_account_credential(export_key)
        audit.log_operation(
            audit.actor_from_request(request),
            "account.credential.read",
            target_type="account",
            target_id=export_key,
            detail={"provider": result.get("provider")},
        )
        return JSONResponse(
            content=result,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    except Exception as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/api/accounts/export")
def export_selected_accounts(body: ExportRequest, request: Request) -> Response:
    try:
        accounts = export_credentials(body.exportKeys)
        raw = json.dumps(accounts, ensure_ascii=False, indent=2).encode("utf-8")
        filename = f"msdsj-grok-credentials-{time.strftime('%Y%m%d-%H%M%S')}.json"
        audit.log_operation(
            audit.actor_from_request(request),
            "account.export",
            target_type="account",
            target_id=",".join(body.exportKeys[:20]),
            detail={"count": len(accounts), "format": "json"},
        )
        return _download(raw, filename, "application/json; charset=utf-8")
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/accounts/export-cpa")
def export_selected_cpa_accounts(body: ExportRequest, request: Request) -> Response:
    try:
        accounts = export_credentials(body.exportKeys)
        raw, filename, content_type = build_cpa_download(accounts)
        audit.log_operation(
            audit.actor_from_request(request),
            "account.export",
            target_type="account",
            target_id=",".join(body.exportKeys[:20]),
            detail={"count": len(accounts), "format": "cpa"},
        )
        return _download(raw, filename, content_type)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/accounts/delete")
def delete_selected_accounts(body: DeleteAccountsRequest, request: Request) -> dict:
    try:
        result = delete_accounts(body.exportKeys)
        audit.log_operation(
            audit.actor_from_request(request),
            "account.delete",
            target_type="account",
            target_id=",".join(body.exportKeys[:20]),
            detail={"deleted": result.get("deleted"), "count": len(body.exportKeys)},
        )
        return result
    except Exception as error:
        audit.log_operation(
            audit.actor_from_request(request),
            "account.delete",
            target_type="account",
            target_id=",".join(body.exportKeys[:20]),
            result=f"failed: {error}",
        )
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/accounts/refresh-quota")
def refresh_quota(body: RefreshQuotaRequest, request: Request) -> dict:
    account_id = str(body.accountId or "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="accountId 必填")
    try:
        result = refresh_account_quota(account_id)
        audit.log_operation(
            audit.actor_from_request(request),
            "account.quota.refresh",
            target_type="account",
            target_id=account_id,
        )
        return result
    except Exception as error:
        audit.log_operation(
            audit.actor_from_request(request),
            "account.quota.refresh",
            target_type="account",
            target_id=account_id,
            result=f"failed: {error}",
        )
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/accounts/test-batch")
def test_accounts(body: TestBatchRequest, request: Request) -> dict:
    timeout = safe_int(body.timeout, default=180, minimum=5, maximum=180)
    try:
        result = test_selected_accounts(body.exportKeys, timeout=timeout)
        audit.log_operation(
            audit.actor_from_request(request),
            "account.test.batch",
            target_type="account",
            target_id=",".join(body.exportKeys[:20]),
            detail={"count": len(body.exportKeys), "timeout": timeout},
        )
        return result
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/accounts/chat-test")
def test_chat(body: AccountChatTestRequest, request: Request) -> dict:
    timeout = safe_int(body.timeout, default=180, minimum=5, maximum=180)
    try:
        result = test_account_chat(
            export_key=body.exportKey,
            model=body.model,
            messages=body.messages,
            timeout=timeout,
        )
        audit.log_operation(
            audit.actor_from_request(request),
            "account.test.chat",
            target_type="account",
            target_id=str(body.exportKey or ""),
            detail={"model": body.model, "timeout": timeout},
        )
        return result
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/accounts/image-test")
def test_image(body: AccountImageTestRequest, request: Request) -> dict:
    timeout = safe_int(body.timeout, default=180, minimum=5, maximum=180)
    count = safe_int(body.n, default=1, minimum=1, maximum=4)
    try:
        result = test_account_image(
            export_key=body.exportKey,
            model=body.model,
            prompt=body.prompt,
            n=count,
            size=body.size,
            timeout=timeout,
        )
        audit.log_operation(
            audit.actor_from_request(request),
            "account.test.image",
            target_type="account",
            target_id=str(body.exportKey or ""),
            detail={"model": body.model, "count": count, "timeout": timeout},
        )
        return result
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/accounts/video-test")
def test_video(body: AccountVideoTestRequest, request: Request) -> dict:
    timeout = safe_int(body.timeout, default=180, minimum=5, maximum=180)
    duration = safe_int(body.duration, default=6, minimum=1, maximum=30)
    try:
        result = test_account_video(
            export_key=body.exportKey,
            model=body.model,
            prompt=body.prompt,
            duration=duration,
            resolution=body.resolution,
            aspect_ratio=body.aspectRatio,
            timeout=timeout,
        )
        audit.log_operation(
            audit.actor_from_request(request),
            "account.test.video",
            target_type="account",
            target_id=str(body.exportKey or ""),
            detail={"model": body.model, "duration": duration, "timeout": timeout},
        )
        return result
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/accounts/video-status")
def video_status(body: AccountVideoStatusRequest, request: Request) -> dict:
    timeout = safe_int(body.timeout, default=180, minimum=5, maximum=180)
    try:
        return test_account_video_status(
            export_key=body.exportKey,
            video_id=body.videoId,
            timeout=timeout,
        )
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
