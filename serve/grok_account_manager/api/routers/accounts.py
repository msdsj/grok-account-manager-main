"""Account export, quota, and test endpoints."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ...sinks.cpa_credential import build_cpa_download
from ..schemas import (
    AccountChatTestRequest,
    AccountImageTestRequest,
    DeleteAccountsRequest,
    ExportRequest,
    RefreshQuotaRequest,
    TestBatchRequest,
)
from ..services.accounts import (
    delete_accounts,
    export_credentials,
    refresh_account_quota,
    test_account_chat,
    test_account_image,
    test_selected_accounts,
)
from ..utils import safe_int

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


@router.post("/api/accounts/export")
def export_selected_accounts(body: ExportRequest) -> Response:
    try:
        accounts = export_credentials(body.exportKeys)
        raw = json.dumps(accounts, ensure_ascii=False, indent=2).encode("utf-8")
        filename = f"msdsj-grok-credentials-{time.strftime('%Y%m%d-%H%M%S')}.json"
        return _download(raw, filename, "application/json; charset=utf-8")
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/accounts/export-cpa")
def export_selected_cpa_accounts(body: ExportRequest) -> Response:
    try:
        accounts = export_credentials(body.exportKeys)
        raw, filename, content_type = build_cpa_download(accounts)
        return _download(raw, filename, content_type)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/accounts/delete")
def delete_selected_accounts(body: DeleteAccountsRequest) -> dict:
    try:
        return delete_accounts(body.exportKeys)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/accounts/refresh-quota")
def refresh_quota(body: RefreshQuotaRequest) -> dict:
    account_id = str(body.accountId or "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="accountId 必填")
    try:
        return refresh_account_quota(account_id)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/accounts/test-batch")
def test_accounts(body: TestBatchRequest) -> dict:
    timeout = safe_int(body.timeout, default=120, minimum=5, maximum=120)
    try:
        return test_selected_accounts(body.exportKeys, timeout=timeout)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/accounts/chat-test")
def test_chat(body: AccountChatTestRequest) -> dict:
    timeout = safe_int(body.timeout, default=120, minimum=5, maximum=120)
    try:
        return test_account_chat(
            export_key=body.exportKey,
            model=body.model,
            messages=body.messages,
            timeout=timeout,
        )
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/accounts/image-test")
def test_image(body: AccountImageTestRequest) -> dict:
    timeout = safe_int(body.timeout, default=120, minimum=5, maximum=120)
    count = safe_int(body.n, default=1, minimum=1, maximum=4)
    try:
        return test_account_image(
            export_key=body.exportKey,
            model=body.model,
            prompt=body.prompt,
            n=count,
            size=body.size,
            timeout=timeout,
        )
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
