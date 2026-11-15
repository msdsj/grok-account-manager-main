"""注册环境与使用环境一致性相关的测试。

覆盖三类问题：
1. 指纹脚本不再凭空造出真实 Chrome 不存在的属性，也不再自相矛盾；
2. 网关侧的请求头必须复述注册时的浏览器（UA / Client Hints / 语言）；
3. 设备标识必须账号级稳定，绝不能每次请求都换。
"""

import tempfile
import unittest
from pathlib import Path

from grok_account_manager.core import browser_identity
from grok_account_manager.core.consistency import (
    CRITICAL,
    check_credential_consistency,
    summarize_credential,
)
from grok_account_manager.core.fingerprint import (
    BrowserIdentity,
    build_fingerprint_script,
    random_identity,
)
from grok_account_manager.providers.gpt.gateway import upstream


MAC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)


def _identity(**overrides) -> dict:
    base = {
        "user_agent": MAC_UA,
        "sec_ch_ua": '"Not-A.Brand";v="99", "Google Chrome";v="157", "Chromium";v="157"',
        "sec_ch_ua_platform": "macOS",
        "sec_ch_ua_arch": "arm",
        "sec_ch_ua_bitness": "64",
        "sec_ch_ua_mobile": "?0",
        "accept_language": "en-US,en;q=0.9",
        "navigator_language": "en-US",
        "navigator_languages": ["en-US", "en"],
        "hardware_concurrency": 10,
        "device_memory": 16,
        "timezone": "Asia/Shanghai",
        "timezone_offset_minutes": -480,
        "device_id": "11111111-2222-3333-4444-555555555555",
    }
    base.update(overrides)
    return base


class FingerprintScriptTests(unittest.TestCase):
    def test_script_does_not_invent_chrome_runtime(self) -> None:
        """没有安装扩展的页面上 chrome.runtime 本来就是 undefined。

        旧实现会凭空补一个带 PlatformArch/PlatformOs 的 chrome.runtime，
        反而制造了「真实 Chrome 不存在」的属性。
        """
        script = build_fingerprint_script(random_identity("darwin", lang="en-US"))

        self.assertNotIn("chrome.runtime =", script)
        self.assertNotIn("OnInstalledReason", script)
        self.assertNotIn("PlatformArch", script)

    def test_real_device_script_changes_nothing(self) -> None:
        """real_device 模式只保留插件兜底，不再改写任何硬件属性。"""
        identity = random_identity("darwin", lang="en-US", real_device=True)
        script = build_fingerprint_script(identity)

        self.assertNotIn("pixelNoise", script)
        self.assertNotIn("audioNoise", script)
        self.assertNotIn("hardwareConcurrency", script)
        self.assertNotIn("deviceMemory", script)
        # 插件兜底仍然保留（正常桌面 Chrome 用不到，属于防御性补丁）。
        self.assertIn("navigator.plugins", script)

    def test_webgl_extension_list_stays_consistent(self) -> None:
        """getSupportedExtensions 与 getExtension 必须给出同一个答案。"""
        script = build_fingerprint_script(random_identity("darwin", lang="en-US"))

        self.assertIn("getExtension", script)
        self.assertIn("getAdvertised", script)
        # 扩展列表必须延后到真正使用时再取，不能在 document_start 就取。
        self.assertIn("advertised = null", script)

    def test_random_identity_keeps_languages_and_platform(self) -> None:
        identity = random_identity("darwin", lang="en-US")
        self.assertEqual(identity.languages[0], "en-US")
        self.assertEqual(identity.platform, "MacIntel")


