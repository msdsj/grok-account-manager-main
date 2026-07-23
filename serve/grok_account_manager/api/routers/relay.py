"""Relay management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..schemas import RelayConfigRequest, RelayModelsRequest, RelaySyncRequest
from ..services.accounts import export_all_credentials, export_credentials
from ..services.relay import RELAY_MANAGER

router = APIRouter(tags=["relay"])


@router.get("/api/relay")
def get_relay() -> dict:
    return {"relay": RELAY_MANAGER.snapshot()}


@router.post("/api/relay/config")
def update_relay_config(body: RelayConfigRequest) -> dict:
    try:
        return {"relay": RELAY_MANAGER.update_config(body.to_patch())}
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/relay/start")
def start_relay() -> dict:
    try:
        return {"relay": RELAY_MANAGER.start()}
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/relay/stop")
def stop_relay() -> dict:
    return {"relay": RELAY_MANAGER.stop()}


@router.post("/api/relay/sync-accounts")
def sync_relay_accounts(body: RelaySyncRequest) -> dict:
    try:
        credentials = export_credentials(body.exportKeys) if body.exportKeys else export_all_credentials()
        result = RELAY_MANAGER.replace_accounts(credentials, pool="basic", prune_unlisted=True)
        return {"sync": result, "relay": RELAY_MANAGER.snapshot()}
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/relay/models")
def probe_relay_models(body: RelayModelsRequest) -> dict:
    try:
        result = RELAY_MANAGER.probe_models(probe_chat=body.probeChat)
        return {"result": result, "relay": RELAY_MANAGER.snapshot()}
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
