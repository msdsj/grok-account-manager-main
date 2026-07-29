"""通用临时邮箱  + 验证码工具（DuckMail）。

虽然历史上叫 openai_register.py，但实际是 provider-agnostic 的邮箱 OTP 工具，
被 grok / openai / 任何需要邮箱验证码的注册流程复用。
"""

import os
import re
import secrets
import string
import time
from email.utils import parseaddr

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("DUCKMAIL_BASE_URL", "https://api.duckmail.sbs").rstrip("/")
DUCKMAIL_API_KEY = os.getenv("DUCKMAIL_API_KEY", "")
DUCKMAIL_DOMAIN = os.getenv("DUCKMAIL_DOMAIN", "@msdsj.cyou")

_CODE_TOKEN_PATTERN = r"([A-Z0-9]{3}-[A-Z0-9]{3}|\d{6})"
_VERIFICATION_PATTERNS = (
    rf"(?:confirmation|verification|security|one[-\s]?time)\s*(?:code|pin|passcode)[\s\S]{{0,80}}?{_CODE_TOKEN_PATTERN}",
    rf"(?:code|pin|passcode)\s*(?:is|:|：)[\s\S]{{0,40}}?{_CODE_TOKEN_PATTERN}",
    rf"code\s+below\s+to\s+(?:validate|verify)[\s\S]{{0,200}}?{_CODE_TOKEN_PATTERN}",
    rf"(?:验证码|确认码|校验码|一次性密码)[：:\s为是]*{_CODE_TOKEN_PATTERN}",
    rf"{_CODE_TOKEN_PATTERN}[\s\S]{{0,80}}?(?:is\s+your\s+(?:confirmation|verification|security)?\s*(?:code|pin|passcode)|(?:confirmation|verification)\s*code|作为您的验证码)",
)


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


def _is_trusted_xai_sender(sender: str) -> bool:
    address = parseaddr(str(sender or ""))[1].strip().lower()
    if not address and "@" in str(sender or ""):
        address = str(sender).strip().lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    return domain in {"x.ai", "grok.com"} or domain.endswith((".x.ai", ".grok.com"))


def _extract_code(subject, text, html_text="", sender=""):
    """只从明确验证码上下文或可信 xAI 邮件中提取 OTP。"""
    text = text or ""
    html_text = html_text or ""
    subject = subject or ""
    full_text = f"{subject}\n{text}\n{html_text}"

    for pat in _VERIFICATION_PATTERNS:
        m = re.search(pat, full_text, re.IGNORECASE | re.DOTALL)
        if m:
            code = m.group(1)
            if code:
                return code.upper() if "-" in code else code

    if _is_trusted_xai_sender(sender):
        m = re.search(rf"\b{_CODE_TOKEN_PATTERN}\b", full_text, re.IGNORECASE)
        if m:
            code = m.group(1)
            return code.upper() if "-" in code else code
    return None


def extract_verification_code(subject="", text="", html_text="", sender=""):
    """公开的验证码提取入口，供不同邮箱源复用。"""
    return _extract_code(subject, text, html_text, sender)


def _is_verification_email(subject, sender):
    """判断是否为验证码邮件。"""
    if _is_trusted_xai_sender(sender):
        return True
    keywords = ["verification code", "confirmation code", "security code", "验证码", "确认码", "校验码"]
    text = f"{subject} {sender}".lower()
    return any(k in text for k in keywords)


def _interruptible_sleep(seconds: float, stop_event=None) -> bool:
    """休眠最多 seconds 秒；若期间停止信号被设置则提前返回 True。"""
    if not stop_event:
        time.sleep(seconds)
        return False
    deadline = time.monotonic() + seconds
    while True:
        if stop_event.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.2, remaining))


def _duckmail_get_with_retry(url, headers, deadline, interval, stop_event=None):
    while time.monotonic() < deadline:
        if stop_event and stop_event.is_set():
            return None
        remaining = deadline - time.monotonic()
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=max(0.1, min(30.0, remaining)),
            )
        except (requests.Timeout, requests.ConnectionError):
            print("[警告] DuckMail 请求暂时失败，稍后重试")
        else:
            if response.status_code != 429 and response.status_code < 500:
                response.raise_for_status()
                return response
            print(f"[警告] DuckMail 请求返回 {response.status_code}，稍后重试")

        sleep_for = min(interval, max(0.0, deadline - time.monotonic()))
        if _interruptible_sleep(sleep_for, stop_event):
            return None
    return None


def get_oai_code(
    token,
    email,
    include_seen=False,
    stop_event=None,
    timeout=180,
    interval=3,
):
    try:
        headers = {"Authorization": f"Bearer {token}"}
        timeout = max(0.0, float(timeout))
        interval = max(0.0, float(interval))
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if stop_event and stop_event.is_set():
                print("[*] 收到停止信号，取消验证码轮询")
                return None
            r = _duckmail_get_with_retry(
                f"{BASE_URL}/messages",
                headers,
                deadline,
                interval,
                stop_event,
            )
            if r is None:
                break
            try:
                messages = r.json().get("hydra:member", [])
            except (AttributeError, ValueError):
                print("[警告] DuckMail 邮件列表响应无效，稍后重试")
                if _interruptible_sleep(interval, stop_event):
                    return None
                continue
            if not isinstance(messages, list):
                messages = []

            if not include_seen:
                messages = [m for m in messages if isinstance(m, dict) and not m.get("seen")]

            # 按创建时间倒序，优先处理最新邮件
            messages.sort(key=lambda m: m.get("createdAt", ""), reverse=True)

            # 优先检查看起来像验证码的邮件
            verification_msgs = [
                m
                for m in messages
                if isinstance(m, dict)
                and _is_verification_email(m.get("subject", ""), m.get("from", ""))
            ]
            other_msgs = [m for m in messages if m not in verification_msgs]
            ordered_msgs = verification_msgs + other_msgs

            for msg in ordered_msgs:
                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("id")
                if not msg_id:
                    continue

                # 获取邮件详情
                r = _duckmail_get_with_retry(
                    f"{BASE_URL}/messages/{msg_id}",
                    headers,
                    deadline,
                    interval,
                    stop_event,
                )
                if r is None:
                    break
                try:
                    detail = r.json()
                except ValueError:
                    continue
                if not isinstance(detail, dict):
                    continue

                subject = detail.get("subject", "")
                text = detail.get("text", "")
                html = detail.get("html", [])
                html_content = " ".join(html) if isinstance(html, list) else str(html)
                sender_data = detail.get("from") or {}
                sender = sender_data.get("address", "") if isinstance(sender_data, dict) else str(sender_data)

                code = _extract_code(subject, text, html_content, sender)
                if code:
                    # xAI 验证码为 WVB-8OE 格式，OTP 输入框通常只需 6 位字母数字，去掉连字符
                    if "-" in code:
                        code = code.replace("-", "")
                    print("[*] 已获取邮箱验证码")
                    # 标记为已读，避免重复提取
                    try:
                        requests.patch(f"{BASE_URL}/messages/{msg_id}", headers={**headers, "Content-Type": "application/merge-patch+json"}, json={"seen": True}, timeout=10)
                    except Exception:
                        pass
                    return code

            if _interruptible_sleep(interval, stop_event):
                print("[*] 收到停止信号，取消验证码轮询")
                return None

        print("[Error] 轮询超时，未获取到验证码")
        return None
    except Exception as e:
        print(f"[Error] 获取验证码失败: {e}")
        return None
