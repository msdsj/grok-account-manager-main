"""每轮注册生成一套内部一致的随机浏览器指纹身份。

批量注册时如果每个窗口的 WebGL / Canvas / CPU 核数等特征完全相同，Cloudflare、
DataDome、Google 等风控很容易把它们关联成"同一台机器在刷号"。这里随机化不影响
网络层的纯 JS 可见信号（WebGL vendor/renderer、Canvas 噪声、AudioContext 噪声、
hardwareConcurrency、deviceMemory、languages），不改写 User-Agent / Chrome 版本号
——因为真实 Chrome 的 UA 天然和它自己的 TLS/JA3 指纹、Client Hints（sec-ch-ua 等
请求头）保持一致，一旦只改 UA 字符串而不同步改这些底层信号，反而会造成比不改
更容易被识别的不一致。

覆盖的信号：

- ``navigator.webdriver`` —— 真实 Chrome 是 ``false``，不是 ``undefined``（写错反而更显眼）
- ``navigator.hardwareConcurrency`` / ``deviceMemory``
- ``navigator.languages`` / ``language`` —— 与 ``--lang`` 对齐，避免与请求头语言矛盾
- ``navigator.platform`` / ``maxTouchPoints`` / ``pdfViewerEnabled``
- Canvas 2D 读数噪声（getImageData / toDataURL / toBlob）
- WebGL UNMASKED_VENDOR/RENDERER + 扩展列表 + 常见 getParameter 常量
- AudioContext 指纹噪声（AnalyserNode / AudioBuffer）
- ``Permissions.query('notifications')`` 与 ``Notification.permission`` 一致性
- ``window.chrome`` 兜底（仅在缺失时补，正常 Chrome 本来就有）
- **``Function.prototype.toString`` 伪装** —— 否则上面所有 patch 都能被
  ``fn.toString().includes('[native code]')`` 一眼识破

每次调用 `random_identity()` 生成的一组字段互相匹配、同一轮注册全程复用，
不同轮次之间随机变化。
"""

from __future__ import annotations

import json
import secrets
import sys
from dataclasses import dataclass, field

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

# WebGL 扩展列表必须和 spoof 的 GPU 家族自洽：苹果走 Metal 通道，Windows 走 D3D11 通道，
# 各家支持的压缩纹理格式不同。随便给一个通用列表反而会暴露不一致。
_WEBGL_EXTENSIONS_COMMON = (
    "EXT_color_buffer_float",
    "EXT_color_buffer_half_float",
    "EXT_float_blend",
    "EXT_frag_depth",
    "EXT_shader_texture_lod",
    "EXT_texture_filter_anisotropic",
    "EXT_sRGB",
    "OES_draw_buffers_indexed",
    "OES_element_index_uint",
    "OES_fbo_render_mipmap",
    "OES_standard_derivatives",
    "OES_texture_float",
    "OES_texture_float_linear",
    "OES_texture_half_float",
    "OES_texture_half_float_linear",
    "OES_vertex_array_object",
    "WEBGL_debug_renderer_info",
    "WEBGL_debug_shaders",
    "WEBGL_draw_buffers",
    "WEBGL_lose_context",
    "WEBGL_multi_draw",
    "WEBGL_provoking_vertex",
)

_WEBGL_EXTENSIONS_BY_FAMILY: dict[str, tuple[str, ...]] = {
    "apple": (
        "EXT_texture_compression_bptc",
        "EXT_texture_compression_rgtc",
        "WEBGL_compressed_texture_astc",
        "WEBGL_compressed_texture_etc",
        "WEBGL_compressed_texture_etc1",
        "WEBGL_compressed_texture_pvrtc",
        "WEBGL_compressed_texture_s3tc",
        "WEBGL_compressed_texture_s3tc_srgb",
    ),
    "nvidia": (
        "EXT_texture_compression_bptc",
        "EXT_texture_compression_rgtc",
        "WEBGL_compressed_texture_s3tc",
        "WEBGL_compressed_texture_s3tc_srgb",
    ),
    "amd": (
        "EXT_texture_compression_bptc",
        "EXT_texture_compression_rgtc",
        "WEBGL_compressed_texture_s3tc",
        "WEBGL_compressed_texture_s3tc_srgb",
    ),
    "intel": (
        "EXT_texture_compression_bptc",
        "EXT_texture_compression_rgtc",
        "WEBGL_compressed_texture_s3tc",
        "WEBGL_compressed_texture_s3tc_srgb",
    ),
}

