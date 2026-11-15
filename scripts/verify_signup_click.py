#!/usr/bin/env python3
"""注册按钮点击逻辑的端到端验证。

用一张本地 mock 页复刻 OpenAI 注册页的结构（顶栏 Log in / Sign up，
弹层里的 Continue with email，填完邮箱后的输入框），然后把
``registration._SIGNUP_CLICK_SCRIPT`` 原样跑进去，逐个断言：

1. 能找到「Sign up」按钮；
2. 点击真的触发了按钮的处理器（不是只派发了没人监听的事件）；
3. 弹层出现后能找到并点中「Continue with email」；
4. 邮箱输入框出现后判定为 ready；
5. 页面还没接管元素的按钮会被判成 not-hydrated（而不是傻点空）。

这样不用连 chatgpt.com 也能验证交互逻辑本身是对的。

用法::

    uv run python scripts/verify_signup_click.py
    uv run python scripts/verify_signup_click.py --no-sandbox   # 受限环境
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "serve"))

from grok_account_manager.core.browser import (  # noqa: E402
    DrissionBrowserSession,
    build_chromium_options,
)
from grok_account_manager.providers.gpt import humanize  # noqa: E402
from grok_account_manager.providers.gpt.registration import (  # noqa: E402
    _SIGNUP_CLICK_SCRIPT,
)

# 复刻 OpenAI 注册页的关键结构。
MOCK_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>mock signup</title>
<style>
  body { margin: 0; font-family: system-ui, sans-serif; }
  #modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.4); }
  #modal.open { display: flex; align-items: center; justify-content: center; }
  #modal > div { background: #fff; padding: 24px; border-radius: 12px; width: 420px; }
  button { display: block; width: 100%; margin: 8px 0; padding: 10px; }
</style>
</head>
<body>
  <header>
    <nav>
      <a href="#">ChatGPT</a>
      <button id="signup">Sign up</button>
      <!-- 没有事件处理器的静态按钮：用来验证 hydration 门槛（放在后面，
           这样正常流程里 pick() 不会先选中它） -->
      <button id="static-signup">Sign up</button>
      <button id="login">Log in</button>
    </nav>
  </header>

  <div id="modal">
    <div>
      <button id="google">Continue with Google</button>
      <button id="apple">Continue with Apple</button>
      <button id="email">Continue with email</button>
      <div id="email-step" style="display:none">
        <input id="email-input" type="email" name="email" autocomplete="username" />
        <button id="email-continue">Continue</button>
      </div>
    </div>
  </div>

<script>
  window.__events = [];
  const modal = document.getElementById('modal');
  const emailStep = document.getElementById('email-step');

  document.getElementById('signup').addEventListener('click', function () {
    window.__events.push('signup');
    modal.classList.add('open');
  });
  document.getElementById('email').addEventListener('click', function () {
    window.__events.push('continue-with-email');
    // 进入邮箱填写页后，「Continue with email」按钮会被替换掉（与真实页面一致）
    emailStep.style.display = 'block';
    document.getElementById('email').style.display = 'none';
  });
  document.getElementById('email-continue').addEventListener('click', function () {
    window.__events.push('email-continue');
  });

  // 模拟 React 页面：根节点带 __reactContainer$，已接管的元素带 __reactProps$。
  // #static 故意不带，用来验证「hydration 未完成时不发点击」。
  document.body.__reactContainer$mock = {};
  document.getElementById('signup').__reactProps$mock = { onClick: function () {} };
  document.getElementById('email').__reactProps$mock = { onClick: function () {} };
  document.getElementById('email-continue').__reactProps$mock = { onClick: function () {} };
  window.__mockReady = true;
</script>
</body></html>
"""

SCRIPT = _SIGNUP_CLICK_SCRIPT.replace("__HUMAN_CLICK__", humanize.CLICK_TARGET_JS)


def run_script(page, force: bool) -> dict:
    script = SCRIPT.replace("__FORCE__", "true" if force else "false")
    # as_expr=True：脚本本身是 IIFE 表达式。DrissionPage 默认会把它包进
    # function(){...}，那样返回值会被丢掉。
    return page.run_js(script, as_expr=True) or {}


