"""ChatGPT sentinel proof-of-work 最小实现。

只保留图片链路需要的 PoW 计算与首页脚本解析，移植自参考项目
chatgpt2api 的 `utils/pow.py`，去掉了 pybase64 依赖。
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

DEFAULT_POW_SCRIPT = "https://chatgpt.com/backend-api/sentinel/sdk.js"

CORES = [8, 16, 24, 32]
DOCUMENT_KEYS = ["__reactContainer$fzelfjyxej8", "_reactListening5dehydibo78", "location"]
SCREEN_RESOLUTIONS = [[1920, 1080], [1440, 900], [2560, 1440], [3840, 2160]]


class _ScriptSrcParser(HTMLParser):
    """从首页 HTML 里提取脚本地址与 data-build 版本号。"""

    def __init__(self) -> None:
        super().__init__()
        self.script_sources: list[str] = []
        self.data_build = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attrs_dict = dict(attrs)
        src = attrs_dict.get("src")
        if not src:
            return
        self.script_sources.append(src)
        match = re.search(r"c/[^/]*/_", src)
        if match:
            self.data_build = match.group(0)


def parse_pow_resources(html_content: str) -> tuple[list[str], str]:
    """返回 (脚本地址列表, data-build)。"""
    parser = _ScriptSrcParser()
    parser.feed(html_content)
    script_sources = parser.script_sources or [DEFAULT_POW_SCRIPT]
    data_build = parser.data_build
    if not data_build:
        match = re.search(r'<html[^>]*data-build="([^"]*)"', html_content)
        if match:
            data_build = match.group(1)
    return script_sources, data_build


def _legacy_parse_time(timezone_offset_minutes: int | None = None) -> str:
    if timezone_offset_minutes is None:
        timezone_offset_minutes = -480
    localized = datetime.now(timezone.utc) + timedelta(minutes=timezone_offset_minutes)
    sign = "-" if timezone_offset_minutes <= 0 else "+"
    hours = abs(int(timezone_offset_minutes)) // 60
    return localized.strftime("%a %b %d %Y %H:%M:%S") + f" GMT{sign}{hours:02d}00"


def _device_facts(device: dict | None) -> dict[str, Any]:
    """把浏览器身份快照里的设备信息整理成 PoW 配置需要的字段。

    这里的每一个值都会原样上报给 OpenAI 的 sentinel 接口：如果它写的是
    「1920x1080 / 32 核 / 美国东部时间」，而账号实际是在 macOS 笔记本的
    Chrome 里注册出来的，服务端一眼就能看出两边不是同一台设备。
    """
    facts = device if isinstance(device, dict) else {}
    screen = facts.get("screen") if isinstance(facts.get("screen"), dict) else {}
    try:
        width = int(screen.get("width") or 0)
    except (TypeError, ValueError):
        width = 0
    try:
        height = int(screen.get("height") or 0)
    except (TypeError, ValueError):
        height = 0
    try:
        cores = int(facts.get("hardware_concurrency") or 0)
    except (TypeError, ValueError):
        cores = 0
    language = str(facts.get("navigator_language") or "en-US").strip() or "en-US"
    accept_language = str(facts.get("accept_language") or "").strip() or (
        f"{language},{language.split('-')[0]};q=0.9,en;q=0.8"
    )
    try:
        offset_minutes = int(facts.get("timezone_offset_minutes"))
    except (TypeError, ValueError):
        offset_minutes = -480
    return {
        "screen_width": width or 1920,
        "screen_height": height or 1080,
        "cores": cores or 8,
        "language": language,
        "accept_language": accept_language,
        "timezone_offset_minutes": offset_minutes,
    }


def _build_pow_config(
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
    device: dict | None = None,
) -> list[Any]:
    facts = _device_facts(device)
    cores = facts["cores"]
    language = facts["language"]
    navigator_key = random.choice(
        [
            "registerProtocolHandler−function registerProtocolHandler() { [native code] }",
            "storage−[object StorageManager]",
            "locks−[object LockManager]",
            "appCodeName−Mozilla",
            "permissions−[object Permissions]",
            "share−function share() { [native code] }",
            "webdriver−false",
            "managed−[object NavigatorManagedData]",
            "canShare−function canShare() { [native code] }",
            "vendor−Google Inc.",
            "mediaDevices−[object MediaDevices]",
            "vibrate−function vibrate() { [native code] }",
            "storageBuckets−[object StorageBucketManager]",
            "mediaCapabilities−[object MediaCapabilities]",
            "cookieEnabled−true",
            "virtualKeyboard−[object VirtualKeyboard]",
            "product−Gecko",
            "presentation−[object Presentation]",
            "onLine−true",
            "mimeTypes−[object MimeTypeArray]",
            "credentials−[object CredentialsContainer]",
            "serviceWorker−[object ServiceWorkerContainer]",
            "keyboard−[object Keyboard]",
            "gpu−[object GPU]",
            "doNotTrack",
            "serial−[object Serial]",
            "pdfViewerEnabled−true",
            f"language−{language}",
            "geolocation−[object Geolocation]",
            "userAgentData−[object NavigatorUAData]",
            "getUserMedia−function getUserMedia() { [native code] }",
            "sendBeacon−function sendBeacon() { [native code] }",
            f"hardwareConcurrency−{cores}",
            "windowControlsOverlay−[object WindowControlsOverlay]",
        ]
    )
    window_key = random.choice(
        [
            "0",
            "window",
            "self",
            "document",
            "name",
            "location",
            "customElements",
            "history",
            "navigation",
            "innerWidth",
            "innerHeight",
            "scrollX",
            "scrollY",
            "visualViewport",
            "screenX",
            "screenY",
            "outerWidth",
            "outerHeight",
            "devicePixelRatio",
            "screen",
            "chrome",
            "navigator",
            "onresize",
            "performance",
            "crypto",
            "indexedDB",
            "sessionStorage",
            "localStorage",
            "scheduler",
            "alert",
            "atob",
            "btoa",
            "fetch",
            "matchMedia",
            "postMessage",
            "queueMicrotask",
            "requestAnimationFrame",
            "setInterval",
            "setTimeout",
            "caches",
            "__NEXT_DATA__",
            "__BUILD_MANIFEST",
            "__NEXT_PRELOADREADY",
        ]
    )
    script_source = random.choice(list(script_sources)) if script_sources else DEFAULT_POW_SCRIPT
    return [
        facts["screen_width"] + facts["screen_height"],
        _legacy_parse_time(facts["timezone_offset_minutes"]),
        4294705152,
        1,
        user_agent,
        script_source,
        data_build,
        language,
        facts["accept_language"],
        random.random(),
        navigator_key,
        random.choice(DOCUMENT_KEYS),
        window_key,
        time.perf_counter() * 1000,
        uuid.uuid4().hex,
        "",
        cores,
        time.time() * 1000 - (time.perf_counter() * 1000),
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    ]


def _pow_generate(seed: str, difficulty: str, config: list[Any], limit: int = 500000) -> tuple[str, bool]:
    target = bytes.fromhex(difficulty)
    diff_len = len(difficulty) // 2
    seed_bytes = seed.encode()
    static_1 = (json.dumps(config[:3], separators=(",", ":"), ensure_ascii=False)[:-1] + ",").encode()
    static_2 = ("," + json.dumps(config[4:9], separators=(",", ":"), ensure_ascii=False)[1:-1] + ",").encode()
    static_3 = ("," + json.dumps(config[10:], separators=(",", ":"), ensure_ascii=False)[1:]).encode()
    for index in range(limit):
        final_json = static_1 + str(index).encode() + static_2 + str(index >> 1).encode() + static_3
        encoded = base64.b64encode(final_json)
        digest = hashlib.sha3_512(seed_bytes + encoded).digest()
        if digest[:diff_len] <= target:
            return encoded.decode(), True
    fallback = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + base64.b64encode(f'"{seed}"'.encode()).decode()
    return fallback, False


def build_legacy_requirements_token(
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
    device: dict | None = None,
) -> str:
    config = _build_pow_config(
        user_agent, script_sources=script_sources, data_build=data_build, device=device
    )
    return "gAAAAAC" + base64.b64encode(
        json.dumps(config, separators=(",", ":"), ensure_ascii=False).encode()
    ).decode()


def build_proof_token(
    seed: str,
    difficulty: str,
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
    device: dict | None = None,
) -> str:
    config = _build_pow_config(
        user_agent, script_sources=script_sources, data_build=data_build, device=device
    )
    answer, solved = _pow_generate(seed, difficulty, config)
    if not solved:
        raise RuntimeError(f"failed to solve proof token: difficulty={difficulty}")
    return "gAAAAAB" + answer
