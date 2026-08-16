"""Registration task endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..schemas import RegisterRequest, RegistrationProxyImportRequest
from ..services.jobs import JOB_MANAGER
from ..services.registration_proxy_pool import (
    clear_saved_registration_proxies,
    registration_proxy_pool_snapshot,
    save_registration_proxy_nodes,
)

router = APIRouter(tags=["registration"])


@router.get("/api/register/proxies")
def get_registration_proxies() -> dict:
    try:
        return registration_proxy_pool_snapshot()
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.put("/api/register/proxies")
def import_registration_proxies(body: RegistrationProxyImportRequest) -> dict:
    try:
        return save_registration_proxy_nodes(body.data, replace=body.replace)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/api/register/proxies")
def clear_registration_proxies() -> dict:
    try:
        return clear_saved_registration_proxies()
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/register")
def start_registration(body: RegisterRequest) -> dict:
    try:
        job = JOB_MANAGER.start(
            total=body.total,
            concurrency=body.concurrency,
            oauth_exchange=body.oauthExchange,
            auto_import_sub2api=body.autoImportSub2Api,
            minimize_browsers=body.minimizeBrowsers,
            email_source=body.emailSource,
            outlook_data=body.outlookData,
            outlook_accounts_file=body.outlookAccountsFile,
            google_data=body.googleData,
            google_accounts_file=body.googleAccountsFile,
            cloud_mail_api_base=body.cloudMailApiBase,
            cloud_mail_public_token=body.cloudMailPublicToken,
            cloud_mail_login_email=body.cloudMailLoginEmail,
            cloud_mail_login_password=body.cloudMailLoginPassword,
            cloud_mail_domains=body.cloudMailDomains,
            proxy_pool_enabled=body.proxyPoolEnabled,
            proxy_data=body.proxyData,
            proxy_file=body.proxyFile,
        )
        return {"job": job}
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/register/stop")
def stop_registration() -> dict:
    return {"job": JOB_MANAGER.stop()}


@router.post("/api/register/windows/minimize")
def minimize_registration_windows() -> dict:
    try:
        return JOB_MANAGER.set_windows_minimized(True)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/register/windows/restore")
def restore_registration_windows() -> dict:
    try:
        return JOB_MANAGER.set_windows_minimized(False)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/register/retry")
def retry_registration() -> dict:
    """Re-run a single registration round using the last task's config."""
    try:
        job = JOB_MANAGER.retry()
        return {"job": job}
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
