"""Registration task endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..schemas import RegisterRequest
from ..services.jobs import JOB_MANAGER

router = APIRouter(tags=["registration"])


@router.post("/api/register")
def start_registration(body: RegisterRequest) -> dict:
    try:
        job = JOB_MANAGER.start(
            total=body.total,
            concurrency=body.concurrency,
            oauth_exchange=body.oauthExchange,
            email_source=body.emailSource,
            outlook_data=body.outlookData,
            outlook_accounts_file=body.outlookAccountsFile,
            google_data=body.googleData,
            google_accounts_file=body.googleAccountsFile,
        )
        return {"job": job}
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/register/stop")
def stop_registration() -> dict:
    return {"job": JOB_MANAGER.stop()}


@router.post("/api/register/retry")
def retry_registration() -> dict:
    """Re-run a single registration round using the last task's config."""
    try:
        job = JOB_MANAGER.retry()
        return {"job": job}
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
