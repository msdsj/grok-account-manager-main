"""Persistent log query endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ...core import database as db

router = APIRouter(tags=["logs"])


@router.get("/api/logs/operations")
def list_operation_logs(
    actor: str = "",
    action: str = "",
    result: str = "",
    startAt: int = 0,
    endAt: int = 0,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return db.list_operation_logs(
        actor=actor,
        action=action,
        result=result,
        start_at=startAt,
        end_at=endAt,
        limit=limit,
        offset=offset,
    )


@router.get("/api/logs/jobs/{job_id}")
def list_job_logs(
    job_id: str,
    limit: int = Query(default=500, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return db.list_job_logs(job_id, limit=limit, offset=offset)


@router.get("/api/logs/system")
def list_system_logs(
    level: str = "",
    source: str = "",
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return db.list_system_logs(level=level, source=source, limit=limit, offset=offset)


@router.get("/api/logs/job-runs")
def list_job_runs(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return db.list_job_runs(limit=limit, offset=offset)
