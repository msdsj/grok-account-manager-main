"""采集运行中 Chromium 会话的「浏览器身份快照」。

注册期的风控信号全部由浏览器本体产生：User-Agent、Client Hints、UA-CH 高熵字段、
语言、屏幕、插件列表、时区、device id……如果后续 Python HTTP 侧（网关 / PoW /
sentinel）用一套写死的值去续用这个账号，服务端看到的就是「注册用 macOS 上的
Chrome、使用用 Windows 上的 Edge」这种自相矛盾合——这正是账号在注册后几分钟内
被判定为异常并回收的直接原因。

本模块把浏览器侧的真实值一次性采集下来：

1. 注册流程把它写进凭证（``full_credential["browser_identity"]``）；
2. 网关层用 :func:`identity_headers` 原样回放，保证「注册环境」与「使用环境」严格同源。

采集只读不写，不修改页面状态，也不需要额外的 CDP 权限。
"""

from __future__ import annotations

import re
import time
from typing import Any

# 真实 Chrome 在 https 请求上固定发送的 Accept-CH 值。
# 服务端若回 Opt-In（Accept-CH 响应头），Chrome 才会附带对应的高熵提示符。
CHROME_ACCEPT_CH = (
    "Sec-CH-UA-Platform-Version, Sec-CH-UA-Model, Sec-CH-UA-Full-Version-List, "
    "Sec-CH-UA-WoW64, Sec-CH-UA-Bitness, Sec-CH-UA-Arch"
)

# oai-did 是 chatgpt.com 下发的设备标识 cookie，也是 OAI-Device-Id 请求头的来源。
DEVICE_ID_COOKIE_NAMES = ("oai-did", "oai_did", "oai-device-id")

_VERSION_RE = re.compile(r"Chrome/(\d+)")


def _text(value: Any) -> str:
    return str(value or "").strip()


# 采集脚本：一次性取回所有同步可读的浏览器属性。
#
# 高熵提示符（architecture / bitness / platformVersion / fullVersionList）只能通过
# getHighEntropyValues() 的 Promise 拿到，DrissionPage 的 run_js 不会 await Promise，
# 所以先把结果挂到 window 上，再由 Python 侧轮询取回。
_COLLECT_JS = r"""
(function () {
    function safe(fn, fallback) {
        try {
            var value = fn();
            return value === undefined || value === null ? fallback : value;
        } catch (error) {
            return fallback;
        }
    }

    var uaData = navigator.userAgentData || null;

    var brands = safe(function () {
        if (!uaData || !uaData.brands) return [];
        return Array.from(uaData.brands).map(function (item) {
            return { brand: String(item.brand || ''), version: String(item.version || '') };
        });
    }, []);

    // 触发一次高熵提示符读取，结果写在 window.__wbHighEntropy 上。
    window.__wbHighEntropy = null;
    if (uaData && typeof uaData.getHighEntropyValues === 'function') {
        try {
            uaData.getHighEntropyValues([
                'architecture', 'bitness', 'model', 'platformVersion',
                'fullVersionList', 'wow64', 'uaFullVersion'
            ]).then(function (values) {
                window.__wbHighEntropy = values;
            }).catch(function () {});
        } catch (error) {}
    }

    var plugins = safe(function () {
        return Array.from(navigator.plugins || []).map(function (item) {
            return String(item.name || '');
        });
    }, []);

    var mimeTypes = safe(function () {
        return Array.from(navigator.mimeTypes || []).map(function (item) {
            return String(item.type || '');
        });
    }, []);

    var screenInfo = safe(function () {
        return {
            width: screen.width,
            height: screen.height,
            availWidth: screen.availWidth,
            availHeight: screen.availHeight,
            colorDepth: screen.colorDepth,
            pixelDepth: screen.pixelDepth,
            devicePixelRatio: window.devicePixelRatio,
            innerWidth: window.innerWidth,
            innerHeight: window.innerHeight,
            outerWidth: window.outerWidth,
            outerHeight: window.outerHeight,
        };
    }, {});

    var metaValue = function (name) {
        var element = document.querySelector('meta[name="' + name + '"]');
        return element ? String(element.getAttribute('content') || '') : '';
    };

    return {
        user_agent: String(navigator.userAgent || ''),
        app_version: String(navigator.appVersion || ''),
        platform: String(navigator.platform || ''),
        language: String(navigator.language || ''),
        languages: Array.from(navigator.languages || []),
        hardware_concurrency: safe(function () { return navigator.hardwareConcurrency; }, 0),
        device_memory: safe(function () { return navigator.deviceMemory; }, 0),
        webdriver: safe(function () { return navigator.webdriver; }, false),
        max_touch_points: safe(function () { return navigator.maxTouchPoints; }, 0),
        cookie_enabled: safe(function () { return navigator.cookieEnabled; }, true),
        do_not_track: safe(function () { return navigator.doNotTrack; }, ''),
        timezone: safe(function () {
            return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
        }, ''),
        timezone_offset_minutes: new Date().getTimezoneOffset(),
        brands: brands,
        ua_platform: uaData ? String(uaData.platform || '') : '',
        mobile: uaData ? !!uaData.mobile : false,
        plugins: plugins,
        mime_types: mimeTypes,
        screen: screenInfo,
        client_version: metaValue('oai-client-version'),
        client_build_number: metaValue('oai-client-build-number'),
        data_build: safe(function () {
            return document.body ? String(document.body.getAttribute('data-build') || '') : '';
        }, ''),
    };
})();
"""

