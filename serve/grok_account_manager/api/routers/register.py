"""Registration task endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..schemas import (
    MailConfigRequest,
    RegisterRequest,
    RegistrationProxyImportRequest,
)
from ...core import audit
from ..services.jobs import JOB_MANAGER
from ..services.mail_config import get_mail_config, save_mail_config
from ..services.registration_proxy_pool import (
    clear_saved_registration_proxies,
    registration_proxy_pool_snapshot,
    save_registration_proxy_nodes,
)

router = APIRouter(tags=["registration"])


@router.get("/api/register/mail-config")
def read_mail_config() -> dict:
    try:
        return get_mail_config()
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.put("/api/register/mail-config")
def write_mail_config(body: MailConfigRequest, request: Request) -> dict:
    try:
        result = save_mail_config(body.model_dump())
        audit.log_operation(
            audit.actor_from_request(request),
            "mail.config.update",
            target_type="config",
            target_id="mail",
            detail={"emailSource": body.emailSource},
        )
        return result
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/api/register/proxies")
def get_registration_proxies() -> dict:
    try:
        return registration_proxy_pool_snapshot()
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.put("/api/register/proxies")
def import_registration_proxies(body: RegistrationProxyImportRequest, request: Request) -> dict:
    try:
        result = save_registration_proxy_nodes(body.data, replace=body.replace)
        audit.log_operation(
            audit.actor_from_request(request),
            "proxy_pool.import",
            target_type="proxy_pool",
            detail={"replace": bool(body.replace), "count": result.get("count") if isinstance(result, dict) else None},
        )
        return result
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/api/register/proxies")
def clear_registration_proxies(request: Request) -> dict:
    try:
        result = clear_saved_registration_proxies()
        audit.log_operation(
            audit.actor_from_request(request),
            "proxy_pool.clear",
            target_type="proxy_pool",
            detail={"removed": result.get("removed") if isinstance(result, dict) else None},
        )
        return result
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/register")
def start_registration(body: RegisterRequest, request: Request) -> dict:
    try:
        job = JOB_MANAGER.start(
            provider=body.provider,
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
            duck_mail_api_base=body.duckMailApiBase,
            duck_mail_api_key=body.duckMailApiKey,
            duck_mail_domain=body.duckMailDomain,
            cloud_mail_api_base=body.cloudMailApiBase,
            cloud_mail_auth_mode=body.cloudMailAuthMode,
            cloud_mail_public_token=body.cloudMailPublicToken,
            cloud_mail_login_email=body.cloudMailLoginEmail,
            cloud_mail_login_password=body.cloudMailLoginPassword,
            cloud_mail_domains=body.cloudMailDomains,
            proxy_pool_enabled=body.proxyPoolEnabled,
            proxy_data=body.proxyData,
            proxy_file=body.proxyFile,
        )
        audit.log_operation(
            audit.actor_from_request(request),
            "registration.start",
            target_type="job",
            target_id=str((job or {}).get("id") or ""),
            detail={
                "provider": body.provider,
                "total": body.total,
                "concurrency": body.concurrency,
                "emailSource": body.emailSource,
            },
        )
        return {"job": job}
    except Exception as error:
        audit.log_operation(
            audit.actor_from_request(request),
            "registration.start",
            target_type="job",
            detail={"provider": body.provider, "total": body.total},
            result=f"failed: {error}",
        )
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/register/stop")
def stop_registration(request: Request) -> dict:
    result = JOB_MANAGER.stop()
    audit.log_operation(
        audit.actor_from_request(request),
        "registration.stop",
        target_type="job",
        target_id=str((result or {}).get("id") or ""),
    )
    return {"job": result}


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
def retry_registration(request: Request) -> dict:
    """Re-run a single registration round using the last task's config."""
    try:
        job = JOB_MANAGER.retry()
        audit.log_operation(
            audit.actor_from_request(request),
            "registration.retry",
            target_type="job",
            target_id=str((job or {}).get("id") or ""),
        )
        return {"job": job}
    except Exception as error:
        audit.log_operation(
            audit.actor_from_request(request),
            "registration.retry",
            target_type="job",
            result=f"failed: {error}",
        )
        raise HTTPException(status_code=400, detail=str(error)) from error
