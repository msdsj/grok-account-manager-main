# 项目长期记忆

## 项目性质

MSDSJ 多网关账号注册与凭证管理工具（grok-account-manager）。Python 3.12/3.13 +
FastAPI 后端，React 控制台（`web/`），DrissionPage 驱动真实 Chrome 做注册。
支持 Grok / GPT(ChatGPT) / Kiro 三个 provider，网关层把凭证转成 OpenAI 兼容 API。

## 环境事实（重要，容易踩坑）

- 本机 Chrome 版本 **157.0.8103.78**。UA 是 `Chrome/157.0.0.0`，
  `navigator.userAgentData.brands` 为 `Not-A.Brand/99, Google Chrome/157, Chromium/157`。
- Chrome 157 仍保留 `window.chrome.csi` 与 `window.chrome.loadTimes`，
  但**没有扩展时 `window.chrome.runtime` 是 undefined**（补它就是破绽）。
- Chrome 157 的 `navigator.deviceMemory` 上限已放宽到 **16**（旧版为 8）。
- `navigator.hardwareConcurrency` = 10（本机真实核数）。
- 本机系统代理 = **127.0.0.1:7890**（Clash 类，HTTP/HTTPS/SOCKS 同端口）。
  Chrome 会继承系统代理，**Python requests 不会**（它只认 HTTP_PROXY/HTTPS_PROXY
  环境变量）。实测直连 chatgpt.com 10 秒超时失败，走 7890 则 2.8 秒返回 200。
  现在用 `GROK_ACCOUNT_MANAGER_EGRESS_PROXY`（默认 `http://127.0.0.1:7890`）
  把浏览器和 Python 侧统一到同一个出口；写成 none/off/direct 可关闭。

## DrissionPage 的坑（踩过，勿重蹈）

`page.run_js(script)` 默认会把脚本包进 `function(){ ... }` 再求值，因此
**IIFE 形式的脚本 `(function(){...})()` 返回值会被丢掉**。两种正确写法：

- `page.run_js(script, as_expr=True)` —— 脚本按表达式求值（推荐）；
- 或脚本顶层直接写 `return ...`。

`run_js` 的 `arguments` 传参在 `as_expr=True` 下不可用；DOM 元素也不能安全地
跨 CDP 传参，所以要操作元素时应把定位逻辑**内联进脚本**。
另外 `as_expr=True` 下返回值不能带 `return`，也不能取回数组。

## 浏览器交互的硬规则

- **查找元素和点击必须在同一次 `run_js` 里完成**。拆成两次（先标记
  `window.xxx`、下一轮再点）会在页面跳转后指向已销毁的节点，点击永远发不出去——
  表现为「有时没有点到注册按钮」。代码里已有 `_click_first_matching()`。
- **要等 React 接管元素**：hydration 完成前 DOM 里有按钮但没有事件处理器，
  点击是空操作。判断依据是元素上有没有 `__reactProps$` / `__reactFiber$`；
  但只有确认页面是 React（根节点有 `__reactContainer$`）时才用这个门槛，
  否则 `addEventListener` 的监听器会被误判成「没接管」。
- 注册最后一步之后会停在 **"You're all set" + Continue 按钮** 的引导页，
  URL 已是 chatgpt.com 但没有聊天输入框——不点这个 Continue 就永远进不了主页。

## 窗口布局约定

`calculate_window_bounds()` **优先保证视口够大**，不再把窗口高度对半分。
并发 4 个以上时旧实现把 1800×1169 的屏切成 600×584，视口压到 ~490px，
chatgpt.com 会切换成移动端版式，注册按钮的 DOM 结构就变了。
现在每个窗口保持满屏高度，放不下时在屏幕上错位 24px 重叠（窗口默认最小化，不影响）。

## 关键约定

- 指纹策略由 `GROK_ACCOUNT_MANAGER_FINGERPRINT_MODE` 控制：
  `real` = 不伪造任何硬件特征（与手动注册同源）；`random` = 原有随机画像；
  不设置时为 `random`。
- **注册期与使用期必须同源**：注册时把浏览器身份快照写进凭证
  （`browser_identity`），网关层据此回放请求头。UA / Sec-CH-UA / Accept-Language /
  OAI-Device-Id 全部来自这份快照，绝不写死。
- `OAI-Device-Id` / `OAI-Session-Id` 必须**账号级稳定**，绝不能每次请求重新生成。
- 指纹脚本的原则：**不伪装就不改、伪装就同族**——任何被改写的属性都不能和
  另一个真实属性互相矛盾（WebGL 扩展列表与 `getExtension()` 是典型例子）。

## 验证工具

- `uv run python scripts/fingerprint_audit.py [--real-device]` ——
  差分法指纹审计（不注入 vs 注入，逐属性对比）。
  受限沙箱内 Chrome 子进程无法初始化自己的沙箱，需要加 `--no-sandbox`
  （**仅用于审计，正常注册不要加**）。
- `uv run python scripts/verify_signup_click.py [--concurrency N]` ——
  用本地 mock 页复刻 OpenAI 注册页/引导页，在真实浏览器里端到端验证点击逻辑。
- `uv run python scripts/check_registration_consistency.py` ——
  账号池注册/使用一致性体检。

## 测试

`uv run python -m unittest discover -s tests`（全量 246 个用例）。
