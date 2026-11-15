"""注册环境与使用环境的一致性核对。

账号在注册时由浏览器产生一整套身份特征（UA、Client Hints、语言、设备 id、
屏幕、时区……）。随后网关侧用 Python HTTP 继续使用这个账号——如果两侧上报的
特征不是同一套，服务端会判定「这个凭证来自一台和注册时不同的设备」，
账号通常在注册后几分钟内被回收。

本模块把这件事变成可自动检查的规则：给定一份凭证，列出注册期与使用期之间
所有互相矛盾的地方。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"


@dataclass(frozen=True)
class ConsistencyIssue:
    dimension: str
    severity: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "dimension": self.dimension,
            "severity": self.severity,
            "message": self.message,
        }


def _text(value: Any) -> str:
    return str(value or "").strip()


def _ua_family(user_agent: str) -> str:
    agent = _text(user_agent)
    if "Edg/" in agent:
        return "edge"
    if "OPR/" in agent:
        return "opera"
    if "Chrome/" in agent:
        return "chrome"
    if "Safari/" in agent:
        return "safari"
    return "unknown"


def _ua_platform(user_agent: str) -> str:
    agent = _text(user_agent)
    if "Windows" in agent:
        return "Windows"
    if "Macintosh" in agent or "Mac OS X" in agent:
        return "macOS"
    if "Android" in agent:
        return "Android"
    if "CrOS" in agent:
        return "Chrome OS"
    if "Linux" in agent:
        return "Linux"
    return ""


def _platform_arch(platform: str) -> set[str]:
    """某个平台下常见的 Sec-CH-UA-Arch 取值。"""
    normalized = _text(platform).lower()
    if normalized in {"macos", "ios"}:
        return {"arm", "x86"}
    if normalized == "windows":
        return {"x86", "arm"}
    if normalized == "linux":
        return {"x86", "arm"}
    return {"x86", "arm"}


def check_credential_consistency(credential: dict[str, Any]) -> list[ConsistencyIssue]:
    """核对一份凭证的「注册环境」与「使用环境」是否同源。"""
    from ..providers.gpt.gateway import upstream

    issues: list[ConsistencyIssue] = []
    identity = credential.get("browser_identity")
    identity = identity if isinstance(identity, dict) else {}

    headers = upstream._base_headers(credential)

    # ---- 1. User-Agent 是否与注册时一致 ----------------------------------
    registration_ua = _text(identity.get("user_agent"))
    credential_ua = _text(credential.get("user_agent"))
    usage_ua = _text(headers.get("User-Agent"))
    if registration_ua and usage_ua and registration_ua != usage_ua:
        issues.append(
            ConsistencyIssue(
                "user-agent",
                CRITICAL,
                f"使用期的 UA 与注册期不一致：注册={registration_ua!r} / 使用={usage_ua!r}",
            )
        )
    if not registration_ua:
        issues.append(
            ConsistencyIssue(
                "user-agent",
                CRITICAL,
                "凭证里没有记录注册时的 User-Agent，无法保证使用期与注册期同源",
            )
        )

    # ---- 2. UA 家族与 Sec-CH-UA 家族必须一致 ------------------------------
    ua_family = _ua_family(usage_ua)
    sec_ch_ua = _text(headers.get("Sec-CH-UA"))
    if ua_family == "edge" and "Microsoft Edge" not in sec_ch_ua:
        issues.append(
            ConsistencyIssue(
                "sec-ch-ua",
                CRITICAL,
                f"UA 是 Edge，但 Sec-CH-UA 里没有 Microsoft Edge：{sec_ch_ua!r}",
            )
        )
    if ua_family == "chrome" and "Microsoft Edge" in sec_ch_ua:
        issues.append(
            ConsistencyIssue(
                "sec-ch-ua",
                CRITICAL,
                f"UA 是 Chrome，但 Sec-CH-UA 声明了 Microsoft Edge：{sec_ch_ua!r}",
            )
        )
    if not sec_ch_ua:
        issues.append(
            ConsistencyIssue("sec-ch-ua", CRITICAL, "缺少 Sec-CH-UA 请求头")
        )

    # ---- 3. Sec-CH-UA-Platform 必须与 UA 的操作系统一致 -------------------
    ua_platform = _ua_platform(usage_ua)
    ch_platform = _text(headers.get("Sec-CH-UA-Platform")).strip('"')
    if ua_platform and ch_platform and ua_platform != ch_platform:
        issues.append(
            ConsistencyIssue(
                "sec-ch-ua-platform",
                CRITICAL,
                f"UA 表示 {ua_platform}，但 Sec-CH-UA-Platform 声称 {ch_platform}",
            )
        )

    # ---- 4. Sec-CH-UA-Arch 必须与平台相容 ---------------------------------
    ch_arch = _text(headers.get("Sec-CH-UA-Arch")).strip('"')
    if ch_arch and ch_platform:
        allowed = _platform_arch(ch_platform)
        if ch_arch not in allowed:
            issues.append(
                ConsistencyIssue(
                    "sec-ch-ua-arch",
                    CRITICAL,
                    f"{ch_platform} 上出现了不合理架构 {ch_arch}",
                )
            )

    # ---- 5. Accept-Language 必须与 navigator.languages 一致 ----------------
    accept_language = _text(headers.get("Accept-Language"))
    navigator_languages = identity.get("navigator_languages")
    navigator_languages = (
        [_text(item) for item in navigator_languages if _text(item)]
        if isinstance(navigator_languages, list)
        else []
    )
    if navigator_languages and accept_language:
        first = accept_language.split(",")[0].split(";")[0].strip()
        if first and first != navigator_languages[0]:
            issues.append(
                ConsistencyIssue(
                    "accept-language",
                    CRITICAL,
                    f"Accept-Language 首选 {first!r}，但注册时 navigator.languages[0] 是 "
                    f"{navigator_languages[0]!r}",
                )
            )
    if not accept_language:
        issues.append(
            ConsistencyIssue("accept-language", WARNING, "缺少 Accept-Language 请求头")
        )

    # ---- 6. OAI-Language 必须与 Accept-Language 同源 ----------------------
    oai_language = _text(headers.get("OAI-Language"))
    if oai_language and navigator_languages and oai_language != navigator_languages[0]:
        issues.append(
            ConsistencyIssue(
                "oai-language",
                WARNING,
                f"OAI-Language={oai_language!r} 与注册语言 {navigator_languages[0]!r} 不一致",
            )
        )

    # ---- 7. Accept 请求头必须存在 -----------------------------------------
    if not _text(headers.get("Accept")):
        issues.append(
            ConsistencyIssue("accept", WARNING, "缺少 Accept 请求头（真实浏览器一定会带）")
        )

    # ---- 8. 设备 id 必须跨请求稳定 ----------------------------------------
    device_id = _text(headers.get("OAI-Device-Id"))
    if not device_id:
        issues.append(ConsistencyIssue("oai-device-id", CRITICAL, "缺少 OAI-Device-Id"))
    else:
        # 用一份「全新的」凭证字典重新计算，验证派生逻辑本身是稳定的，
        # 而不是靠调用方缓存。
        fresh = dict(credential)
        fresh.pop("oai_device_id", None)
        fresh.pop("device_id", None)
        second = _text(upstream._base_headers(fresh).get("OAI-Device-Id"))
        if second != device_id:
            issues.append(
                ConsistencyIssue(
                    "oai-device-id",
                    CRITICAL,
                    f"设备 id 不稳定：{device_id!r} -> {second!r}（每次请求换设备等于自曝）",
                )
            )
        if len(device_id) != 36:
            issues.append(
                ConsistencyIssue(
                    "oai-device-id",
                    WARNING,
                    f"设备 id 长度 {len(device_id)}，与浏览器下发的 UUID 格式不符",
                )
            )

    # ---- 9. 会话 id 必须跨请求稳定 ----------------------------------------
    session_id = _text(headers.get("OAI-Session-Id"))
    fresh = dict(credential)
    fresh.pop("oai_session_id", None)
    fresh.pop("session_id", None)
    second_session = _text(upstream._base_headers(fresh).get("OAI-Session-Id"))
    if session_id and second_session and session_id != second_session:
        issues.append(
            ConsistencyIssue(
                "oai-session-id",
                CRITICAL,
                f"会话 id 不稳定：{session_id!r} -> {second_session!r}",
            )
        )

    return issues


def summarize_credential(credential: dict[str, Any]) -> dict[str, Any]:
    """汇总一份凭证的注册环境特征，便于人工核对。"""
    from ..providers.gpt.gateway import upstream

    identity = credential.get("browser_identity")
    identity = identity if isinstance(identity, dict) else {}
    headers = upstream._base_headers(credential)
    screen = identity.get("screen") if isinstance(identity.get("screen"), dict) else {}
    return {
        "注册 UA": _text(headers.get("User-Agent")),
        "注册 UA 家族": _ua_family(_text(headers.get("User-Agent"))),
        "Sec-CH-UA": _text(headers.get("Sec-CH-UA")),
        "Sec-CH-UA-Platform": _text(headers.get("Sec-CH-UA-Platform")),
        "Sec-CH-UA-Arch": _text(headers.get("Sec-CH-UA-Arch")),
        "Sec-CH-UA-Bitness": _text(headers.get("Sec-CH-UA-Bitness")),
        "Accept-Language": _text(headers.get("Accept-Language")),
        "OAI-Language": _text(headers.get("OAI-Language")),
        "OAI-Device-Id": _text(headers.get("OAI-Device-Id")),
        "OAI-Session-Id": _text(headers.get("OAI-Session-Id")),
        "时区": _text(identity.get("timezone")),
        "屏幕": f"{screen.get('width', '?')}x{screen.get('height', '?')}",
        "CPU 核数": identity.get("hardware_concurrency"),
        "内存 GB": identity.get("device_memory"),
        "插件数": len(identity.get("plugins") or []),
        "webdriver": identity.get("webdriver"),
    }


__all__ = [
    "CRITICAL",
    "ConsistencyIssue",
    "INFO",
    "WARNING",
    "check_credential_consistency",
    "summarize_credential",
]
