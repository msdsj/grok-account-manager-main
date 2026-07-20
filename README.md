# grok-account-manager

MSDSJ 的 Grok 账号注册与凭证管理工具。项目提供 Python 自动化注册流程和 React 本地控制台，支持 DuckMail 域名邮箱、Outlook 账号池接码、GrokAccount JSON 凭证归档，以及可选推送到外部 Sub2API 实例。

本项目允许免费使用、学习和二次开发。请勿把 `.env`、Outlook refresh token、浏览器 cookie、SSO token 或 `output/` 里的凭证提交到 GitHub。

## 功能

- Grok 邮箱注册自动化，注册资料姓名和密码每轮随机生成。
- 邮箱源支持 DuckMail 和 Outlook IMAP，Outlook 格式兼容 `邮箱----密码----clientId----refreshToken`。
- 输出 cockpit-tools 可导入的 GrokAccount JSON 数组。
- React 控制台支持注册任务、并发、日志、账号列表和 JSON 导出。
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

把 Outlook 账号池保存为本地文件，例如 `outlook_accounts.txt`。该文件已被 `.gitignore` 忽略。

```text
email@example.com----password----clientId----refreshToken
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

## Web 控制台

开发模式需要两个终端：

```bash
uv run grok-account-manager-web
```

```bash
cd web
npm install
npm run dev
```

打开 `http://127.0.0.1:5173`。生产模式可先构建前端：

```bash
cd web
npm run build
cd ..
uv run grok-account-manager-web
```

然后打开 `http://127.0.0.1:8765`。

## 输出文件

- `output/credentials/grok_credentials.json`：GrokAccount JSON 数组。
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
src/grok_account_manager/
  cli.py                  # CLI 参数解析和轮次调度
  core/browser.py         # Chromium 启停、扩展加载、cookie 等待
  mail/duckmail.py        # DuckMail 接码
  mail/sources.py         # DuckMail / Outlook 邮箱源
  grok/client.py          # GrokAccount JSON 构建与额度 API
  grok/oauth_exchange.py  # xAI OAuth PKCE loopback
  providers/grok.py       # Grok 注册页面自动化
  sinks/                  # JSON/TXT/Sub2API 输出
  webapp/server.py        # 本地 API 和静态页面服务
extensions/turnstile_patch/
web/src/
scripts/
docs/
```

## 验证

```bash
python3 -X pycache_prefix=/private/tmp/grok-account-manager-pyc -m compileall src
cd web && npm run build
```

## 开源说明与致谢

本项目由 MSDSJ 维护，允许免费使用和二次开发。项目实现参考和感谢以下开源项目：

- [cockpit-tools](https://github.com/jlcodes99/cockpit-tools)：GrokAccount JSON 结构、OAuth 账号管理和导入格式参考。
- [Kiro-account-manager](https://github.com/chaogei/Kiro-account-manager)：账号池管理、邮箱账号格式和桌面账号管理产品思路参考。
- [Sub2API](https://github.com/Wei-Shaw/sub2api)：可选的账号下游管理 API，本项目仅通过其 Admin API 写入账号。
- Turnstile MouseEvent patch 思路来自仓库内 `extensions/turnstile_patch/` 所保留的扩展说明。

本项目与 xAI、Grok、Microsoft、Outlook、DuckMail、cockpit-tools、Kiro-account-manager、Sub2API 官方均无隶属关系。使用者需要自行遵守相关服务条款、当地法律和开源许可证要求。
