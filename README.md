# grok-account-manager
 
MSDSJ 的 Grok 账号注册与凭证管理工具。项目提供 Python 自动化注册流程和 React 本地控制台，支持 DuckMail 域名邮箱、Outlook 账号池接码、GrokAccount JSON 凭证归档，以及可选推送到外部 Sub2API 实例。

本项目允许免费使用、学习和二次开发。请勿把 `.env`、Outlook refresh token、浏览器 cookie、SSO token 或 `output/` 里的凭证提交到 GitHub。

## 项目界面

![MSDSJ Grok 注册机控制台](docs/images/dashboard.png)

## 项目地址与交流群

GitHub：<https://github.com/msdsj/grok-account-manager-main>

如果这个项目帮到你，欢迎帮忙点一个 Star。使用中遇到账号池、Grok CLI 4.5、Chat 对话或图片生成问题。
## 更新日志

完整版本记录见 [CHANGELOG.md](CHANGELOG.md)。当前版本重点更新 FastAPI 后端重构、账号数据库、Grok CLI 4.5 测试、Chat/图片测试、本地中转和新版控制台界面。

## 功能

- Grok 邮箱注册自动化，注册资料姓名和密码每轮随机生成。
- 邮箱源支持 DuckMail、Outlook、Gmail/Google 账号池，`----` 和 `|` 两种分隔格式都兼容。
- 输出 cockpit-tools 可导入的 GrokAccount JSON 数组，并同步写入本地账号数据库。
- React 控制台使用侧边栏路由，支持注册任务、账号列表、Grok CLI 4.5 测试、Chat 对话测试、图片生成测试和本地中转。
- 可选 `sub2api` sink，把注册结果写入独立部署的 Sub2API 管理 API。

## 环境要求

- Python 3.12 或 3.13
- uv
- Google Chrome 或 Chromium
- Node.js 20+ 与 npm，用于前端开发/构建

注册流程默认打开可见浏览器窗口。Grok 的 Turnstile 验证对 headless 不稳定，不建议后台无窗口运行。

## 快速开始

```bash
uv sync
cp .env.example .env
```

编辑 `.env`，至少配置 DuckMail：

```bash
DUCKMAIL_API_KEY=your_duckmail_api_key
DUCKMAIL_DOMAIN=@msdsj.cyou
```

单轮注册并保存 JSON：

```bash
uv run grok-account-manager grok --count 1 --sink json
```

也可以用模块方式运行：

```bash
uv run python -m grok_account_manager grok --count 1 --sink json
```

## Outlook 邮箱源

把 Outlook 账号池保存为本地文件，例如 `outlook_accounts.txt`。该文件已被 `.gitignore` 忽略。账号字段支持 `----` 或 `|` 两种分隔符。第五段可选，用于指定 Microsoft 邮件读取方式：`auto`、`imap` 或 `graph`；不填时默认 `auto`，会先尝试 IMAP，再尝试 Microsoft Graph。

```text
email@example.com----password----clientId----refreshToken
email@example.com----password----clientId----refreshToken----graph
email@example.com|password|clientId|refreshToken|auto
```

运行：

```bash
uv run grok-account-manager grok --count 1 \
  --email-source outlook \
  --outlook-accounts-file outlook_accounts.txt \
  --sink json
```

也可以在 `.env` 中设置：

```bash
GROK_ACCOUNT_MANAGER_EMAIL_SOURCE=outlook
OUTLOOK_ACCOUNTS_FILE=outlook_accounts.txt
```

Google/Gmail 账号池同样支持两种格式：

```text
email@gmail.com----password----recovery@example.com
email@gmail.com|password|recovery@example.com
```

## 本地数据库

账号完整凭证、导出索引和测试结果会写入本地 SQLite：

```bash
GROK_ACCOUNT_MANAGER_DB_PATH=output/grok-account-manager.db
```

`docker-compose.yml` 预留了本地 Postgres 服务，后续如果要切到 Postgres，需要再安装对应 Python 驱动并迁移数据库层；当前默认 SQLite 不需要额外容器即可使用。

