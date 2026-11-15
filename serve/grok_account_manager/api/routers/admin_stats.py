"""仪表盘统计：用数据库里的账号、模型与网关请求生成概览数据。"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from ...gateway import catalog
from ...core import database as db

router = APIRouter(tags=["admin-stats"])

_PERIODS = {
    "24h": (24 * 3600, 3600),
    "7d": (7 * 24 * 3600, 24 * 3600),
    "30d": (30 * 24 * 3600, 24 * 3600),
    "90d": (90 * 24 * 3600, 24 * 3600),
}


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@router.get("/api/admin/v1/dashboard")
def dashboard(request: Request, period: str = "24h", timezone: str = "") -> dict:
    _ = timezone
    window, bucket = _PERIODS.get(str(period or "24h"), _PERIODS["24h"])
    now = time.time()
    start_at = now - window
    start_ms = int(start_at * 1000)
    rows = db.aggregate_gateway_requests(start_at=start_ms, bucket_seconds=bucket)

    accounts = db.list_accounts()
    total_accounts = len(accounts)
    active_accounts = sum(
        1 for account in accounts if account.get("hasAccessToken") or account.get("hasRefreshToken")
    )
    build_accounts = active_accounts
    web_accounts = sum(1 for account in accounts if account.get("webAvailable"))
    console_accounts = 0

    models = catalog.list_models(enabled_only=False)
    keys = db.list_client_keys()

    requests_total = 0
    success_total = 0
    prompt_total = 0
    completion_total = 0
    bucket_rows: dict[int, dict] = {}
    model_rows: dict[str, dict] = {}
    provider_rows: dict[str, dict] = {}

    for row in rows:
        count = int(row["requests"])
        prompt = int(row["promptTokens"])
        completion = int(row["completionTokens"])
        requests_total += count
        prompt_total += prompt
        completion_total += completion
        if row["status"] == "success":
            success_total += count

        bucket_entry = bucket_rows.setdefault(
            int(row["bucket"]),
            {"start": int(row["bucket"]), "requests": 0, "inputTokens": 0, "outputTokens": 0},
        )
        bucket_entry["requests"] += count
        bucket_entry["inputTokens"] += prompt
        bucket_entry["outputTokens"] += completion

        model = str(row["model"] or "unknown")
        model_entry = model_rows.setdefault(
            model,
            {"model": model, "requests": 0, "inputTokens": 0, "outputTokens": 0},
        )
        model_entry["requests"] += count
        model_entry["inputTokens"] += prompt
        model_entry["outputTokens"] += completion

        provider = str(row["provider"] or "unknown")
        provider_entry = provider_rows.setdefault(
            provider,
            {"provider": provider, "requests": 0, "successfulRequests": 0, "tokens": 0},
        )
        provider_entry["requests"] += count
        provider_entry["tokens"] += prompt + completion
        if row["status"] == "success":
            provider_entry["successfulRequests"] += count

    bucket_ms = bucket * 1000
    first_bucket = (start_ms // bucket_ms) * bucket_ms
    series: list[dict] = []
    cursor = first_bucket
    while cursor <= int(now * 1000):
        entry = bucket_rows.get(cursor, {"requests": 0, "inputTokens": 0, "outputTokens": 0})
        series.append(
            {
                "start": _iso(cursor / 1000),
                "end": _iso((cursor + bucket_ms) / 1000),
                "requests": int(entry["requests"]),
                "inputTokens": int(entry["inputTokens"]),
                "cachedInputTokens": 0,
                "outputTokens": int(entry["outputTokens"]),
                "reasoningTokens": 0,
                "tokens": int(entry["inputTokens"]) + int(entry["outputTokens"]),
                "billedCostUsdTicks": 0,
            }
        )
        cursor += bucket_ms

    failed_total = max(0, requests_total - success_total)
    return {
        "data": {
            "period": period if period in _PERIODS else "24h",
            "generatedAt": _iso(now),
            "range": {"start": _iso(start_at), "end": _iso(now)},
            "resources": {
                "activeAccounts": active_accounts,
                "totalAccounts": total_accounts,
                "buildAccounts": build_accounts,
                "webAccounts": web_accounts,
                "consoleAccounts": console_accounts,
                "enabledModels": sum(1 for entry in models if entry.get("enabled", True)),
                "totalModels": len(models),
                "activeClientKeys": sum(1 for key in keys if key.get("enabled")),
                "totalClientKeys": len(keys),
            },
            "usage": {
                "requests": requests_total,
                "successfulRequests": success_total,
                "failedRequests": failed_total,
                "inputTokens": prompt_total,
                "cachedInputTokens": 0,
                "outputTokens": completion_total,
                "reasoningTokens": 0,
                "tokens": prompt_total + completion_total,
                "billedCostUsdTicks": 0,
                "successRate": round(success_total / requests_total, 4) if requests_total else 0,
            },
            "series": series,
            "activity": [{"start": item["start"], "requests": item["requests"]} for item in series],
            "topModels": sorted(
                (
                    {
                        "model": item["model"],
                        "requests": item["requests"],
                        "inputTokens": item["inputTokens"],
                        "cachedInputTokens": 0,
                        "outputTokens": item["outputTokens"],
                        "reasoningTokens": 0,
                        "tokens": item["inputTokens"] + item["outputTokens"],
                        "billedCostUsdTicks": 0,
                    }
                    for item in model_rows.values()
                ),
                key=lambda item: item["requests"],
                reverse=True,
            )[:8],
            "providers": sorted(provider_rows.values(), key=lambda item: item["requests"], reverse=True),
        }
    }
