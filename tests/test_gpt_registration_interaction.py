"""ChatGPT 注册流程的交互健壮性测试。

重点覆盖两类已经踩过的坑：

1. **点击必须原子完成**——查找按钮和点击一旦拆成两次 run_js，页面一旦跳转，
   元素引用就指向被销毁的节点，点击永远发不出去（表现为「没点到注册按钮」）。
2. **要等前端接管元素**——React hydration 完成之前，DOM 里有按钮但没有事件
   处理器，点击是空操作；并发时 hydration 变慢，这个坑更容易踩。
"""

import unittest
from unittest import mock

from grok_account_manager.providers.gpt import humanize, registration


class FakePage:
    """记录每一次 run_js，并按队列返回预设结果。"""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def run_js(self, script, *args, as_expr=False):
        self.calls.append({"script": script, "as_expr": as_expr})
        if self.responses:
            return self.responses.pop(0)
        return None

    def refresh(self):
        return None


class FakeSession:
    def __init__(self, responses=None):
        self.page = FakePage(responses)

    def refresh_page(self):
        return self.page


class AtomicClickTests(unittest.TestCase):
    def test_signup_step_clicks_in_the_same_script_that_finds_the_button(self) -> None:
        """查找和点击必须在同一次 run_js 里完成。"""
        session = FakeSession(
            [
                {"status": "clicked_signup", "diagnostics": {}},
                {"status": "ready", "diagnostics": {}},
            ]
        )
        with mock.patch.object(registration.time, "sleep", lambda *_: None), \
             mock.patch.object(humanize, "human_pause", lambda *_, **__: 0.0):
            result = registration._wait_and_click_signup_button(session, timeout=5)

        self.assertTrue(result)
        self.assertGreaterEqual(len(session.page.calls), 2)

        first_script = session.page.calls[0]["script"]
        # 脚本里必须自带点击实现。
        self.assertIn("__humanClick", first_script)
        self.assertIn("pointerdown", first_script)
        # 不能再出现「先标记、下一轮再点」的模式。
        self.assertNotIn("__grokClickTarget", first_script)
        self.assertNotIn("window.__grok", first_script)

    def test_click_first_matching_is_a_single_call(self) -> None:
        """_click_first_matching 只发一次请求，且请求里同时包含定位与点击。"""
        page = FakePage(["clicked"])
        status = registration._click_first_matching(page, "document.querySelector('button')")

        self.assertEqual(status, "clicked")
        self.assertEqual(len(page.calls), 1)
        script = page.calls[0]["script"]
        self.assertIn("document.querySelector('button')", script)
        self.assertIn("__humanClick", script)

    def test_humanize_click_script_has_no_cross_call_state(self) -> None:
        """点击脚本里不允许出现任何跨 run_js 的 window 引用。"""
        self.assertNotIn("window.__", humanize.CLICK_TARGET_JS)
        self.assertIn("pointermove", humanize.CLICK_TARGET_JS)
        self.assertIn("mousedown", humanize.CLICK_TARGET_JS)
        self.assertIn("mouseup", humanize.CLICK_TARGET_JS)


class HydrationGateTests(unittest.TestCase):
    def test_waits_for_hydration_before_clicking(self) -> None:
        """页面还没接管元素时不能点，要先等。"""
        session = FakeSession(
            [
                {"status": "not-hydrated", "diagnostics": {}},
                {"status": "not-hydrated", "diagnostics": {}},
                {"status": "ready", "diagnostics": {}},
            ]
        )
        with mock.patch.object(registration.time, "sleep", lambda *_: None), \
             mock.patch.object(humanize, "human_pause", lambda *_, **__: 0.0):
            result = registration._wait_and_click_signup_button(session, timeout=5)

        self.assertTrue(result)
        self.assertEqual(len(session.page.calls), 3)
        # 前两次必须处于「等 hydration」状态，不能发点击。
        self.assertIn("'not-hydrated'", session.page.calls[0]["script"])
        self.assertIn("'not-hydrated'", session.page.calls[1]["script"])

    def test_forces_click_after_repeated_not_hydrated(self) -> None:
        """连续几次都没接管，就不再死等，直接强制点一次。"""
        session = FakeSession(
            [
                {"status": "not-hydrated", "diagnostics": {}},
                {"status": "not-hydrated", "diagnostics": {}},
                {"status": "not-hydrated", "diagnostics": {}},
                {"status": "ready", "diagnostics": {}},
            ]
        )
        with mock.patch.object(registration.time, "sleep", lambda *_: None), \
             mock.patch.object(humanize, "human_pause", lambda *_, **__: 0.0):
            registration._wait_and_click_signup_button(session, timeout=5)

        # 前 3 轮强制位为 false，第 4 轮起为 true。
        self.assertIn("needsHydration(signupButton)", session.page.calls[0]["script"])
        self.assertIn("__FORCE__", registration._SIGNUP_CLICK_SCRIPT)
        # 第 4 次调用时脚本里的 __FORCE__ 已被替换成 true。
        self.assertNotIn("__FORCE__", session.page.calls[3]["script"])
        self.assertIn("if (true) return false;", session.page.calls[3]["script"])

    def test_diagnostics_are_reported_on_timeout(self) -> None:
        """超时要把页面诊断信息带出来，方便定位。"""
        session = FakeSession(
            [{"status": "waiting", "diagnostics": {"viewport": "600x490"}} for _ in range(3)]
        )
        with mock.patch.object(registration.time, "sleep", lambda *_: None), \
             mock.patch.object(humanize, "human_pause", lambda *_, **__: 0.0):
            with self.assertRaises(Exception) as ctx:
                registration._wait_and_click_signup_button(session, timeout=0.01)
        self.assertIn("600x490", str(ctx.exception))


class ConcurrencyTimeoutTests(unittest.TestCase):
    def test_timeout_scales_with_worker_count(self) -> None:
        """并发越高，等注册按钮的时间要越长。"""
        provider = registration.ChatGPTProvider()

        provider.window_count = 1
        single = provider._signup_button_timeout()
        provider.window_count = 3
        three = provider._signup_button_timeout()
        provider.window_count = 10
        ten = provider._signup_button_timeout()

        self.assertEqual(single, 60)
        self.assertGreater(three, single)
        self.assertGreater(ten, three)
        self.assertLessEqual(ten, 180)

    def test_missing_worker_count_falls_back_to_single(self) -> None:
        provider = registration.ChatGPTProvider()
        provider.window_count = 0
        self.assertEqual(provider._signup_button_timeout(), 60)


class MatchersTests(unittest.TestCase):
    def test_continue_matcher_excludes_third_party_buttons(self) -> None:
        """第三方登录按钮不能被当成提交按钮。"""
        self.assertIn("thirdPartyKeywords", registration._CONTINUE_BUTTON_MATCHER)
        self.assertIn("google", registration._CONTINUE_BUTTON_MATCHER)

    def test_matchers_return_element_or_null(self) -> None:
        for matcher in (
            registration._CONTINUE_BUTTON_MATCHER,
            registration._OTP_CONTINUE_MATCHER,
            registration._PROFILE_CONTINUE_MATCHER,
        ):
            self.assertIn("return", matcher)
            self.assertIn("null", matcher)


if __name__ == "__main__":
    unittest.main()