_PLATFORM_NAMES = {"darwin": "MacIntel", "win32": "Win32", "linux": "Linux x86_64"}

# 常见语言的 navigator.languages 兜底列表（与 --lang 对齐）。
_LANGUAGE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "zh": ("zh-CN", "zh", "en"),
    "en": ("en-US", "en"),
    "ja": ("ja-JP", "ja", "en"),
    "ko": ("ko-KR", "ko", "en"),
    "vi": ("vi-VN", "vi", "en"),
}


def _platform_key(platform_name: str | None) -> str:
    name = str(platform_name or sys.platform).lower()
    if name.startswith("win"):
        return "win32"
    if name == "darwin":
        return "darwin"
    return "linux"


def _languages_for(lang: str | None) -> tuple[str, ...]:
    raw = str(lang or "").strip().replace("_", "-")
    if not raw:
        return ("en-US", "en")
    primary = raw.split("-", 1)[0].lower()
    if primary in _LANGUAGE_FALLBACKS:
        fallback = _LANGUAGE_FALLBACKS[primary]
        # --lang 指定了具体区域（如 zh-TW）时以它为首选，其余保持同语言族。
        if raw.lower() == fallback[0].lower():
            return fallback
        return (raw, primary) + tuple(item for item in fallback[1:] if item.lower() != primary)
    return (raw, primary, "en")


def _gpu_family(gpu_renderer: str) -> str:
    text = str(gpu_renderer).lower()
    for family in ("apple", "nvidia", "amd", "intel"):
        if family in text:
            return family
    return "intel"


@dataclass(frozen=True)
class BrowserIdentity:
    gpu_vendor: str
    gpu_renderer: str
    hardware_concurrency: int
    device_memory: int
    canvas_seed: int
    # 以下字段都有默认值，方便测试直接构造最小身份。
    audio_seed: int = 1
    languages: tuple[str, ...] = ("en-US", "en")
    platform: str = "MacIntel"
    webgl_extensions: tuple[str, ...] = field(default_factory=tuple)


def random_identity(platform_name: str | None = None, lang: str | None = None) -> BrowserIdentity:
    """生成一套本轮注册全程复用的随机指纹身份。"""
    platform_key = _platform_key(platform_name)
    gpu_vendor, gpu_renderer, concurrency_values, memory_values = secrets.choice(
        _DEVICE_PROFILES_BY_PLATFORM[platform_key]
    )
    family = _gpu_family(gpu_renderer)
    extensions = tuple(
        sorted(set(_WEBGL_EXTENSIONS_COMMON) | set(_WEBGL_EXTENSIONS_BY_FAMILY.get(family, ())))
    )
    return BrowserIdentity(
        gpu_vendor=gpu_vendor,
        gpu_renderer=gpu_renderer,
        hardware_concurrency=secrets.choice(concurrency_values),
        device_memory=secrets.choice(memory_values),
        canvas_seed=secrets.randbelow(2**31) or 1,
        audio_seed=secrets.randbelow(2**31) or 1,
        languages=_languages_for(lang),
        platform=_PLATFORM_NAMES[platform_key],
        webgl_extensions=extensions,
    )