def verify_concurrent(workers: int, extra_args: list[str]) -> list[tuple[str, bool, str]]:
    """按产品真实的并发模型验证：每个 worker 一个独立浏览器进程。

    产品里每个并发 worker 都有自己的 Chrome 实例（独立 profile、独立 CDP 端口），
    所以这里也用 N 个独立会话，而不是在一个浏览器里开 N 个标签页。
    """
    import threading

    html = Path(tempfile.gettempdir()) / "grok-signup-mock.html"
    html.write_text(MOCK_PAGE, encoding="utf-8")
    url = html.as_uri()

    results: list[tuple[int, dict, list]] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        options = build_chromium_options(
            lang="en-US",
            headless=False,
            window_index=index,
            window_count=workers,
            real_device=True,
        )
        options._grok_skip_fingerprint = True
        for argument in extra_args:
            options.set_argument(argument)
        session = DrissionBrowserSession(options)
        try:
            session.start()
            session.open_url(url)
            page = session.refresh_page()
            page.wait.doc_loaded()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if page.run_js("!!window.__mockReady", as_expr=True):
                    break
                time.sleep(0.05)
            state = page.run_js(SCRIPT.replace("__FORCE__", "false"), as_expr=True) or {}
            events = page.run_js("return window.__events || [];") or []
            with lock:
                results.append((index, state, events))
        except Exception as error:
            with lock:
                results.append((index, {"status": f"error: {error}"}, []))
        finally:
            try:
                session.stop()
            except Exception:
                pass

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    checks: list[tuple[str, bool, str]] = []
    ok_count = sum(
        1 for _, state, events in results
        if state.get("status") == "clicked_signup" and "signup" in events
    )
    checks.append(
        (
            f"{workers} 个并发浏览器全部成功点中 Sign up",
            ok_count == workers,
            f"成功 {ok_count}/{workers}；状态={[s.get('status') for _, s, _ in results]}",
        )
    )
    statuses = sorted({str(state.get("status")) for _, state, _ in results})
    checks.append(
        (
            "并发下没有出现状态串扰",
            len(statuses) == 1 and statuses[0] == "clicked_signup",
            f"status 集合={statuses}",
        )
    )
    return checks


# 注册最后一步之后的引导页（"You're all set"）。
ONBOARDING_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>onboarding</title>
<style>body{font-family:system-ui,sans-serif;text-align:center;padding:80px}
button{padding:12px 40px;font-size:16px}</style>
</head>
<body>
  <div class="check">&#10003;</div>
  <h1>You're all set</h1>
  <p>ChatGPT can make mistakes. Chats may be reviewed and used for training.
     <a href="#">Learn more</a></p>
  <button id="continue">Continue</button>
  <p>By continuing, you agree to our <a href="#">Terms</a> and have read our
     <a href="#">Privacy Policy</a>.</p>
  <div id="chat" style="display:none">
    <textarea placeholder="Ask anything"></textarea>
  </div>
<script>
  window.__events = [];
  document.getElementById('continue').addEventListener('click', function () {
    window.__events.push('onboarding-continue');
    document.getElementById('chat').style.display = 'block';
  });
  document.body.__reactContainer$mock = {};
  document.getElementById('continue').__reactProps$mock = { onClick: function () {} };
  window.__mockReady = true;
