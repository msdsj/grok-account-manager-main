"""ChatGPT 注册工具函数：Sentinel Token 生成、PoW 求解、Turnstile 处理。

借鉴自 any-auto-register 和 chatgpt2api 项目的实现。
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import time
import uuid
from typing import Any


class SentinelTokenGenerator:
    """Sentinel Token 生成器（支持 PoW - Proof of Work）。

    用于 OpenAI 注册、登录等需要反机器人验证的流程。
    """

    MAX_ATTEMPTS = 500_000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, user_agent: str):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        """FNV-1a 32位哈希算法实现。"""
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    @staticmethod
    def _b64(data) -> str:
        """Base64 编码 JSON 数据。"""
        return base64.b64encode(
            json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).decode("ascii")

    def _get_config(self) -> list:
        """生成 Sentinel 配置数组。"""
        perf_now = random.uniform(1000, 50000)
        navigator_key = random.choice([
            "hardwareConcurrency−32",
            "webdriver−false",
            "vendor−Google Inc.",
            "language−en-US",
            "onLine−true",
            "cookieEnabled−true",
        ])
        window_key = random.choice([
            "location", "document", "window", "localStorage",
            "performance", "navigator", "screen"
        ])
        return [
            "1920x1080",  # 屏幕分辨率
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),  # 会被 nonce 覆盖
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            "en-US",
            random.random(),  # 会被时间偏移覆盖
            navigator_key,
            window_key,
            random.choice(["Object", "Function", "Array"]),
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16, 24, 32]),  # CPU 核心数
            time.time() * 1000 - perf_now,
        ]

    def generate_requirements_token(self) -> str:
        """生成 Sentinel requirements token（初始请求）。"""
        config = self._get_config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(config)

    def generate_token(self, seed: str, difficulty: str) -> str:
        """生成 Sentinel PoW token（解决工作量证明）。

        Args:
            seed: 服务器提供的种子
            difficulty: 难度值（十六进制字符串）

        Returns:
            完整的 PoW token
        """
        start = time.time()
        config = self._get_config()
        difficulty = str(difficulty or "0")

        for i in range(self.MAX_ATTEMPTS):
            config[3] = i  # nonce
            config[9] = round((time.time() - start) * 1000)  # 时间偏移
            payload = self._b64(config)
            digest = self._fnv1a_32(seed + payload)

            # 检查哈希是否满足难度要求
            if digest[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"

        # 超过最大尝试次数，返回错误标记
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


def solve_turnstile_dx(dx: str, p: str) -> str | None:
    """解密 Turnstile DX token。

    Args:
        dx: base64 编码的加密数据
        p: XOR 密钥（来自 sentinel requirements token）

    Returns:
        解密后的 base64 token，失败返回 None
    """
    try:
        decoded = base64.b64decode(dx).decode()
        token_list = json.loads(_xor_string(decoded, p))
    except Exception:
        return None

    # 简化的 VM 实现 - 只处理关键操作
    process_map: dict[Any, Any] = {}
    result = ""

    def func_2(e: float, t: Any) -> None:
        process_map[e] = t

    def func_3(e: str) -> None:
        nonlocal result
        result = base64.b64encode(e.encode()).decode()

    def func_8(e: float, t: float) -> None:
        process_map[e] = process_map.get(t)

    process_map.update({
        2: func_2,
        3: func_3,
        8: func_8,
        9: token_list,
    })

    for token in token_list:
        try:
            fn = process_map.get(token[0])
            if callable(fn):
                fn(*token[1:])
        except Exception:
            continue

    return result or None


def _xor_string(text: str, key: str) -> str:
    """XOR 字符串加密/解密。"""
    if not key:
        return text
    return "".join(chr(ord(ch) ^ ord(key[i % len(key)])) for i, ch in enumerate(text))


def generate_datadog_trace_headers() -> dict[str, str]:
    """生成 Datadog 追踪头（OpenAI 后端需要）。"""
    import secrets
    trace_hex = secrets.token_hex(8).rjust(16, "0")
    parent_hex = secrets.token_hex(8).rjust(16, "0")
    trace_id = str(int(trace_hex, 16))
    parent_id = str(int(parent_hex, 16))
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def generate_pkce() -> tuple[str, str]:
    """生成 OAuth PKCE code_verifier 和 code_challenge。

    Returns:
        (code_verifier, code_challenge) 元组
    """
    import secrets
    import hashlib
    import base64

    # 生成 code_verifier (43-128字符)
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8').rstrip('=')

    # 生成 code_challenge (SHA256)
    challenge_bytes = hashlib.sha256(code_verifier.encode('utf-8')).digest()
    code_challenge = base64.urlsafe_b64encode(challenge_bytes).decode('utf-8').rstrip('=')

    return code_verifier, code_challenge


# OpenAI 注册相关常量
OPENAI_AUTH = "https://auth.openai.com"
CHATGPT_APP = "https://chatgpt.com"
SENTINEL_BASE = "https://sentinel.openai.com"
SENTINEL_REQ_URL = f"{SENTINEL_BASE}/backend-api/sentinel/req"
SENTINEL_FRAME_URL = f"{SENTINEL_BASE}/backend-api/sentinel/frame.html"

# OpenAI API Endpoints
OPENAI_API_ENDPOINTS = {
    "signup": f"{OPENAI_AUTH}/api/accounts/authorize/continue",
    "register": f"{OPENAI_AUTH}/api/accounts/register",
    "send_otp": f"{OPENAI_AUTH}/api/accounts/email_otp/send",
    "validate_otp": f"{OPENAI_AUTH}/api/accounts/email_otp/validate",
    "create_account": f"{OPENAI_AUTH}/api/accounts/create",
    "sentinel": SENTINEL_REQ_URL,
}

# Codex CLI OAuth 配置（用于获取 refresh_token）
CODEX_CLIENT_ID = "pdlliX4V5LqH3F16CWBR1NnlsVNw8axB"
CODEX_REDIRECT_URI = "http://localhost:54321"
CODEX_SCOPE = "openid email profile offline_access model.request model.read organization.read organization.write"

# 默认 User-Agent
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
