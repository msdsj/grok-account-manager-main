# ai_signuper

AI 服务自动注册机框架。当前实现 Grok（xAI），并把产生的 sso JWT 灌入 [Sub2API](https://github.com/Wei-Shaw/sub2api) 当作可分发的上游账号。

## 快速开始

```bash
# 1. 装依赖（uv 管理；不要用 pip）
uv sync

# 2. 单轮试跑（产物默认落到 ./output/sso.txt）
uv run python -m ai_signuper grok --count 1

# 3. 保存 cockpit-tools 可导入的 GrokAccount JSON 数组
uv run python -m ai_signuper grok --count 1 --sink json

# 4. 同时保存 JSON 和 TXT（推荐）
uv run python -m ai_signuper grok --count 1 --sink json+txt

# 5. 长跑
uv run python -m ai_signuper grok --count 0          # 无限循环，Ctrl-C 停
uv run python -m ai_signuper grok --count 10         # 跑 10 轮
```

注册流程会打开一个**可见的** Chromium 窗口。Turnstile 需要真人化的鼠标轨迹，请把窗口留在前台、不要最小化。

## Web 控制台

项目内置一个 React 控制台，可以设置注册次数、并发账号数，并查看已注册账号列表。

```bash
# 1. 启动本地 API
uv run ai-signuper-web

# 2. 另开终端启动 React 页面
cd web
npm install
npm run dev
```

打开 `http://127.0.0.1:5173`。React 开发服务器会把 `/api` 代理到 `http://127.0.0.1:8765`。

生产模式也可以先构建前端，然后直接由 Python 服务托管：

```bash
cd web
npm run build
cd ..
uv run ai-signuper-web
```

然后打开 `http://127.0.0.1:8765`。

控制台会把成功账号继续写到原目录：

- `output/credentials/`：cockpit-tools 可导入的 GrokAccount JSON
- `output/sso.txt`：每行一个 sso cookie 兜底记录

## 完整凭证（JSON 格式）

使用 `--sink json` 或 `--sink json+txt` 可以保存 cockpit-tools 可直接导入的 GrokAccount JSON 数组：

```bash
# 保存到默认目录 output/credentials/
uv run python -m ai_signuper grok --count 1 --sink json

# 自定义输出目录
uv run python -m ai_signuper grok --count 1 --sink json --json-output /path/to/credentials
```

JSON 文件固定保存为数组格式：`[{ ...GrokAccount }]`。字段名和 cockpit-tools 的 Grok 导入/导出保持一致，包含：

- **基本信息**：id, email, auth_mode=oauth, access_token, refresh_token, created_at, last_used
- **用户身份**：user_id, principal_id, principal_type, team_id, first_name, last_name 等
- **订阅和配额**：plan_type, quota.subscriptionTier, quota.frequentUsage, quota.occasionalUsage 等
- **原始 API 响应**：auth_raw, billing_raw, user_raw, subscription_raw, task_usage_raw

如果 OAuth 换取或配额接口失败，`json` sink 也会兜底输出 cockpit-tools 可识别的 OAuth 账号结构；这种情况下可能缺少 `refresh_token` 和配额原始响应，但不会被误写成 `api_key` 格式。

每个凭证保存为独立的 JSON 文件：`grok_{timestamp}_{email_hash}.json`

**实现原理**：注册完成后默认使用浏览器里的 `sso` cookie 生成 cockpit-tools 可导入 JSON，并尽量调用以下 API 补全用户、订阅和配额：
- `https://cli-chat-proxy.grok.com/v1/billing` - 账单和配额
- `https://cli-chat-proxy.grok.com/v1/user` - 用户信息
- `https://grok.com/rest/subscriptions` - 订阅详情
- `https://grok.com/rest/tasks/usage` - 任务使用情况

如需尝试换取 `refresh_token / id_token`，可显式加 `--oauth-exchange`：

```bash
uv run python -m ai_signuper grok --count 1 --sink json --oauth-exchange
```

注意：`--oauth-exchange` 走 xAI Authorization Code + PKCE loopback 流程，会在本机监听 `127.0.0.1:56121/callback`。邮箱登录入口和最终“允许”授权会自动推进；Cloudflare 真人验证仍可能需要人工完成。浏览器回调本机后，程序再换取 `refresh_token`。

## 灌入 Sub2API

部署 Sub2API（参见 `sub2api/README.md`），在管理后台 `/admin/settings` 生成 Admin API Key，复制到项目根 `.env`：

```bash
cp .env.example .env
# 填 SUB2API_BASE_URL 和 SUB2API_ADMIN_API_KEY
```

然后：

```bash
uv run python -m ai_signuper grok --count 1 --sink sub2api
```

注册成功的账号会以 `platform=grok, type=apikey, credentials.api_key=<sso jwt>` 的形态写入 Sub2API。批量入库失败会兜底落 `output/sso-failed.txt`，避免丢账号。

## 目录结构

```
src/ai_signuper/
  __main__.py        # CLI 入口
  runtime.py         # Chromium 启停 + Python 守卫
  mail_otp.py        # DuckMail + 验证码（provider 共用）
  grok_api.py        # 使用 sso token 调用 xAI API 获取完整凭证
  providers/
    base.py          # Provider 协议（实现新 provider 时实现它）
    grok.py          # Grok 注册流程
  sinks/
    base.py          # Sink 协议
    txt_file.py      # 兜底：append 到 sso.txt
    json_credential.py  # 保存完整 JSON 格式凭证
    sub2api.py       # 灌入 Sub2API 管理 API
turnstilePatch/      # Cloudflare Turnstile 鼠标坐标 spoof 扩展
sub2api/             # vendored 的 Sub2API 网关（独立 .git，不要在里面 git push）
output/              # 运行产物
  sso.txt            # txt sink 输出（仅 sso cookie）
  credentials/       # json sink 输出（完整凭证 JSON 文件）
  sso-failed.txt     # sub2api sink 失败时的后备
```

## 加一个新 provider

1. 在 `src/ai_signuper/providers/` 新建 `<name>.py`，写一个类实现 `Provider` 协议（见 `providers/base.py`）：`name / signup_url / chrome_lang / success_cookie_name` + `run_round(session)`。
2. 在 `__main__.py` 的 `PROVIDERS` 字典里注册它。
3. `chrome_lang` 决定页面渲染语言；如果你的 `run_round` 里写死了某种语言的按钮文本，就要用对应的 lang，否则按钮匹配会落空。
4. 凭证类型不一样时直接复用 sinks——sub2api sink 用 `provider.name` 当 platform，credentials 字段 hack 走 `apikey`。
5. 在 README 这一节加一条记录该 provider 的 sink 行为。

## 已知陷阱

详见 `CLAUDE.md`。摘要：

- **Python 必须 3.12 / 3.13**，3.14 上原 Mail.tm TLS 偶发挂掉（现已改用 DuckMail，但 `requires-python` 限制保留）。
- **Chrome 必须 zh-CN locale**：所有按钮匹配字符串是中文。`runtime.build_chromium_options` 已强制 `--lang=zh-CN`。
- **页面交互必须 JS 注入**：x.ai 是 React 受控表单，Python `.input()` 会让 React 内部状态不同步、按钮永远 disabled。providers/grok.py 里的 JS 块不要"简化"。
- **每轮重启浏览器**，不要复用 cookie / session（反检测）。
