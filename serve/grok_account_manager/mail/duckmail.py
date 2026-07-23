"""通用临时邮箱  + 验证码工具（DuckMail）。

虽然历史上叫 openai_register.py，但实际是 provider-agnostic 的邮箱 OTP 工具，
被 grok / openai / 任何需要邮箱验证码的注册流程复用。
"""

import os
import re
import secrets
import string
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("DUCKMAIL_BASE_URL", "https://api.duckmail.sbs").rstrip("/")
DUCKMAIL_API_KEY = os.getenv("DUCKMAIL_API_KEY", "")
DUCKMAIL_DOMAIN = os.getenv("DUCKMAIL_DOMAIN", "@msdsj.cyou")


def _random_string(length=12):
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(length))


def get_email_and_token():
    try:
        if not DUCKMAIL_API_KEY:
            print("[Error] 未配置 DUCKMAIL_API_KEY 环境变量")
            return None, None

        # 1. 生成随机邮箱和密码
        local_part = _random_string(10)
        domain = DUCKMAIL_DOMAIN.lstrip("@")
        email = f"{local_part}@{domain}"
        password = _random_string(16)

        # 2. 创建账户（需要 API Key 认证）
        r = requests.post(
            f"{BASE_URL}/accounts",
            json={"address": email, "password": password},
            headers={"Authorization": f"Bearer {DUCKMAIL_API_KEY}"},
            timeout=30,
        )

        # 409 表示邮箱已存在，可以继续
        if r.status_code == 409:
            print(f"[*] 邮箱已存在，继续使用: {email}")
        elif r.status_code == 201:
            print(f"[*] 邮箱创建成功: {email}")
        else:
            print(f"[Error] 创建邮箱失败: {r.status_code} - {r.text}")
            return None, None

        # 3. 获取 token（不需要 API Key）
        r = requests.post(
            f"{BASE_URL}/token",
            json={"address": email, "password": password},
            timeout=30,
        )
        r.raise_for_status()
        token = r.json().get("token")
        if not token:
            print("[Error] 无法获取 DuckMail token")
            return None, None

        print(f"[*] Token 获取成功")
        return email, token
    except Exception as e:
        print(f"[Error] 获取邮箱失败: {e}")
        return None, None


def _extract_code(subject, text, html_text=""):
    """从邮件文本中提取验证码。优先匹配 xAI/常见服务的验证码格式。"""
    text = text or ""
    html_text = html_text or ""
    subject = subject or ""
    full_text = f"{subject}\n{text}\n{html_text}"

    # 1. xAI 主题自带验证码格式: EB5-XJA xAI confirmation code
    m = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b.*confirmation\s*code", subject, re.IGNORECASE)
    if m:
        return m.group(1).upper()

    # 2. 通用 xAI/OpenAI 验证码格式（字母数字混合）
    patterns = [
        # xAI 正文: code below to validate ... EB5-XJA
        r"code\s+below\s+to\s+validate.*?\n\s*([A-Z0-9]{3}-[A-Z0-9]{3})\s*\n",
        # 通用字母数字验证码: ABC-123, 123-ABC, A1B-2C3
        r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b",
        # verification code is 123456
        r"(?:verification\s*code|code\s*is)[:\s]+(\d{6})",
        # 中文验证码
        r"(?:验证码|代码|确认码)[:\s为]+(\d{6})",
        # code: 123456
        r"(?:code|验证码)[:\s]+(\d{6})",
        # 6位数字带上下文
        r"\b(\d{6})\b(?:\s*(?:is your|作为您的)\s*(?:verification\s*code|code|验证码))?",
    ]

    for pat in patterns:
        m = re.search(pat, full_text, re.IGNORECASE | re.DOTALL)
        if m:
            code = m.group(1)
            if code:
                return code.upper() if "-" in code else code

    # 3. 兜底：仅在纯文本中匹配 6 位数字，避开 HTML 颜色代码
    if text:
        for m in re.finditer(r"(?<!#)\b(\d{6})\b", text):
            code = m.group(1)
            if not code.startswith("20"):
                return code
    return None


def extract_verification_code(subject="", text="", html_text=""):
    """公开的验证码提取入口，供不同邮箱源复用。"""
    return _extract_code(subject, text, html_text)


def _is_verification_email(subject, sender):
    """判断是否为验证码邮件。"""
    keywords = ["verification", "code", "验证码", "确认码", "xai", "x.ai", "openai", "grok"]
    text = f"{subject} {sender}".lower()
    return any(k in text for k in keywords)


def get_oai_code(token, email, include_seen=False):
    try:
        headers = {"Authorization": f"Bearer {token}"}
        deadline = time.time() + 180  # 最多轮询 180 秒

        while time.time() < deadline:
            r = requests.get(f"{BASE_URL}/messages", headers=headers, timeout=30)
            r.raise_for_status()
            messages = r.json().get("hydra:member", [])

            if not include_seen:
                messages = [m for m in messages if not m.get("seen")]

            # 按创建时间倒序，优先处理最新邮件
            messages.sort(key=lambda m: m.get("createdAt", ""), reverse=True)

            # 优先检查看起来像验证码的邮件
            verification_msgs = [m for m in messages if _is_verification_email(m.get("subject", ""), "")]
            other_msgs = [m for m in messages if m not in verification_msgs]
            ordered_msgs = verification_msgs + other_msgs

            for msg in ordered_msgs:
                msg_id = msg.get("id")
                if not msg_id:
                    continue

                # 获取邮件详情
                r = requests.get(f"{BASE_URL}/messages/{msg_id}", headers=headers, timeout=30)
                r.raise_for_status()
                detail = r.json()

                subject = detail.get("subject", "")
                text = detail.get("text", "")
                html = detail.get("html", [])
                html_content = " ".join(html) if isinstance(html, list) else str(html)
                sender = detail.get("from", {}).get("address", "")

                code = _extract_code(subject, text, html_content)
                if code:
                    # xAI 验证码为 WVB-8OE 格式，OTP 输入框通常只需 6 位字母数字，去掉连字符
                    if "-" in code:
                        code = code.replace("-", "")
                    print(f"[*] 获取到验证码: {code}")
                    # 标记为已读，避免重复提取
                    try:
                        requests.patch(f"{BASE_URL}/messages/{msg_id}", headers={**headers, "Content-Type": "application/merge-patch+json"}, json={"seen": True}, timeout=10)
                    except Exception:
                        pass
                    return code
            time.sleep(3)

        print("[Error] 轮询超时，未获取到验证码")
        return None
    except Exception as e:
        print(f"[Error] 获取验证码失败: {e}")
        return None