class BrowserIdentityTests(unittest.TestCase):
    def test_sec_ch_ua_follows_brands_order(self) -> None:
        brands = [
            {"brand": "Not-A.Brand", "version": "99"},
            {"brand": "Google Chrome", "version": "157"},
            {"brand": "Chromium", "version": "157"},
        ]
        self.assertEqual(
            browser_identity.build_sec_ch_ua(brands),
            '"Not-A.Brand";v="99", "Google Chrome";v="157", "Chromium";v="157"',
        )

    def test_accept_language_uses_chrome_q_value_rules(self) -> None:
        self.assertEqual(
            browser_identity.chrome_accept_language(["en-US", "en"]), "en-US,en;q=0.9"
        )
        self.assertEqual(
            browser_identity.chrome_accept_language(["zh-CN", "zh", "en"]),
            "zh-CN,zh;q=0.9,en;q=0.8",
        )

    def test_device_id_extracted_from_oai_did_cookie(self) -> None:
        cookies = [
            {"name": "__Secure-next-auth.session-token", "value": "abc"},
            {"name": "oai-did", "value": "device-123"},
        ]
        self.assertEqual(browser_identity.extract_device_id(cookies), "device-123")

    def test_identity_headers_mirror_the_browser(self) -> None:
        headers = browser_identity.identity_headers(_identity())
        self.assertEqual(headers["Sec-CH-UA-Platform"], '"macOS"')
        self.assertEqual(headers["Sec-CH-UA-Arch"], '"arm"')
        self.assertEqual(headers["Accept-Language"], "en-US,en;q=0.9")
        self.assertIn("Accept-CH", headers)


