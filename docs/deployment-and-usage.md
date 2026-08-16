# 部署、运行与使用指南

本文面向第一次部署 `grok-account-manager` 的用户，说明本地开发、构建后的本地运行、命令行注册、邮箱与代理配置、账号管理和本地中转的完整流程。

> 本项目默认按本机工具设计，只监听 `127.0.0.1`。请遵守 Grok、邮箱服务和代理服务的条款，不要把账号凭证、Cookie、OAuth token、代理账号或 `.env` 上传到 GitHub，也不要把管理端口直接暴露到公网。
>
> **安全边界：** 控制台登录由本地 `grok2api` 中转处理，并不等于 FastAPI 的全局访问控制。注册、配置和中转管理 API 不应直接暴露到互联网或不受信任的局域网。远程使用请走 SSH 隧道或受控 VPN，不要使用 `--host 0.0.0.0` 直接开放服务。

## 目录

- [运行模式](#运行模式)
- [环境要求](#环境要求)
- [安装项目](#安装项目)
- [配置邮箱与运行参数](#配置邮箱与运行参数)
- [命令行注册](#命令行注册)
- [本地控制台](#本地控制台)
- [注册代理池](#注册代理池)
- [账号与凭证](#账号与凭证)
- [本地中转](#本地中转)
- [更新与验证](#更新与验证)
- [常见问题](#常见问题)
- [安全清单](#安全清单)

## 运行模式

| 模式 | 适用场景 | 需要的组件 | 入口 |
| --- | --- | --- | --- |
| CLI | 单机执行注册并写入凭证 | Python、uv、Chrome/Chromium | `uv run grok-account-manager grok ...` |
| 控制台开发模式 | 修改前端或后端时调试 | CLI 组件，加 Node.js 和 pnpm/npm | 后端 `43187`，前端 `43188` |
| 控制台构建模式 | 本机日常使用 | CLI 组件，加一次前端构建 | `http://127.0.0.1:43187` |
| 本地中转 | 使用账号池提供 OpenAI 兼容接口 | 控制台组件，加 Docker Desktop 和外部 `grok2api` 检出 | 控制台的“本地中转”页面 |

CLI 注册不依赖 `grok2api`。当前完整 Web 控制台的登录、账号管理和中转页面会通过本地 `grok2api` 实例工作，因此要使用完整控制台，请一并完成[本地中转](#本地中转)配置。

## 环境要求

### 必需软件

- Git
- Python `3.12` 或 `3.13`，不支持 Python `3.14`
- [uv](https://docs.astral.sh/uv/)
- Google Chrome 或 Chromium，可见浏览器模式用于注册流程

运行 Web 控制台还需要：

- Node.js `20+`
- pnpm `11+`。仓库带有 `pnpm-lock.yaml`，推荐使用 pnpm；已有 npm 环境也可使用 `npm install` 和 `npm run ...`。

运行本地中转还需要：

- Docker Desktop，且 Docker daemon 已启动
- 与当前中转接口兼容的 `grok2api` 源码目录，目录内必须包含 `backend/go.mod`

### 端口

| 服务 | 默认地址 | 用途 |
| --- | --- | --- |
| FastAPI 后端 | `127.0.0.1:43187` | 注册 API、静态前端、OpenAI 代理入口 |
| Vite 开发服务器 | `127.0.0.1:43188` | 前端热更新开发服务 |
| 本地中转 | `127.0.0.1:43871` | 由 `grok2api` 提供的上游服务 |
| 可选 Postgres 容器 | 主机端口 `54329` | 预留开发数据库，不是当前默认运行依赖；现有 Compose 默认会发布端口 |

若端口冲突，请优先改启动参数，而不是结束不明进程。例如后端改为 `43190`：

```bash
uv run grok-account-manager-api --port 43190
```

开发前端也要指向同一后端：

```bash
cd web
VITE_DEV_API_TARGET=http://127.0.0.1:43190 pnpm dev
```

## 安装项目

以下命令以 macOS/Linux shell 为例；Windows PowerShell 可使用等价的 Git、uv 和 pnpm 命令。

```bash
git clone https://github.com/LXXYSLF/grok-account-manager-main.git
cd grok-account-manager-main

uv sync
cp .env.example .env
```

安装前端依赖：

```bash
cd web
corepack enable
pnpm install --frozen-lockfile
cd ..
```

如果没有 pnpm，也可以使用：

```bash
cd web
npm install
cd ..
```

确认基础环境可用：

```bash
uv run python --version
uv run grok-account-manager --help
```

浏览器会在启动时自动探测 Chrome/Chromium。macOS 默认会查找 `/Applications/Google Chrome.app` 和 `/Applications/Chromium.app`；如果未安装，先安装一个可见的浏览器再运行注册任务。

## 配置邮箱与运行参数

所有本地机密都写入项目根目录 `.env`。它由 `.gitignore` 排除，不能替代为提交到仓库的配置文件。

### DuckMail：默认邮箱源

最小可运行配置如下：

```dotenv
DUCKMAIL_BASE_URL=https://api.duckmail.sbs
DUCKMAIL_API_KEY=replace-with-your-key
DUCKMAIL_DOMAIN=@msdsj.cyou
GROK_ACCOUNT_MANAGER_EMAIL_SOURCE=duckmail
```

`DUCKMAIL_API_KEY` 为空时，默认 DuckMail 流程无法创建邮箱。域名必须是你的 DuckMail 服务实际支持的域名。更多说明见 [DuckMail 邮箱源](duckmail.md)。

### Cloud Mail

Cloud Mail 支持 Public Token 和站点账号登录两种认证方式。每轮会在配置域名中选择一个域名创建随机邮箱，只读取该地址且 `emailId` 高于当前游标的新邮件。API 地址末尾可以包含 `/api`。

Public Token 示例：

```dotenv
GROK_ACCOUNT_MANAGER_EMAIL_SOURCE=cloud_mail
CLOUD_MAIL_API_BASE=https://mail.example.com
CLOUD_MAIL_DOMAINS=example.com,example.net
CLOUD_MAIL_PUBLIC_TOKEN=replace-with-public-token
```

账号登录示例：

```dotenv
GROK_ACCOUNT_MANAGER_EMAIL_SOURCE=cloud_mail
CLOUD_MAIL_API_BASE=https://mail.example.com/api
CLOUD_MAIL_DOMAINS=example.com
CLOUD_MAIL_LOGIN_EMAIL=admin@example.com
CLOUD_MAIL_LOGIN_PASSWORD=replace-with-password
```

Public Token 非空时优先使用 Public 模式；否则必须同时填写登录邮箱和密码。控制台可以在“注册任务”的邮箱来源中选择 Cloud Mail 并填写相同配置，这些输入只用于当前进程和任务重试，不会写入任务快照。完整说明见 [Cloud Mail 邮箱源](cloud-mail.md)。

### Outlook 现有账号池

Outlook 功能只读取已有账号收取验证码，**不会自动注册 Microsoft 账号**。每行一条记录，使用 `----` 或 `|` 分隔：

```text
email@example.com----password----clientId----refreshToken----auto
email@example.com|password|clientId|refreshToken|graph
```

第五列可为 `auto`、`imap` 或 `graph`。可在命令行传入文件，或写入 `.env`：

```dotenv
GROK_ACCOUNT_MANAGER_EMAIL_SOURCE=outlook
OUTLOOK_ACCOUNTS_FILE=/absolute/path/to/outlook_accounts.txt
```

控制台内保存的 Outlook 池位于 `output/mailboxes/outlook-accounts.txt`，会尽量以仅当前用户可读写的权限保存。格式和限制见 [Outlook 邮箱池](outlook-mailbox-pool.md)。

### Gmail 与 Google 账号池

`gmail` 模式使用 Gmail 接收验证码；`google` 模式会走网站的 Google 登录/注册入口。两者都从 `GOOGLE_ACCOUNTS_FILE` 或对应命令行参数读取数据：

```text
# gmail：建议使用 Gmail 应用专用密码
name@gmail.com----app-password

# google：账号、密码、可选辅助邮箱
name@gmail.com----password----recovery@example.com
```

示例配置：

```dotenv
GROK_ACCOUNT_MANAGER_EMAIL_SOURCE=google
GOOGLE_ACCOUNTS_FILE=/absolute/path/to/google_accounts.txt
```

账号池一轮只会领取一次。开启多并发、浏览器重试或 Google 登录时，请准备多于目标轮数的账号记录。

### 并发与重试参数

`.env.example` 包含以下常用参数：

```dotenv
GROK_ACCOUNT_MANAGER_MAX_CONCURRENCY=20
GROK_ACCOUNT_MANAGER_MAX_OAUTH_CONCURRENCY=2
GROK_ACCOUNT_MANAGER_MAX_REGISTRATION_ATTEMPTS=3
```

- `GROK_ACCOUNT_MANAGER_MAX_CONCURRENCY`：控制台允许的注册窗口上限。
- `GROK_ACCOUNT_MANAGER_MAX_OAUTH_CONCURRENCY`：同时进行 OAuth token 交换的窗口数。
- `GROK_ACCOUNT_MANAGER_MAX_REGISTRATION_ATTEMPTS`：单轮注册页面失败时的最大重试次数，范围为 `1` 到 `4`。

并发、独立 profile 和临时指纹只用于隔离本地浏览器会话，不能改变多个窗口共用同一个公网出口的事实，也不能保证通过第三方风控。

## 命令行注册

### 最小命令

执行一轮 DuckMail 注册，并同时写入 JSON 与 TXT：

```bash
uv run grok-account-manager grok --count 1 --sink json+txt
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--count N` | 执行 N 轮；`0` 表示无限循环（使用有限代理池时会在池耗尽后结束） |
| `--sink txt` | 写入 TXT SSO 输出 |
| `--sink json` | 写入 GrokAccount JSON |
| `--sink json+txt` | 同时使用多个输出端 |
| `--output PATH` | 指定 TXT 输出路径 |
| `--json-output PATH` | 指定 JSON 输出目录 |
| `--email-source duckmail/cloud_mail/outlook/gmail/google` | 指定邮箱或 Google 注册模式 |
| `--cloud-mail-api-base URL` | Cloud Mail 站点地址，末尾可带 `/api` |
| `--cloud-mail-domains DOMAINS` | Cloud Mail 邮箱域名，使用逗号或换行分隔 |
| `--cloud-mail-public-token TOKEN` | Cloud Mail Public Token |
| `--cloud-mail-login-email EMAIL` | Cloud Mail 登录邮箱 |
| `--cloud-mail-login-password PASSWORD` | Cloud Mail 登录密码 |
| `--oauth-exchange` | 注册后尝试换取 OAuth `refresh_token` |
| `--proxy-file PATH` | 指定无认证注册代理列表 |
| `--no-proxy` | 强制直连并忽略代理池 |
| `--headless` | 无窗口运行；不建议用于有 Turnstile 的页面 |

使用 Outlook 账号池的示例：

```bash
uv run grok-account-manager grok \
  --count 1 \
  --email-source outlook \
  --outlook-accounts-file /absolute/path/to/outlook_accounts.txt \
  --sink json
```

OAuth 交换示例：

```bash
uv run grok-account-manager grok --count 1 --sink json --oauth-exchange
```

OAuth 交换使用 xAI Device Flow：程序会在同一个浏览器中打开授权标签页，并轮询 token endpoint，不会监听固定的本地回调端口。授权或人机验证页面仍可能需要用户在可见浏览器中完成操作。只有成功得到有效 `refresh_token` 的结果才会按 OAuth 模式写入正式凭证。详细流程见 [Grok OAuth Flow](grok-oauth-flow.md)。

按 `Ctrl+C` 可停止 CLI 后续轮次。不要在同一批次同时启动 CLI 和控制台注册任务，以免它们争用账号池、浏览器和输出文件。

## 本地控制台

### 开发模式

终端一启动后端：

```bash
uv run grok-account-manager-api --reload
```

终端二启动前端：

```bash
cd web
pnpm dev
```

打开 `http://127.0.0.1:43188`。Vite 会把 `/api`、`/v1`、`/healthz` 和 `/readyz` 转发到 `http://127.0.0.1:43187`。

### 构建后的本地运行

先构建静态前端：

```bash
cd web
pnpm build
cd ..
```

再启动后端：

```bash
uv run grok-account-manager-api
```

打开 `http://127.0.0.1:43187`。此时 FastAPI 会直接提供 `web/dist` 中的前端文件，不需要 Vite 开发服务器。

控制台包含注册任务、账号列表、账号测试、图片/对话测试、代理与中转配置等页面。注册任务启动后可以停止任务，也可以最小化或还原程序启动的浏览器窗口。

### 后台启动

仓库提供了一个仅启动后端的辅助脚本：

```bash
uv run python scripts/start_web_daemon.py \
  "$(pwd)" \
  output/backend.pid \
  output/backend.log
```

该脚本要求 `.venv/bin/grok-account-manager-api` 已由 `uv sync` 安装。停止时先读取 `output/backend.pid`，确认 PID 对应本项目进程后再停止；不要对未知 PID 执行终止命令。

## 注册代理池

注册代理池只接受无认证的 `HOST:PORT`、`http://HOST:PORT` 或 `https://HOST:PORT`。当前注册浏览器不支持带用户名/密码的代理 URL 或 SOCKS 节点。

代理文件格式：

```text
# 一行一个端点；空行和 # 注释会忽略
198.51.100.10:8080
http://198.51.100.11:8080
https://198.51.100.12:8443
```

使用方式：

```bash
uv run grok-account-manager grok \
  --count 10 \
  --proxy-file /absolute/path/to/proxies.txt \
  --sink json
```

代理来源按以下优先级选择：

1. 传入 `--proxy-file` 或设置 `.env` 的 `GROK_ACCOUNT_MANAGER_PROXY_FILE` 时，只使用该文件。
2. 两者均为空时，自动合并 `~/Downloads/xx.txt`（存在时）与控制台“注册任务 -> 管理已保存节点”中的内容，并去重。
3. 传入 `--no-proxy` 时，强制直连并忽略所有代理来源。

任务内每轮会领取一个未使用端点；Provider 重试和下一轮浏览器重启时也会领取新端点。请求轮数大于节点数量时，CLI 会按可用节点数量收尾，避免在同一任务里重复使用端点。控制台保存的节点位于 `output/registration-proxies.json`，界面和日志只显示脱敏后的地址摘要。

## 账号与凭证

运行产物位于 `output/`，默认不应进入 Git：

| 文件或目录 | 说明 |
| --- | --- |
| `output/credentials/grok_credentials.json` | GrokAccount JSON 凭证集合 |
| `output/sso.txt` | TXT sink 生成的 SSO 输出 |
| `output/grok-account-manager.db` | 本地 SQLite 账号索引、凭证引用和测试结果 |
| `output/pending-registration-results.json` | 注册成功但 OAuth 或持久化尚未完成的恢复队列 |
| `output/mailboxes/outlook-accounts.txt` | 控制台保存的 Outlook 账号池 |
| `output/registration-proxies.json` | 控制台保存的注册代理池 |
| `output/relay-config.json` | 本地中转配置，包含敏感管理信息 |

凭证 JSON 损坏时，程序会先创建带时间戳的 `.broken-*` 备份，再拒绝覆盖源文件。`persistence_failed` 队列会在后端下次启动时尝试恢复；`oauth_pending` 只记录已注册但未得到完整 OAuth 凭证的状态，不会伪装成可用 OAuth 账号。

备份时请使用加密磁盘或受限目录，并同时备份 `output/credentials/`、SQLite 数据库和你单独保存的邮箱账号池。不要把备份直接放回仓库根目录后提交。

## 本地中转

本项目不内置 `grok2api` 源码。要启用完整控制台和 OpenAI 兼容中转，请准备一个兼容的新版本 `grok2api` 源码目录，并启动 Docker Desktop。

推荐把外部项目放到默认位置：

```bash
git clone https://github.com/chenyme/grok2api.git "$HOME/Downloads/grok2api-main"
```

也可以在 `.env` 指定其他绝对路径：

```dotenv
GROK2API_PATH=/absolute/path/to/grok2api-main
```

启动本项目后，后端会在后台尝试构建并启动本地中转镜像。若 Docker、源码目录或 `backend/go.mod` 缺失，注册 API 仍可启动，但完整 Web 控制台的登录和中转功能会不可用。

首次成功准备中转时，项目会在 `output/` 下生成中转配置和数据目录。中转管理员用户名为 `grok-account-manager`；首次生成的管理员密码保存在仅本机可读的 `output/relay-config.json` 的 `admin_key` 字段中。该值是机密，不能截图、提交或发送给他人；应在可信本机上妥善保存并按需更换。

在控制台“本地中转”页面可以检查状态、启动或停止中转、同步本地账号和查看可用模型。中转默认地址为 `http://127.0.0.1:43871`。不要把它、后端端口或中转配置直接映射到公网。

`docker-compose.yml` 中的 Postgres 服务是后续数据库迁移预留项。当前默认账号数据库是 SQLite，不会因为运行 `docker compose up -d` 自动切换到 Postgres。现有 Compose 使用示例账号并默认发布主机端口 `54329`，只应在可信开发网络中运行；如要启用它，应先修改密码和端口映射策略。

## 更新与验证

更新前先确认本地机密和未提交改动：

```bash
git status --short
```

正常更新流程：

```bash
git pull --rebase --autostash
uv sync

cd web
pnpm install --frozen-lockfile
pnpm build
cd ..
```

`--autostash` 只处理 Git 已跟踪的本地改动，不会代替备份未跟踪的账号池、HTML 工具或其他本地文件。更新前应手动备份重要的未跟踪文件和 `output/`。

验证后端与前端：

```bash
uv run python -m compileall serve tests
uv run python -m unittest discover -s tests

cd web
pnpm build
```

构建后的服务启动后，可检查：

```bash
curl http://127.0.0.1:43187/api/openapi.json
```

完整变更记录见 [CHANGELOG.md](../CHANGELOG.md)。

## 常见问题

### `43187` 或 `43188` 已被占用

先确认具体监听者：

```bash
lsof -nP -iTCP:43187 -sTCP:LISTEN
lsof -nP -iTCP:43188 -sTCP:LISTEN
```

优先换本项目端口。后端可使用 `--port`，前端通过 `VITE_DEV_API_TARGET` 指向新的后端端口。不要停止自己无法确认归属的进程。

### 控制台显示无法连接、登录页不可用

按顺序检查：

1. 后端是否运行在 `127.0.0.1:43187`。
2. 开发模式下，`VITE_DEV_API_TARGET` 是否与后端实际地址一致。
3. 是否已完成 `grok2api` 源码路径和 Docker Desktop 配置。
4. 查看 `output/backend.log` 与 `output/grok2api-relay.log` 的最后几行，不要把含 token 的整份日志公开。

### 浏览器没有打开或注册页无法加载

- 确认本机安装了 Chrome/Chromium，并关闭已经残留的本项目浏览器进程后重试。
- 优先使用可见浏览器，不要把 `--headless` 当作默认运行方式。
- 检查网络、代理节点和邮箱服务是否可访问。页面改版、验证码或服务端风控都可能要求人工处理或导致该轮失败。

### 没有收到邮箱验证码

- 检查 `.env` 中 DuckMail 的 API Key 和域名。
- Cloud Mail 模式确认 API 地址、域名和所选认证方式完整；服务响应需要是 HTTP 200 且 JSON `code` 为 `200`。
- Outlook/Google 模式确认账号池格式、授权信息和可读收件箱。
- 账号池记录耗尽时，补充足够行数后再启动新的任务。

### 启用 OAuth 后账号没有写入 JSON

OAuth 模式要求拿到有效的 `refresh_token`。如果授权被拒绝、超时或仍停留在验证页面，结果会保留到恢复队列或记录失败状态，而不会写成不完整的 OAuth 凭证。可在可见浏览器中完成授权后重新执行。

## 安全清单

- `.env`、Cloud Mail Token/登录密码、`output/`、账号池文件、代理列表和浏览器 Cookie 都是机密。
- 后端、Vite 和中转默认应保持在 `127.0.0.1`；远程访问请使用 SSH 隧道或受控 VPN，不要直接绑定 `0.0.0.0`。
- 当前 `docker-compose.yml` 的 Postgres 示例端口会发布到主机接口，运行前应修改示例凭据并确认防火墙和网络边界。
- 不要提交 `output/relay-config.json`、SQLite 数据库、`grok_credentials.json`、SSO 文本或截图中的管理密码。
- 不要把控制台日志直接粘贴到公开 Issue；日志可能包含邮箱、代理摘要或错误上下文。
- 升级前备份重要产物，尤其是未被 Git 跟踪的本地工具和账号池。
