"""每轮注册生成一套内部一致的随机浏览器指纹身份。

批量注册时如果每个窗口的 WebGL / Canvas / CPU 核数等特征完全相同，Cloudflare
等风控很容易把它们关联成"同一台机器在刷号"。这里只随机化不影响网络层的纯
JS 可见信号（WebGL vendor/renderer、Canvas 噪声、hardwareConcurrency、
deviceMemory），不改写 User-Agent / Chrome 版本号——因为真实 Chrome 的 UA 天然
和它自己的 TLS/JA3 指纹、Client Hints（sec-ch-ua 等请求头）保持一致，一旦只
改 UA 字符串而不同步改这些底层信号，反而会造成比不改更容易被识别的不一致。

每次调用 `random_identity()` 生成的一组字段互相匹配、同一轮注册全程复用，
不同轮次之间随机变化。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

_GPU_PROFILES: list[tuple[str, str]] = [
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x00009A49) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E9B) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002504) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti (0x00002191) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 (0x000073FF) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)"),
]

_HARDWARE_CONCURRENCY: list[int] = [4, 6, 8, 12, 16]
_DEVICE_MEMORY: list[int] = [4, 8, 16]


@dataclass(frozen=True)
class BrowserIdentity:
    gpu_vendor: str
    gpu_renderer: str
    hardware_concurrency: int
    device_memory: int
    canvas_seed: int
    webgl_seed: int


def random_identity() -> BrowserIdentity:
    """生成一套本轮注册全程复用的随机指纹身份。"""
    gpu_vendor, gpu_renderer = secrets.choice(_GPU_PROFILES)
    return BrowserIdentity(
        gpu_vendor=gpu_vendor,
        gpu_renderer=gpu_renderer,
        hardware_concurrency=secrets.choice(_HARDWARE_CONCURRENCY),
        device_memory=secrets.choice(_DEVICE_MEMORY),
        canvas_seed=secrets.randbelow(2**31) or 1,
        webgl_seed=secrets.randbelow(2**31) or 1,
    )


def build_fingerprint_script(identity: BrowserIdentity) -> str:
    """生成随浏览器扩展注入的 JS：document_start / MAIN world 执行，覆盖真实 Chrome 的
    WebGL vendor/renderer、Canvas 读数、hardwareConcurrency、deviceMemory 与
    navigator.webdriver。写法上和仓库里已有的 turnstile_patch 扩展保持一致。
    """
    gpu_vendor = json.dumps(identity.gpu_vendor)
    gpu_renderer = json.dumps(identity.gpu_renderer)
    return f"""
(function () {{
    function define(target, prop, value) {{
        try {{
            Object.defineProperty(target, prop, {{ get: function () {{ return value; }}, configurable: true }});
        }} catch (e) {{}}
    }}

    define(Navigator.prototype, 'hardwareConcurrency', {identity.hardware_concurrency});
    define(Navigator.prototype, 'deviceMemory', {identity.device_memory});
    define(Navigator.prototype, 'webdriver', undefined);

    // 简单可复现的伪随机数生成器：同一轮注册内种子固定，多次取值互相一致。
    function mulberry32(seed) {{
        return function () {{
            seed |= 0;
            seed = (seed + 0x6D2B79F5) | 0;
            var t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
            t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
            return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
        }};
    }}

    var canvasRandom = mulberry32({identity.canvas_seed});
    var origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function () {{
        var imageData = origGetImageData.apply(this, arguments);
        var data = imageData.data;
        for (var i = 0; i < data.length; i += 4) {{
            var noise = Math.floor(canvasRandom() * 3) - 1;
            data[i] = Math.min(255, Math.max(0, data[i] + noise));
        }}
        return imageData;
    }};
    var origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function () {{
        try {{
            var ctx = this.getContext('2d');
            if (ctx && this.width > 0 && this.height > 0) {{
                var imageData = ctx.getImageData(0, 0, this.width, this.height);
                ctx.putImageData(imageData, 0, 0);
            }}
        }} catch (e) {{}}
        return origToDataURL.apply(this, arguments);
    }};

    function patchWebGL(proto) {{
        if (!proto) return;
        var origGetParameter = proto.getParameter;
        proto.getParameter = function (parameter) {{
            if (parameter === 37445) return {gpu_vendor};   // UNMASKED_VENDOR_WEBGL
            if (parameter === 37446) return {gpu_renderer};  // UNMASKED_RENDERER_WEBGL
            return origGetParameter.call(this, parameter);
        }};
    }}
    try {{ patchWebGL(window.WebGLRenderingContext && window.WebGLRenderingContext.prototype); }} catch (e) {{}}
    try {{ patchWebGL(window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype); }} catch (e) {{}}
}})();
""".strip()


def build_fingerprint_extension_files(identity: BrowserIdentity) -> dict[str, str]:
    """返回 {文件名: 内容} 字典，写到临时目录后可用 ChromiumOptions.add_extension() 加载。"""
    manifest = {
        "manifest_version": 3,
        "name": "Fingerprint Patcher",
        "version": "1.0",
        "content_scripts": [
            {
                "js": ["./script.js"],
                "matches": ["<all_urls>"],
                "run_at": "document_start",
                "all_frames": True,
                "world": "MAIN",
            }
        ],
    }
    return {
        "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2),
        "script.js": build_fingerprint_script(identity),
    }