# JS 模板用占位符替换而不是 f-string：脚本里有大量 { }，f-string 转义极易出错。
_SCRIPT_TEMPLATE = r"""
(function () {
    // ---- 先伪装 Function.prototype.toString ----------------------------------
    // 后面所有 patch 都会替换原生方法，如果不处理 toString，
    // 风控只要 fn.toString().includes('[native code]') 就能一眼识破。
    var nativeToString = Function.prototype.toString;
    var fakeRegistry = new WeakMap();

    function register(fn, name) {
        try {
            fakeRegistry.set(fn, 'function ' + (name || fn.name || '') + '() { [native code] }');
        } catch (e) {}
        return fn;
    }

    function toStringShim() {
        var faked = fakeRegistry.get(this);
        if (typeof faked === 'string') return faked;
        return nativeToString.call(this);
    }
    register(toStringShim, 'toString');
    try {
        Object.defineProperty(Function.prototype, 'toString', {
            value: toStringShim,
            writable: true,
            configurable: true,
            enumerable: false,
        });
    } catch (e) {}

    function define(target, prop, value) {
        try {
            Object.defineProperty(target, prop, { get: function () { return value; }, configurable: true });
        } catch (e) {}
    }

    function defineMethod(target, prop, fn, name) {
        try {
            Object.defineProperty(target, prop, {
                value: register(fn, name || prop),
                writable: true,
                configurable: true,
                enumerable: false,
            });
        } catch (e) {}
    }

    var LANGUAGES = __LANGUAGES__;

    // ---- navigator ----------------------------------------------------------
    define(Navigator.prototype, 'hardwareConcurrency', __HARDWARE_CONCURRENCY__);
    define(Navigator.prototype, 'deviceMemory', __DEVICE_MEMORY__);
    // 真实 Chrome 里 navigator.webdriver 是 false；写成 undefined 反而多一个特征。
    define(Navigator.prototype, 'webdriver', false);
    define(Navigator.prototype, 'platform', __PLATFORM__);
    define(Navigator.prototype, 'maxTouchPoints', 0);
    define(Navigator.prototype, 'pdfViewerEnabled', true);
    try {
        Object.defineProperty(Navigator.prototype, 'language', {
            get: function () { return LANGUAGES[0]; }, configurable: true,
        });
        Object.defineProperty(Navigator.prototype, 'languages', {
            get: function () { return LANGUAGES.slice(); }, configurable: true,
        });
    } catch (e) {}

    // ---- Canvas 噪声 --------------------------------------------------------
    // 噪声只由固定种子和像素坐标决定，同一画布重复读取不会发生漂移。
    function pixelNoise(seed, x, y) {
        var value = seed ^ Math.imul(x + 1, 374761393) ^ Math.imul(y + 1, 668265263);
        value = Math.imul(value ^ (value >>> 13), 1274126177);
        value = value ^ (value >>> 16);
        return ((value >>> 0) % 3) - 1;
    }

    function applyCanvasNoise(imageData, offsetX, offsetY) {
        var data = imageData.data;
        var width = imageData.width;
        for (var index = 0; index < data.length; index += 4) {
            if (data[index + 3] === 0) continue;
            var pixel = index / 4;
            var x = offsetX + (pixel % width);
            var y = offsetY + Math.floor(pixel / width);
            var noise = pixelNoise(__CANVAS_SEED__, x, y);
            data[index] = Math.min(255, Math.max(0, data[index] + noise));
            data[index + 2] = Math.min(255, Math.max(0, data[index + 2] - noise));
        }
        return imageData;
    }

    var origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    defineMethod(CanvasRenderingContext2D.prototype, 'getImageData', function () {
        var imageData = origGetImageData.apply(this, arguments);
        return applyCanvasNoise(imageData, Number(arguments[0]) || 0, Number(arguments[1]) || 0);
    }, 'getImageData');

    var origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    var origToBlob = HTMLCanvasElement.prototype.toBlob;
    function noisyCanvas(source) {
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
    }

    defineMethod(HTMLCanvasElement.prototype, 'toDataURL', function () {
        try {
            return origToDataURL.apply(noisyCanvas(this), arguments);
        } catch (e) {}
        return origToDataURL.apply(this, arguments);
    }, 'toDataURL');

    if (origToBlob) {
        defineMethod(HTMLCanvasElement.prototype, 'toBlob', function () {
            try {
                return origToBlob.apply(noisyCanvas(this), arguments);
            } catch (e) {}
            return origToBlob.apply(this, arguments);
        }, 'toBlob');
    }

    // ---- WebGL --------------------------------------------------------------
    var WEBGL_EXTENSIONS = __WEBGL_EXTENSIONS__;
    var MAX_TEXTURE_SIZE = 16384;

    function patchWebGL(proto) {
        if (!proto) return;

        var origGetParameter = proto.getParameter;
        defineMethod(proto, 'getParameter', function (parameter) {
            if (parameter === 37445) return __GPU_VENDOR__;    // UNMASKED_VENDOR_WEBGL
            if (parameter === 37446) return __GPU_RENDERER__;  // UNMASKED_RENDERER_WEBGL
            if (parameter === 3379) return MAX_TEXTURE_SIZE;   // MAX_TEXTURE_SIZE
            if (parameter === 34076) return MAX_TEXTURE_SIZE;  // MAX_CUBE_MAP_TEXTURE_SIZE
            if (parameter === 34930) return 16;                // MAX_TEXTURE_IMAGE_UNITS
            if (parameter === 35660) return 16;                // MAX_VERTEX_TEXTURE_IMAGE_UNITS
            if (parameter === 35661) return 32;                // MAX_COMBINED_TEXTURE_IMAGE_UNITS
            if (parameter === 36349) return 1024;              // MAX_VERTEX_UNIFORM_VECTORS
            if (parameter === 36348) return 1024;              // MAX_FRAGMENT_UNIFORM_VECTORS
            if (parameter === 3386) return [MAX_TEXTURE_SIZE, MAX_TEXTURE_SIZE];  // MAX_VIEWPORT_DIMS
            if (parameter === 7938) return 'WebGL 1.0 (OpenGL ES 2.0 Chromium)';
            return origGetParameter.call(this, parameter);
        }, 'getParameter');

        var origGetSupportedExtensions = proto.getSupportedExtensions;
        if (typeof origGetSupportedExtensions === 'function') {
            defineMethod(proto, 'getSupportedExtensions', function () {
                return WEBGL_EXTENSIONS.slice();
            }, 'getSupportedExtensions');
        }
    }
    try { patchWebGL(window.WebGLRenderingContext && window.WebGLRenderingContext.prototype); } catch (e) {}
    try { patchWebGL(window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype); } catch (e) {}

    // ---- AudioContext 指纹噪声 ----------------------------------------------
    // 常见做法是用 OfflineAudioContext + DynamicsCompressor 渲染后读浮点序列，
    // 或直接读 AnalyserNode 频谱。两者都加一个由种子决定的极小扰动。
    function audioNoise(index) {
        var value = __AUDIO_SEED__ ^ Math.imul((index | 0) + 1, 2654435761);
        value = Math.imul(value ^ (value >>> 15), 2246822519);
        value = value ^ (value >>> 13);
        return (((value >>> 0) % 2001) - 1000) / 1e6;
    }

    try {
        if (window.AnalyserNode && window.AnalyserNode.prototype) {
            var origFrequencyData = window.AnalyserNode.prototype.getFloatFrequencyData;
            if (typeof origFrequencyData === 'function') {
                defineMethod(window.AnalyserNode.prototype, 'getFloatFrequencyData', function (array) {
                    var result = origFrequencyData.call(this, array);
                    if (array && array.length) {
                        for (var i = 0; i < array.length; i++) {
                            array[i] = array[i] + audioNoise(i);
                        }
                    }
                    return result;
                }, 'getFloatFrequencyData');
            }
        }
    } catch (e) {}

    try {
        if (window.AudioBuffer && window.AudioBuffer.prototype) {
            var origGetChannelData = window.AudioBuffer.prototype.getChannelData;
            if (typeof origGetChannelData === 'function') {
                defineMethod(window.AudioBuffer.prototype, 'getChannelData', function () {
                    var data = origGetChannelData.apply(this, arguments);
                    if (data && data.length) {
                        for (var i = 0; i < data.length; i += 71) {
                            data[i] = data[i] + audioNoise(i);
                        }
                    }
                    return data;
                }, 'getChannelData');
            }
        }
    } catch (e) {}

    // ---- 权限与通知一致性 ----------------------------------------------------
    // 自动化环境下 notification 权限经常和 Permissions.query 结果对不上，这里对齐。
    try {
        if (window.Notification) {
            define(window.Notification, 'permission', 'default');
        }
    } catch (e) {}
    try {
        if (window.Permissions && window.Permissions.prototype) {
            var origQuery = window.Permissions.prototype.query;
            defineMethod(window.Permissions.prototype, 'query', function (parameters) {
                if (parameters && parameters.name === 'notifications') {
                    return Promise.resolve({ name: 'notifications', state: 'default', onchange: null });
                }
                return origQuery.call(this, parameters);
            }, 'query');
        }
    } catch (e) {}

    // ---- window.chrome 兜底 --------------------------------------------------
    // 正常 Chrome 本来就有；这里只在缺失时补上（headless / 特殊启动参数下会丢）。
    try {
        if (!window.chrome) {
            Object.defineProperty(window, 'chrome', { value: {}, writable: true, configurable: true });
        }
        if (window.chrome && !window.chrome.runtime) {
            window.chrome.runtime = {
                OnInstalledReason: {
                    CHROME_UPDATE: 'chrome_update',
                    INSTALL: 'install',
                    SHARED_MODULE_UPDATE: 'shared_module_update',
                    UPDATE: 'update',
                },
                PlatformArch: { ARM: 'arm', ARM64: 'arm64', X86_32: 'x86-32', X86_64: 'x86-64' },
                PlatformOs: {
                    ANDROID: 'android', CROS: 'cros', LINUX: 'linux',
                    MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win',
                },
            };
        }
        if (window.chrome && !window.chrome.csi) {
            window.chrome.csi = register(function csi() {
                var now = Date.now();
                return { onloadT: now, startE: now, pageT: 1000, tran: 15 };
            }, 'csi');
        }
        if (window.chrome && !window.chrome.loadTimes) {
            window.chrome.loadTimes = register(function loadTimes() {
                var now = Date.now() / 1000;
                return {
                    commitLoadTime: now, connectionInfo: 'h2',
                    finishDocumentLoadTime: now, finishLoadTime: now,
                    firstPaintAfterLoadTime: 0, firstPaintTime: now,
                    navigationType: 'Other', npnNegotiatedProtocol: 'h2',
                    requestTime: now, startLoadTime: now,
                    wasAlternateProtocolAvailable: false,
                    wasFetchedViaSpdy: true, wasNpnNegotiated: true,
                };
            }, 'loadTimes');
        }
    } catch (e) {}
})();
"""


