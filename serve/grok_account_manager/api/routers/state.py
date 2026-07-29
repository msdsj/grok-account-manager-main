"""State and account-list endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from ..services.accounts import list_accounts
from ..services.jobs import JOB_MANAGER
from ..services.relay import RELAY_MANAGER

router = APIRouter(tags=["state"])


@router.get("/api/accounts")
def get_accounts() -> dict:
    return {"accounts": list_accounts()}


@router.get("/api/state")
def get_state() -> dict:
    return {
        "job": JOB_MANAGER.snapshot(),
        "accounts": list_accounts(),
        "relay": RELAY_MANAGER.snapshot(),
    }


@router.get("/api/jobs/current")
def get_current_job() -> dict:
    return {"job": JOB_MANAGER.snapshot()}
