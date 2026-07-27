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
import sys
from dataclasses import dataclass

_DeviceProfile = tuple[str, str, tuple[int, ...], tuple[int, ...]]

_DEVICE_PROFILES_BY_PLATFORM: dict[str, list[_DeviceProfile]] = {
    "darwin": [
        ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)", (8,), (8,)),
        ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)", (8, 10, 12), (8,)),
        ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)", (8, 10, 12, 14, 16), (8,)),
        ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M4, Unspecified Version)", (10, 12, 14, 16), (8,)),
    ],
    "win32": [
        ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x00009A49) Direct3D11 vs_5_0 ps_5_0, D3D11)", (8, 12, 16), (4, 8)),
        ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E9B) Direct3D11 vs_5_0 ps_5_0, D3D11)", (4, 6, 8, 12), (4, 8)),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002504) Direct3D11 vs_5_0 ps_5_0, D3D11)", (8, 12, 16), (8,)),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti (0x00002191) Direct3D11 vs_5_0 ps_5_0, D3D11)", (8, 12, 16), (8,)),
        ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 (0x000073FF) Direct3D11 vs_5_0 ps_5_0, D3D11)", (8, 12, 16), (8,)),
    ],
    "linux": [
        ("Google Inc. (Intel)", "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)", (4, 6, 8, 12), (4, 8)),
        ("Google Inc. (Intel)", "ANGLE (Intel, Mesa Intel(R) Iris(R) Xe Graphics (TGL GT2), OpenGL 4.6)", (8, 12, 16), (4, 8)),
        ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 (radeonsi, navi23, LLVM 17.0.6), OpenGL 4.6)", (8, 12, 16), (8,)),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060/PCIe/SSE2, OpenGL 4.6)", (8, 12, 16), (8,)),
    ],
}


@dataclass(frozen=True)
class BrowserIdentity:
    gpu_vendor: str
    gpu_renderer: str
    hardware_concurrency: int
    device_memory: int
    canvas_seed: int


def random_identity(platform_name: str | None = None) -> BrowserIdentity:
    """生成一套本轮注册全程复用的随机指纹身份。"""
    platform_name = str(platform_name or sys.platform).lower()
    if platform_name.startswith("win"):
        platform_key = "win32"
    elif platform_name == "darwin":
        platform_key = "darwin"
    else:
        platform_key = "linux"
    gpu_vendor, gpu_renderer, concurrency_values, memory_values = secrets.choice(
        _DEVICE_PROFILES_BY_PLATFORM[platform_key]
    )
    return BrowserIdentity(
        gpu_vendor=gpu_vendor,
        gpu_renderer=gpu_renderer,
        hardware_concurrency=secrets.choice(concurrency_values),
        device_memory=secrets.choice(memory_values),
        canvas_seed=secrets.randbelow(2**31) or 1,
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

    // 噪声只由固定种子和像素坐标决定，同一画布重复读取不会发生漂移。
    function pixelNoise(seed, x, y) {{
        var value = seed ^ Math.imul(x + 1, 374761393) ^ Math.imul(y + 1, 668265263);
        value = Math.imul(value ^ (value >>> 13), 1274126177);
        value = value ^ (value >>> 16);
        return ((value >>> 0) % 3) - 1;
    }}

    function applyCanvasNoise(imageData, offsetX, offsetY) {{
        var data = imageData.data;
        var width = imageData.width;
        for (var index = 0; index < data.length; index += 4) {{
            if (data[index + 3] === 0) continue;
            var pixel = index / 4;
            var x = offsetX + (pixel % width);
            var y = offsetY + Math.floor(pixel / width);
            var noise = pixelNoise({identity.canvas_seed}, x, y);
            data[index] = Math.min(255, Math.max(0, data[index] + noise));
            data[index + 2] = Math.min(255, Math.max(0, data[index + 2] - noise));
        }}
        return imageData;
    }}

    var origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function () {{
        var imageData = origGetImageData.apply(this, arguments);
        return applyCanvasNoise(imageData, Number(arguments[0]) || 0, Number(arguments[1]) || 0);
    }};

    var origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    var origToBlob = HTMLCanvasElement.prototype.toBlob;
    function noisyCanvas(source) {{
        var clone = document.createElement('canvas');
        clone.width = source.width;
        clone.height = source.height;
        var context = clone.getContext('2d');
        if (!context || !clone.width || !clone.height) return source;
        context.drawImage(source, 0, 0);
        var imageData = origGetImageData.call(context, 0, 0, clone.width, clone.height);
        applyCanvasNoise(imageData, 0, 0);
        context.putImageData(imageData, 0, 0);
        return clone;
    }}

    HTMLCanvasElement.prototype.toDataURL = function () {{
        try {{
            return origToDataURL.apply(noisyCanvas(this), arguments);
        }} catch (e) {{}}
        return origToDataURL.apply(this, arguments);
    }};
    if (origToBlob) {{
        HTMLCanvasElement.prototype.toBlob = function () {{
            try {{
                return origToBlob.apply(noisyCanvas(this), arguments);
            }} catch (e) {{}}
            return origToBlob.apply(this, arguments);
        }};
    }}

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
