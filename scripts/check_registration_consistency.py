#!/usr/bin/env python3
"""账号池一致性体检。

读取账号库里的 ChatGPT 账号，逐个核对「注册时浏览器」与「网关侧使用账号时
上报的请求头」是否同源，并列出每一个矛盾点。

修复前后的对比就靠它：老账号会报出「注册环境未记录 / 设备 id 每次都换 /
UA 与 Sec-CH-UA 打架」之类的严重问题；用修复后的流程重新注册的账号则应当
全部通过。

用法::

    uv run python scripts/check_registration_consistency.py
    uv run python scripts/check_registration_consistency.py --json
    uv run python scripts/check_registration_consistency.py --provider chatgpt
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "serve"))

from grok_account_manager.core import database as db  # noqa: E402
from grok_account_manager.core.consistency import (  # noqa: E402
    CRITICAL,
    WARNING,
    check_credential_consistency,
    summarize_credential,
)


def collect(provider: str) -> list[dict]:
    """读取账号库里的凭证。"""
    accounts = []
    for ref in db.list_credential_refs(provider):
        credential = ref.get("credential")
        if not isinstance(credential, dict):
            continue
        accounts.append(
            {
                "export_key": str(ref.get("export_key") or ""),
                "email": str(ref.get("email") or credential.get("email") or ""),
                "provider": str(ref.get("provider") or credential.get("provider") or ""),
                "credential": credential,
            }
        )
    return accounts


def main() -> int:
    parser = argparse.ArgumentParser(description="账号池注册/使用一致性体检")
    parser.add_argument("--provider", default="chatgpt", help="要检查的 provider")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--details", action="store_true", help="逐账号展开明细")
    args = parser.parse_args()

    accounts = collect(args.provider)

    reports = []
    total_critical = 0
    total_warning = 0
    for account in accounts:
        issues = check_credential_consistency(account["credential"])
        critical = [item for item in issues if item.severity == CRITICAL]
        warning = [item for item in issues if item.severity == WARNING]
        total_critical += len(critical)
        total_warning += len(warning)
        reports.append(
            {
                "export_key": account["export_key"],
                "email": account["email"],
                "issues": [item.as_dict() for item in issues],
                "summary": summarize_credential(account["credential"]),
            }
        )

    payload = {
        "provider": args.provider,
        "account_count": len(accounts),
        "critical_count": total_critical,
        "warning_count": total_warning,
        "reports": reports,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 1 if total_critical else 0

    print()
    print("=" * 78)
    print(f"账号池一致性体检（provider={args.provider}，共 {len(accounts)} 个账号）")
    print("=" *78)
    if not accounts:
        print("  账号库里没有该 provider 的账号。")
        return 0

    for report in reports:
        critical = [i for i in report["issues"] if i["severity"] == CRITICAL]
        warning = [i for i in report["issues"] if i["severity"] == WARNING]
        mark = "❌" if critical else ("⚠️ " if warning else "✅")
        print(f"\n{mark} {report['email'] or report['export_key']}")
        if args.details or critical or warning:
            for key, value in report["summary"].items():
                print(f"      {key:22s} {value}")
        for issue in critical + warning:
            flag = "严重" if issue["severity"] == CRITICAL else "提示"
            print(f"      [{flag}] {issue['dimension']}: {issue['message']}")
        if not critical and not warning:
            print("      注册环境与使用环境完全同源。")

    print()
    print("-" * 78)
    print(f"合计：{len(accounts)} 个账号 · 严重问题 {total_critical} 个 · 提示 {total_warning} 个")
    if total_critical:
        print("严重问题意味着：账号在使用时上报的浏览器特征和注册时不是同一台，")
        print("服务端会把它判定为异常凭证并回收。用修复后的流程重新注册即可。")
    print("-" * 78)
    return 1 if total_critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