class GatewayHeaderTests(unittest.TestCase):
    def test_headers_use_registered_browser_not_a_hardcoded_edge(self) -> None:
        """旧实现写死了「Windows 上的 Edge 143」，与注册环境完全无关。"""
        credential = {"access_token": "tok", "browser_identity": _identity()}
        headers = upstream._base_headers(credential)

        self.assertEqual(headers["User-Agent"], MAC_UA)
        self.assertNotIn("Edg/", headers["User-Agent"])
        self.assertEqual(headers["Sec-CH-UA-Platform"], '"macOS"')
        self.assertNotEqual(headers["Sec-CH-UA-Platform"], '"Windows"')
        self.assertEqual(headers["Accept-Language"], "en-US,en;q=0.9")
        self.assertIn("Accept", headers)

    def test_device_id_is_stable_across_requests(self) -> None:
        """同一个账号的 OAI-Device-Id 必须跨请求保持不变。

        两种来源都要稳定：
        - 注册时浏览器下发的 ``oai-did``（直接沿用）；
        - 老账号没有该字段时，用账号自身信息派生（同样跨请求稳定）。
        """
        credential = {"access_token": "tok", "browser_identity": _identity()}

        first = upstream._base_headers(credential)["OAI-Device-Id"]
        second = upstream._base_headers(dict(credential))["OAI-Device-Id"]
        self.assertEqual(first, second)
        # 浏览器下发的设备 id 优先沿用。
        self.assertEqual(first, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(len(first), 36)

        # 没有落库设备 id 的老账号：派生值也必须稳定，且依赖账号本身。
        legacy_a = upstream._base_headers({"access_token": "tok"})["OAI-Device-Id"]
        legacy_b = upstream._base_headers({"access_token": "tok"})["OAI-Device-Id"]
        legacy_c = upstream._base_headers({"access_token": "other"})["OAI-Device-Id"]
        self.assertEqual(legacy_a, legacy_b)
        self.assertNotEqual(legacy_a, legacy_c)
        self.assertEqual(len(legacy_a), 36)

    def test_session_id_is_stable_across_requests(self) -> None:
        credential = {"access_token": "tok", "browser_identity": _identity()}
        first = upstream._base_headers(credential)["OAI-Session-Id"]
        second = upstream._base_headers(dict(credential))["OAI-Session-Id"]
        self.assertEqual(first, second)

    def test_pow_token_reports_the_real_device(self) -> None:
        """PoW 上报的屏幕 / 核数 / 时区必须来自注册时的浏览器。"""
        device = {
            "screen": {"width": 1800, "height": 1169},
            "hardware_concurrency": 10,
            "navigator_language": "en-US",
            "accept_language": "en-US,en;q=0.9",
            "timezone_offset_minutes": -480,
        }
        from grok_account_manager.providers.gpt.gateway.pow import _build_pow_config

        config = _build_pow_config(MAC_UA, device=device)
        self.assertEqual(config[0], 1800 + 1169)
        self.assertEqual(config[4], MAC_UA)
        self.assertEqual(config[7], "en-US")
        self.assertEqual(config[8], "en-US,en;q=0.9")
        self.assertEqual(config[16], 10)


class ConsistencyCheckerTests(unittest.TestCase):
    def test_consistent_credential_has_no_critical_issues(self) -> None:
        credential = {
            "access_token": "tok",
            "browser_identity": _identity(),
            "user_agent": MAC_UA,
        }
        issues = check_credential_consistency(credential)
        critical = [item for item in issues if item.severity == CRITICAL]
        self.assertEqual(critical, [], f"仍有严重不一致: {[i.message for i in critical]}")

    def test_missing_identity_is_reported(self) -> None:
        issues = check_credential_consistency({"access_token": "tok"})
        self.assertTrue(any(item.dimension == "user-agent" for item in issues))

    def test_platform_mismatch_is_reported(self) -> None:
        credential = {
            "access_token": "tok",
            "user_agent": MAC_UA,
            "browser_identity": _identity(sec_ch_ua_platform="Windows"),
        }
        issues = check_credential_consistency(credential)
        self.assertTrue(
            any(item.dimension == "sec-ch-ua-platform" for item in issues),
            [i.dimension for i in issues],
        )

    def test_language_mismatch_is_reported(self) -> None:
        credential = {
            "access_token": "tok",
            "user_agent": MAC_UA,
            "browser_identity": _identity(accept_language="zh-CN,zh;q=0.9"),
        }
        issues = check_credential_consistency(credential)
        self.assertTrue(
            any(item.dimension == "accept-language" for item in issues),
            [i.dimension for i in issues],
        )

    def test_summary_exposes_registration_environment(self) -> None:
        credential = {
            "access_token": "tok",
            "browser_identity": _identity(),
            "user_agent": MAC_UA,
        }
        summary = summarize_credential(credential)
        self.assertEqual(summary["注册 UA"], MAC_UA)
        self.assertEqual(summary["注册 UA 家族"], "chrome")
        self.assertEqual(summary["Sec-CH-UA-Platform"], '"macOS"')
        self.assertEqual(summary["CPU 核数"], 10)


class CredentialPersistenceTests(unittest.TestCase):
    """浏览器身份必须能随凭证一起落库，否则网关侧拿不到同源信息。"""

    def test_identity_survives_database_round_trip(self) -> None:
        from unittest import mock

        from grok_account_manager.api.services.accounts import account_from_credential
        from grok_account_manager.core import database as db

        credential = {
            "email": "roundtrip@example.com",
            "provider": "chatgpt",
            "access_token": "AT",
            "accessToken": "AT",
            "refresh_token": "RT",
            "browser_identity": _identity(),
            "user_agent": MAC_UA,
            "client_version": "prod-test",
            "client_build_number": "6708908",
        }
        credential["oai_device_id"] = upstream.stable_device_id(credential)
        credential["oai_session_id"] = upstream.stable_session_id(credential)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "roundtrip.db"
            with mock.patch.object(db, "DB_PATH", db_path):
                account = account_from_credential(
                    credential,
                    Path("database"),
                    export_key="db:chatgpt:roundtrip",
                    provider="chatgpt",
                )
                db.upsert_account(account, credential, Path("database"), 0)

                refs = db.list_credential_refs("chatgpt")
                stored = next(
                    item for item in refs if item["export_key"] == "db:chatgpt:roundtrip"
                )

        stored_credential = stored["credential"]
        self.assertIn("browser_identity", stored_credential)
        self.assertEqual(
            stored_credential["browser_identity"]["sec_ch_ua_platform"], "macOS"
        )
        self.assertEqual(stored_credential["user_agent"], MAC_UA)
        self.assertEqual(
            stored_credential["oai_device_id"], credential["oai_device_id"]
        )

        # 落库后的凭证重新计算请求头，依然必须与注册环境同源。
        issues = check_credential_consistency(stored_credential)
        critical = [item for item in issues if item.severity == CRITICAL]
        self.assertEqual(critical, [], [item.message for item in critical])


if __name__ == "__main__":
    unittest.main()