def build_fingerprint_script(identity: BrowserIdentity) -> str:
    """生成随浏览器扩展注入的 JS：document_start / MAIN world 执行。

    覆盖 WebGL、Canvas、AudioContext、navigator 系列字段，并把所有 patch 的
    ``Function.prototype.toString`` 伪装成原生实现。写法上和仓库里已有的
    turnstile_patch 扩展保持一致。
    """
    replacements = {
        "__LANGUAGES__": json.dumps(list(identity.languages or ("en-US", "en"))),
        "__HARDWARE_CONCURRENCY__": str(int(identity.hardware_concurrency)),
        "__DEVICE_MEMORY__": str(int(identity.device_memory)),
        "__PLATFORM__": json.dumps(identity.platform or "MacIntel"),
        "__CANVAS_SEED__": str(int(identity.canvas_seed)),
        "__AUDIO_SEED__": str(int(identity.audio_seed)),
        "__GPU_VENDOR__": json.dumps(identity.gpu_vendor),
        "__GPU_RENDERER__": json.dumps(identity.gpu_renderer),
        "__WEBGL_EXTENSIONS__": json.dumps(list(identity.webgl_extensions or ())),
    }
    script = _SCRIPT_TEMPLATE
    for placeholder, value in replacements.items():
        script = script.replace(placeholder, value)
    return script.strip()


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
