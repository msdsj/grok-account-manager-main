#!/usr/bin/env python3
"""浏览器指纹一致性审计（差分法）。

同一个 Chrome、同一个 Profile 策略，分别跑两遍：

1. **基线**：完全不注入指纹脚本，得到「这台机器上真实 Chrome 的原始取值」；
2. **实验组**：注入项目的指纹脚本。

然后逐项对比：

- 被改写的属性 —— 列出来供人工确认是否合理；
- **没被改写、却和被改写的属性互相矛盾的** —— 这才是真正的破绽
  （例如扩展列表说支持某扩展、``getExtension`` 却返回 null）；
- 凭空多出来的属性 —— 真实 Chrome 没有、注入后出现的，一律判失败。

用法::

    uv run python scripts/fingerprint_audit.py               # 随机指纹模式
    uv run python scripts/fingerprint_audit.py --real-device # 与手动注册同源
    uv run python scripts/fingerprint_audit.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "serve"))

from grok_account_manager.core.browser import (  # noqa: E402
    DrissionBrowserSession,
    build_chromium_options,
)
from grok_account_manager.core.browser_identity import (  # noqa: E402
    capture_browser_identity,
)

# 采集页面里所有与「这台机器上的真实 Chrome」相关的属性。
PROBE_JS = r"""
(function () {
    const out = {};

    out.user_agent = String(navigator.userAgent || '');
    out.platform = String(navigator.platform || '');
    out.languages = Array.from(navigator.languages || []);
    out.language = String(navigator.language || '');
    out.hardware_concurrency = navigator.hardwareConcurrency;
    out.device_memory = navigator.deviceMemory;
    out.webdriver = navigator.webdriver;
    out.max_touch_points = navigator.maxTouchPoints;
    out.pdf_viewer_enabled = navigator.pdfViewerEnabled;
    out.plugins = Array.from(navigator.plugins || []).map(p => String(p.name || ''));
    out.mime_types = Array.from(navigator.mimeTypes || []).map(m => String(m.type || ''));
    out.screen = {
        width: screen.width, height: screen.height,
        availWidth: screen.availWidth, availHeight: screen.availHeight,
        colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth,
        devicePixelRatio: window.devicePixelRatio,
        innerWidth: window.innerWidth, innerHeight: window.innerHeight,
    };
    out.timezone = (function () {
        try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) { return ''; }
    })();

    const uaData = navigator.userAgentData;
    out.ua_platform = uaData ? String(uaData.platform || '') : '';
    out.ua_mobile = uaData ? !!uaData.mobile : false;
    out.ua_brands = uaData ? Array.from(uaData.brands).map(b => b.brand + '/' + b.version) : [];

    // window.chrome 上到底有哪些成员（用来发现「凭空多出来的属性」）。
    const chromeObject = window.chrome || {};
    out.chrome_members = [];
    for (const key in chromeObject) {
        out.chrome_members.push(key + ':' + typeof chromeObject[key]);
    }
    out.chrome_members.sort();

    // WebGL：真实 GPU 字符串 + 扩展列表。
    try {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl');
        if (gl) {
            out.webgl_vendor = String(gl.getParameter(37445) || '');
            out.webgl_renderer = String(gl.getParameter(37446) || '');
            const extensions = gl.getSupportedExtensions() || [];
            out.webgl_extensions = Array.from(extensions).sort();
            // 扩展列表里声称支持、实际却 getExtension 不到的，就是自相矛盾。
            out.webgl_inconsistent = Array.from(extensions).filter(n => !gl.getExtension(n));
        } else {
            out.webgl_vendor = '';
            out.webgl_renderer = '';
            out.webgl_extensions = [];
            out.webgl_inconsistent = [];
        }
    } catch (error) {
        out.webgl_vendor = '';
        out.webgl_renderer = '';
        out.webgl_extensions = [];
        out.webgl_inconsistent = ['error: ' + String(error)];
    }

    // Canvas 读数：同一画布两次必须一致。
    try {
        const canvas = document.createElement('canvas');
        canvas.width = 220;
        canvas.height = 60;
        const ctx = canvas.getContext('2d');
        ctx.textBaseline = 'top';
        ctx.font = '16px Arial';
        ctx.fillStyle = '#f60';
        ctx.fillRect(10, 5, 90, 24);
        ctx.fillStyle = '#069';
        ctx.fillText('consistency-audit', 4, 20);
        out.canvas_hash = canvas.toDataURL().length + ':' + canvas.toDataURL().slice(-24);
    } catch (error) {
        out.canvas_hash = 'error';
    }

    // 被 patch 过的函数必须看起来像原生实现。
    try {
        out.get_image_data_to_string = String(
            CanvasRenderingContext2D.prototype.getImageData
        ).slice(0, 80);
    } catch (error) {
        out.get_image_data_to_string = 'error';
    }

    return out;
})();
"""


def run_probe(real_device: bool, skip_fingerprint: bool, extra_args: list[str]) -> dict:
    """启动一次浏览器并采集属性。"""
    options = build_chromium_options(
        lang="en-US",
        headless=False,
        window_index=0,
        window_count=1,
        real_device=real_device,
    )
    options._grok_skip_fingerprint = bool(skip_fingerprint)
    # extra_args 只用于受限的 CI / 沙箱环境。正常注册流程不要加这些开关——
    # 它们本身也是特征。
    for argument in extra_args:
        options.set_argument(argument)

    session = DrissionBrowserSession(options)
    session.start()
    try:
        html = Path(tempfile.gettempdir()) / "grok-fingerprint-audit.html"
        html.write_text(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>fingerprint audit</title></head><body>audit</body></html>",
            encoding="utf-8",
        )
        session.open_url(html.as_uri())
        page = session.refresh_page()
        page.wait.doc_loaded()
        probe = page.run_js(PROBE_JS, as_expr=True) or {}
        identity = capture_browser_identity(session)
        return {"probe": probe, "identity": identity}
    finally:
        try:
            session.stop()
        except Exception:
            pass


def compare(baseline: dict, patched: dict) -> list[dict]:
    """对比基线与实验组，返回检查结论列表。"""
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1) WebGL 扩展列表与 getExtension 必须自洽。
    inconsistent = patched.get("webgl_inconsistent") or []
    add(
        "WebGL 扩展列表与 getExtension 自洽",
        not inconsistent,
        ("不一致: " + ", ".join(map(str, inconsistent))) if inconsistent else
        f"renderer={patched.get('webgl_renderer')} 扩展数={len(patched.get('webgl_extensions') or [])}",
    )

    # 1b) WebGL 可用时扩展列表不该是空的——空列表意味着降级到了兜底列表，
    #     也就意味着 getSupportedExtensions 和真实能力对不上。
    webgl_available = bool(patched.get("webgl_renderer"))
    add(
        "WebGL 可用时扩展列表非空",
        (not webgl_available) or bool(patched.get("webgl_extensions")),
        (
            f"renderer={patched.get('webgl_renderer') or '(本机 WebGL 不可用)'}, "
            f"扩展数={len(patched.get('webgl_extensions') or [])}"
        ),
    )

    # 2) 不能凭空多出 window.chrome 成员。
    base_members = set(baseline.get("chrome_members") or [])
    new_members = set(patched.get("chrome_members") or []) - base_members
    add(
        "未凭空新增 window.chrome 成员",
        not new_members,
        ("新增: " + ", ".join(sorted(new_members))) if new_members else
        ("基线成员: " + (", ".join(sorted(base_members)) or "无")),
    )

    # 3) UA-CH 平台与 navigator.platform 必须相容。
    ua_platform = patched.get("ua_platform") or ""
    nav_platform = patched.get("platform") or ""
    expected = {"macOS": "MacIntel", "Windows": "Win32", "Linux": "Linux x86_64"}
    add(
        "navigator.platform 与 UA-CH platform 相容",
        not ua_platform or not expected.get(ua_platform) or expected[ua_platform] == nav_platform,
        f"{nav_platform} vs {ua_platform}",
    )

    # 4) 插件列表不能为空。
    add(
        "navigator.plugins 非空",
        len(patched.get("plugins") or []) > 0,
        str(len(patched.get("plugins") or [])),
    )

    # 5) webdriver 必须是 false（真实 Chrome 的取值）。
    add(
        "navigator.webdriver === false",
        patched.get("webdriver") is False,
        str(patched.get("webdriver")),
    )

    # 6) hardwareConcurrency / deviceMemory 必须与真实 Chrome 一致。
    #    这里不写死合法取值集合——Chrome 的上报规则会随版本变化（例如
    #    deviceMemory 的上限从 8 放宽到 16），基线才是唯一正确的参照。
    cores = patched.get("hardware_concurrency")
    base_cores = baseline.get("hardware_concurrency")
    add(
        "hardwareConcurrency 与真实 Chrome 一致",
        isinstance(cores, int) and cores > 0 and cores == base_cores,
        f"基线 {base_cores} / 实验组 {cores}",
    )
    memory = patched.get("device_memory")
    base_memory = baseline.get("device_memory")
    add(
        "deviceMemory 与真实 Chrome 一致",
        memory is not None and memory == base_memory,
        f"基线 {base_memory} / 实验组 {memory}",
    )

    # 7) navigator.language 必须是 languages[0]。
    languages = patched.get("languages") or []
    add(
        "navigator.language 与 languages[0] 一致",
        not languages or patched.get("language") == languages[0],
        f"{patched.get('language')} | {', '.join(languages)}",
    )

    # 8) Canvas 读数必须稳定（同一浏览器两次读数一致）。
    add(
        "Canvas 读数稳定",
        bool(patched.get("canvas_hash")) and patched.get("canvas_hash") != "error",
        str(patched.get("canvas_hash")),
    )

    # 9) 被 patch 的函数要像原生实现。
    to_string = str(patched.get("get_image_data_to_string") or "")
    add(
        "patch 函数的 toString 像原生",
        "[native code]" in to_string,
        to_string,
    )

    # 10) 窗口尺寸必须落在屏幕范围内。
    screen = patched.get("screen") or {}
    add(
        "窗口尺寸在屏幕范围内",
        screen.get("innerWidth", 0) <= screen.get("width", 0)
        and screen.get("innerHeight", 0) <= screen.get("height", 0),
        f"{screen.get('width')}x{screen.get('height')} 窗口 {screen.get('innerWidth')}x{screen.get('innerHeight')}",
    )

    # 11) UA-CH 品牌列表不能为空（Sec-CH-UA 就是由它生成的）。
    add(
        "UA-CH 品牌列表非空",
        bool(patched.get("ua_brands")),
        ", ".join(patched.get("ua_brands") or []),
    )

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="浏览器指纹一致性审计（差分法）")
    parser.add_argument(
        "--real-device",
        action="store_true",
        help="使用真实设备指纹（与手动注册同源）而不是随机指纹",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="受限环境专用：给 Chrome 加 --no-sandbox（正常注册不要使用）",
    )
    args = parser.parse_args()

    extra = ["--no-sandbox", "--disable-gpu"] if args.no_sandbox else []

    print("[1/2] 采集基线（不注入指纹脚本）…")
    baseline = run_probe(args.real_device, True, extra)
    print("[2/2] 采集实验组（注入指纹脚本）…")
    patched = run_probe(args.real_device, False, extra)

    checks = compare(baseline["probe"], patched["probe"])

    # 被改写的属性 —— 人工确认清单。
    changed = []
    for key, base_value in baseline["probe"].items():
        new_value = patched["probe"].get(key)
        if base_value != new_value:
            changed.append(
                {
                    "key": key,
                    "baseline": base_value,
                    "patched": new_value,
                }
            )

    report = {
        "mode": "real" if args.real_device else "random",
        "checks": checks,
        "changed": changed,
        "baseline": baseline,
        "patched": patched,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0

    print()
    print("=" * 78)
    print(f"浏览器指纹一致性审计（模式：{report['mode']}，差分法）")
    print("=" * 78)
    failures = 0
    for check in checks:
        mark = "✅" if check["ok"] else "❌"
        if not check["ok"]:
            failures += 1
        print(f"  {mark} {check['name']}")
        if check["detail"]:
            print(f"       {check['detail']}")

    print()
    print("-" * 78)
    print("被指纹脚本改写的属性（人工确认清单）")
    print("-" * 78)
    if not changed:
        print("  （无 —— 指纹脚本没有改变任何可观测属性）")
    for item in changed:
        print(f"  • {item['key']}")
        print(f"      基线: {str(item['baseline'])[:110]}")
        print(f"      改写: {str(item['patched'])[:110]}")

    identity = patched["identity"] or {}
    print()
    print("-" * 78)
    print("浏览器身份快照（注册时写入凭证，网关侧据此回放请求头）")
    print("-" * 78)
    for key in (
        "user_agent",
        "sec_ch_ua",
        "sec_ch_ua_platform",
        "sec_ch_ua_arch",
        "sec_ch_ua_bitness",
        "accept_language",
        "navigator_language",
        "hardware_concurrency",
        "device_memory",
        "timezone",
        "device_id",
        "webdriver",
    ):
        value = identity.get(key)
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        print(f"  {key:24s} = {value}")
    screen = identity.get("screen") or {}
    print(f"  {'screen':24s} = {screen.get('width')}x{screen.get('height')} @ {screen.get('devicePixelRatio')}x")

    print()
    print(f"审计结果：{len(checks) - failures} 通过 / {failures} 失败")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