</script>
</body></html>
"""


def verify_onboarding(session, extra_args: list[str]) -> list[tuple[str, bool, str]]:
    """验证「You're all set」引导页上的 Continue 会被点掉。"""
    from grok_account_manager.providers.gpt.registration import _click_onboarding_continue

    html = Path(tempfile.gettempdir()) / "grok-onboarding-mock.html"
    html.write_text(ONBOARDING_PAGE, encoding="utf-8")

    options = build_chromium_options(
        lang="en-US", headless=False, window_index=0, window_count=1, real_device=True
    )
    options._grok_skip_fingerprint = True
    for argument in extra_args:
        options.set_argument(argument)
    browser = DrissionBrowserSession(options)
    checks: list[tuple[str, bool, str]] = []
    try:
        browser.start()
        browser.open_url(html.as_uri())
        page = browser.refresh_page()
        page.wait.doc_loaded()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if page.run_js("!!window.__mockReady", as_expr=True):
                break
            time.sleep(0.05)

        status = _click_onboarding_continue(page)
        checks.append(
            ("引导页 Continue 按钮被识别并点击", status == "clicked", f"status={status}")
        )
        events = page.run_js("return window.__events || [];") or []
        checks.append(
            ("引导页 Continue 的处理器已触发", "onboarding-continue" in events, f"events={events}")
        )

        # 再点一次：按钮已经不在页面上了，应该找不到，而不是乱点别的。
        page.run_js("document.getElementById('continue').remove();")
        status = _click_onboarding_continue(page)
        checks.append(
            ("按钮消失后不再乱点", status in ("not-found", "not-onboarding"), f"status={status}")
        )
    except Exception as error:
        checks.append(("引导页验证执行完成", False, f"{type(error).__name__}: {error}"))
    finally:
        try:
            browser.stop()
        except Exception:
            pass
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="注册按钮点击逻辑验证")
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="受限环境专用：给 Chrome 加 --no-sandbox",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=0,
        help="额外跑一轮并发验证：同时在 N 个标签页里点注册按钮",
    )
    args = parser.parse_args()

    options = build_chromium_options(
        lang="en-US",
        headless=False,
        window_index=0,
        window_count=1,
        real_device=True,
    )
    options._grok_skip_fingerprint = True
    if args.no_sandbox:
        options.set_argument("--no-sandbox")
        options.set_argument("--disable-gpu")

    session = DrissionBrowserSession(options)
    session.start()
    checks = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    try:
        html = Path(tempfile.gettempdir()) / "grok-signup-mock.html"
        html.write_text(MOCK_PAGE, encoding="utf-8")
        session.open_url(html.as_uri())
        page = session.refresh_page()
        page.wait.doc_loaded()

        # 1) 第一次：应该找到并点中「Sign up」，并真的触发处理器
        state = run_script(page, force=False)
        check(
            "找到并点击 Sign up 按钮",
            state.get("status") == "clicked_signup",
            f"status={state.get('status')}",
        )
        events = page.run_js("return window.__events || [];") or []
        check(
            "点击真的触发了按钮处理器",
            "signup" in events,
            f"events={events}",
        )
        modal_open = page.run_js(
            "return document.getElementById('modal').classList.contains('open');"
        )
        check("注册弹层已打开", bool(modal_open), f"open={modal_open}")

        # 2) 第二次：应该找到并点中「Continue with email」
        state = run_script(page, force=False)
        check(
            "找到并点击 Continue with email",
            state.get("status") == "clicked_email",
            f"status={state.get('status')}",
        )
        events = page.run_js("return window.__events || [];") or []
        check(
            "Continue with email 处理器已触发",
            "continue-with-email" in events,
            f"events={events}",
        )

        # 3) 第三次：邮箱输入框出现 → ready
        page.run_js(
            "document.getElementById('email-step').style.display = 'block';"
            "document.getElementById('email').style.display = 'none';"
            "document.getElementById('email-input').value = 'a@b.com';"
        )
        state = run_script(page, force=False)
        check(
            "邮箱输入框出现后判定为 ready",
            state.get("status") == "ready",
            f"status={state.get('status')}",
        )

        # 4) 静态按钮（没有事件处理器）→ 应判为 not-hydrated，而不是傻点
        page.run_js(
            "document.getElementById('modal').classList.remove('open');"
            "document.getElementById('email-step').style.display = 'none';"
            "document.getElementById('email-input').value = '';"
            "document.getElementById('signup').remove();"
            "document.getElementById('login').remove();"
        )
        state = run_script(page, force=False)
        check(
            "未接管元素的按钮判为 not-hydrated",
            state.get("status") == "not-hydrated",
            f"status={state.get('status')}",
        )
        events = page.run_js("return window.__events || [];") or []
        check(
            "not-hydrated 时没有发出点击",
            "signup" not in events[-1:],
            f"events={events}",
        )

        # 5) force=True 时，即使没接管也强制点
        state = run_script(page, force=True)
        check(
            "force=True 时强制点击",
            state.get("status") in ("clicked_signup", "click-failed"),
            f"status={state.get('status')}",
        )

        # 6) 诊断信息齐全
        diagnostics = state.get("diagnostics") or {}
        check(
            "诊断信息包含视口尺寸与候选按钮",
            bool(diagnostics.get("viewport")) and bool(diagnostics.get("candidates")),
            f"diagnostics keys={sorted(diagnostics.keys())}",
        )

    finally:
        try:
            session.stop()
        except Exception:
            pass

    extra = ["--no-sandbox", "--disable-gpu"] if args.no_sandbox else []
    for name, ok, detail in verify_onboarding(session, extra):
        checks.append((name, bool(ok), detail))

    if args.concurrency > 0:
        for name, ok, detail in verify_concurrent(args.concurrency, extra):
            checks.append((name, bool(ok), detail))

    print()
    print("=" * 74)
    print("注册按钮点击逻辑验证")
    print("=" * 74)
    failures = 0
    for name, ok, detail in checks:
        mark = "✅" if ok else "❌"
        if not ok:
            failures += 1
        print(f"  {mark} {name}")
        if detail:
            print(f"       {detail}")
    print()
    print(f"验证结果：{len(checks) - failures} 通过 / {failures} 失败")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