_HIGH_ENTROPY_JS = "return window.__wbHighEntropy || null;"


def build_sec_ch_ua(brands: Any) -> str:
    """按 Chrome 自身的规则拼出 ``Sec-CH-UA`` 请求头。

    Chrome 的 ``Sec-CH-UA`` 就是 ``navigator.userAgentData.brands`` 逐项渲染的结果，
    顺序与 GREASE 品牌完全一致——因此这里必须原样使用浏览器给出的顺序，
    任何重排都会与浏览器真实发出的请求对不上。
    """
    if not isinstance(brands, (list, tuple)):
        return ""
    parts: list[str] = []
    for item in brands:
        if not isinstance(item, dict):
            continue
        brand = _text(item.get("brand"))
        version = _text(item.get("version"))
        if brand and version:
            parts.append(f'"{brand}";v="{version}"')
    return ", ".join(parts)


def build_full_version_list(brands: Any) -> str:
    """按 Chrome 自身的规则拼出 ``Sec-CH-UA-Full-Version-List``。"""
    if not isinstance(brands, (list, tuple)):
        return ""
    parts: list[str] = []
    for item in brands:
        if not isinstance(item, dict):
            continue
        brand = _text(item.get("brand"))
        version = _text(item.get("version"))
        if brand and version:
            parts.append(f'"{brand}";v="{version}"')
    return ", ".join(parts)


def chrome_accept_language(languages: Any) -> str:
    """按 Chrome 自身的规则拼出 ``Accept-Language``。

    Chrome 会把 ``navigator.languages`` 逐项降权：首项不带 q 值，后续每项递减 0.1。
    """
    if not isinstance(languages, (list, tuple)):
        return ""
    items = [_text(item) for item in languages]
    items = [item for item in items if item]
    if not items:
        return ""
    parts = [items[0]]
    for index, item in enumerate(items[1:], start=1):
        quality = max(0.1, round(1.0 - 0.1 * index, 1))
        parts.append(f"{item};q={quality}")
    return ",".join(parts)


def extract_device_id(cookies: Any) -> str:
    """从 cookie 列表里取出设备标识（oai-did）。"""
    if not isinstance(cookies, (list, tuple)):
        return ""
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name")).lower()
        if name in DEVICE_ID_COOKIE_NAMES:
            value = _text(item.get("value"))
            if value:
                return value
    return ""


def chrome_major_version(user_agent: str) -> str:
    """从 UA 里取出 Chrome 主版本号。"""
    match = _VERSION_RE.search(_text(user_agent))
    return match.group(1) if match else ""


def _platform_from_user_agent(user_agent: str) -> str:
    """在缺少 userAgentData 时，从 UA 推断 Client Hints 的平台值。"""
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


def _fallback_sec_ch_ua(user_agent: str) -> str:
    """没有 UA-CH 数据时，按 Chrome 的品牌顺序构造一份。

    Chrome 的品牌列表固定包含 GREASE 品牌，且 ``Google Chrome`` 只在非 Chromium
    内核上出现；这里用真实 UA 的版本号填充，避免出现「UA 说 Edge、CH 说 Chromium」
    这类互相打脸的组合。
    """
    version = chrome_major_version(user_agent)
    if not version:
        return ""
    brands = [
        {"brand": "Not A(Brand", "version": "24"},
        {"brand": "Chromium", "version": version},
    ]
    agent = _text(user_agent)
    if "Edg/" in agent:
        brands.append({"brand": "Microsoft Edge", "version": version})
    elif "Chrome/" in agent:
        brands.append({"brand": "Google Chrome", "version": version})
    return build_sec_ch_ua(brands)