## 本地控制台

后端现在是单一 FastAPI 服务，注册机 API、中转站管理 API 和 OpenAI 兼容代理都由它提供。开发模式运行两个进程：一个后端、一个前端。

```bash
uv run grok-account-manager-api
```

```bash
cd web
npm install
npm run dev
```

打开 `http://127.0.0.1:5173`。前端开发服务器会把 `/api`、`/v1` 和 `/admin/api` 代理到 FastAPI 后端。

生产模式可先构建前端：

```bash
cd web
npm run build
cd ..
uv run grok-account-manager-api
```

然后打开 `http://127.0.0.1:8765`。旧命令 `uv run grok-account-manager-web` 仍然可用，内部指向同一个 FastAPI 入口。

## 输出文件

- `output/credentials/grok_credentials.json`：GrokAccount JSON 数组。
- `output/grok-account-manager.db`：本地账号数据库。
- `output/sso.txt`：使用 `txt` sink 时写入的 SSO 兜底文本。
- `output/sso-failed.txt`：Sub2API 写入失败时的兜底文本。

`output/` 是运行产物目录，默认不纳入 Git。

## OAuth Refresh Token

默认 JSON 会基于浏览器里的 `sso` cookie 尝试补全用户、订阅和额度信息。如需尝试换取 `refresh_token / id_token`：

```bash
uv run grok-account-manager grok --count 1 --sink json --oauth-exchange
```

该流程会监听 `127.0.0.1:56121/callback` 并可能需要人工完成网页授权或 Turnstile。

## Sub2API

本仓库不再 vendored Sub2API 源码。请独立部署 [Sub2API](https://github.com/Wei-Shaw/sub2api)，在管理后台生成 Admin API Key 后配置：

```bash
SUB2API_BASE_URL=http://localhost:8080
SUB2API_ADMIN_API_KEY=your_admin_api_key
```

推送：

```bash
uv run grok-account-manager grok --count 1 --sink sub2api
```

## 项目结构

```text
serve/grok_account_manager/
  api/
    app.py                # FastAPI 应用工厂、CORS、SPA 静态入口
    main.py               # 后端启动命令
    routers/              # 注册、账号、中转站、代理 API 路由
    services/             # 任务调度、账号数据库、中转站进程管理
  cli.py                  # CLI 参数解析和轮次调度
  core/browser.py         # Chromium 启停、扩展加载、cookie 等待
  mail/duckmail.py        # DuckMail 接码
  mail/sources.py         # DuckMail / Outlook 邮箱源
  grok/client.py          # GrokAccount JSON 构建与额度 API
  grok/oauth_exchange.py  # xAI OAuth PKCE loopback
  providers/grok.py       # Grok 注册页面自动化
  sinks/                  # JSON/TXT/Sub2API 输出
extensions/turnstile_patch/
web/src/
  routes.tsx              # 侧边栏页面路由配置
scripts/
docs/
```

## 验证

```bash
python3 -X pycache_prefix=/private/tmp/grok-account-manager-pyc -m compileall serve tests
cd web && npm run build
```

## 开源说明与致谢

本项目由 MSDSJ 维护，允许免费使用和二次开发。项目实现参考和感谢以下开源项目：

- [cockpit-tools](https://github.com/jlcodes99/cockpit-tools)：GrokAccount JSON 结构、OAuth 账号管理和导入格式参考。
- [Kiro-account-manager](https://github.com/chaogei/Kiro-account-manager)：账号池管理、邮箱账号格式和桌面账号管理产品思路参考。
- [Sub2API](https://github.com/Wei-Shaw/sub2api)：可选的账号下游管理 API，本项目仅通过其 Admin API 写入账号。
- Turnstile MouseEvent patch 思路来自仓库内 `extensions/turnstile_patch/` 所保留的扩展说明。

本项目与 xAI、Grok、Microsoft、Outlook、DuckMail、cockpit-tools、Kiro-account-manager、Sub2API 官方均无隶属关系。使用者需要自行遵守相关服务条款、当地法律和开源许可证要求。