def normalize_identity(raw: Any) -> dict[str, Any]:
    """把采集到的原始字段整理成稳定、可直接落库的字典。"""
    data = raw if isinstance(raw, dict) else {}
    user_agent = _text(data.get("user_agent"))
    brands = data.get("brands") if isinstance(data.get("brands"), list) else []
    languages = data.get("languages") if isinstance(data.get("languages"), list) else []

    sec_ch_ua = build_sec_ch_ua(brands) or _fallback_sec_ch_ua(user_agent)
    ua_platform = _text(data.get("ua_platform")) or _platform_from_user_agent(user_agent)
    high = data.get("high_entropy") if isinstance(data.get("high_entropy"), dict) else {}

    architecture = _text(high.get("architecture"))
    bitness = _text(high.get("bitness"))
    platform_version = _text(high.get("platformVersion"))
    full_version_list = build_full_version_list(high.get("fullVersionList")) or build_full_version_list(brands)

    identity: dict[str, Any] = {
        "user_agent": user_agent,
        "sec_ch_ua": sec_ch_ua,
        "sec_ch_ua_full_version_list": full_version_list,
        "sec_ch_ua_platform": ua_platform,
        "sec_ch_ua_platform_version": platform_version,
        "sec_ch_ua_arch": architecture,
        "sec_ch_ua_bitness": bitness,
        "sec_ch_ua_mobile": "?1" if data.get("mobile") else "?0",
        "accept_language": chrome_accept_language(languages) or _text(data.get("accept_language")),
        "navigator_language": _text(data.get("language")),
        "navigator_languages": [_text(item) for item in languages if _text(item)],
        "navigator_platform": _text(data.get("platform")),
        "hardware_concurrency": data.get("hardware_concurrency"),
        "device_memory": data.get("device_memory"),
        "webdriver": bool(data.get("webdriver")),
        "max_touch_points": data.get("max_touch_points"),
        "timezone": _text(data.get("timezone")),
        "timezone_offset_minutes": data.get("timezone_offset_minutes"),
        "screen": data.get("screen") if isinstance(data.get("screen"), dict) else {},
        "plugins": data.get("plugins") if isinstance(data.get("plugins"), list) else [],
        "mime_types": data.get("mime_types") if isinstance(data.get("mime_types"), list) else [],
        "device_id": _text(data.get("device_id")),
        "client_version": _text(data.get("client_version")),
        "client_build_number": _text(data.get("client_build_number")),
        "data_build": _text(data.get("data_build")),
    }
    return identity


def capture_browser_identity(session: Any, *, timeout: float = 8.0) -> dict[str, Any]:
    """从一个运行中的浏览器会话采集身份快照。

    采集失败时返回只含空值的字典——调用方应当据此回退到保守默认值，
    而不是抛错阻断注册流程（指纹采集属于「锦上添花」，注册本身已经跑完了）。
    """
    page = getattr(session, "page", None)
    if page is None:
        return normalize_identity({})

    try:
        # as_expr=True：脚本本身是一个 IIFE 表达式。DrissionPage 默认会把脚本包进
        # `function(){ ... }`，那样 IIFE 的返回值会被丢掉（外层没有 return）。
        raw = page.run_js(_COLLECT_JS, as_expr=True)
    except Exception:
        return normalize_identity({})
    if not isinstance(raw, dict):
        return normalize_identity({})

    # 高熵提示符是异步的：轮询一小会儿拿结果。
    deadline = time.monotonic() + max(0.5, float(timeout or 0.0))
    high: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            value = page.run_js(_HIGH_ENTROPY_JS)
        except Exception:
            break
        if isinstance(value, dict) and value:
            high = value
            break
        time.sleep(0.1)
    raw["high_entropy"] = high

    # device id 来自 oai-did cookie：这是浏览器自己生成并持久化的设备标识。
    try:
        cookies = page.cookies(all_domains=True, all_info=True) or []
    except Exception:
        cookies = []
    raw["device_id"] = extract_device_id(cookies)

    return normalize_identity(raw)


def identity_headers(identity: Any, *, fallback_user_agent: str = "") -> dict[str, str]:
    """把身份快照转换成「与浏览器完全一致」的请求头。

    这里刻意只输出浏览器真的会发送、且与服务端可见值一一对应的头：
    UA、Sec-CH-UA 系列、Accept-Language、Accept-CH。
    """
    data = identity if isinstance(identity, dict) else {}
    headers: dict[str, str] = {}

    user_agent = _text(data.get("user_agent")) or _text(fallback_user_agent)
    if user_agent:
        headers["User-Agent"] = user_agent

    sec_ch_ua = _text(data.get("sec_ch_ua"))
    if sec_ch_ua:
        headers["Sec-CH-UA"] = sec_ch_ua

    full_version_list = _text(data.get("sec_ch_ua_full_version_list"))
    if full_version_list:
        headers["Sec-CH-UA-Full-Version-List"] = full_version_list

    platform = _text(data.get("sec_ch_ua_platform"))
    if platform:
        headers["Sec-CH-UA-Platform"] = f'"{platform}"'

    platform_version = _text(data.get("sec_ch_ua_platform_version"))
    if platform_version:
        headers["Sec-CH-UA-Platform-Version"] = f'"{platform_version}"'

    architecture = _text(data.get("sec_ch_ua_arch"))
    if architecture:
        headers["Sec-CH-UA-Arch"] = f'"{architecture}"'

    bitness = _text(data.get("sec_ch_ua_bitness"))
    if bitness:
        headers["Sec-CH-UA-Bitness"] = f'"{bitness}"'

    headers["Sec-CH-UA-Mobile"] = _text(data.get("sec_ch_ua_mobile")) or "?0"

    accept_language = _text(data.get("accept_language"))
    if accept_language:
        headers["Accept-Language"] = accept_language

    headers["Accept-CH"] = CHROME_ACCEPT_CH
    return headers


__all__ = [
    "CHROME_ACCEPT_CH",
    "build_full_version_list",
    "build_sec_ch_ua",
    "capture_browser_identity",
    "chrome_accept_language",
    "chrome_major_version",
    "extract_device_id",
    "identity_headers",
    "normalize_identity",
]
