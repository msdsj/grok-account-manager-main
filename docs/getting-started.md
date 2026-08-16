# 快速使用教程

本文给出一套从安装到启动控制台的最短流程。项目默认只监听本机地址，注册浏览器默认使用可见窗口；`.env`、邮箱账号池、Cookie、SSO、OAuth 凭证和 `output/` 运行数据都只保存在本机。

## 1. 准备环境

需要安装：

- Python `3.12` 或 `3.13`，不支持 Python `3.14`
- [uv](https://docs.astral.sh/uv/)
- Node.js `20+` 和 pnpm `11+`
- Google Chrome 或 Chromium
- 如果使用本地中转，再启动 Docker Desktop，并准备兼容的 `grok2api` 源码

默认端口如下：

| 服务 | 地址 | 用途 |
| --- | --- | --- |
| FastAPI 后端 | `http://127.0.0.1:43187` | API、构建后的前端和任务服务 |
| Vite 前端 | `http://127.0.0.1:43188` | 开发时的热更新页面 |
| Grok2API 中转 | `http://127.0.0.1:43871` | 可选的本地上游中转 |

## 2. 安装项目

```bash
git clone https://github.com/LXXYSLF/grok-account-manager-main.git
cd grok-account-manager-main

uv sync

cd web
corepack enable
pnpm install --frozen-lockfile
cd ..
```

没有 pnpm 时，可以在 `web/` 中使用 `npm install`；后续把 `pnpm dev`、`pnpm build` 替换成 `npm run dev`、`npm run build` 即可。

## 3. 配置本地参数

复制模板作为本地配置；模板可以提交，但不能把真实密钥写入 `.env.example`：

```bash
cp .env.example .env
chmod 600 .env
```

编辑项目根目录的 `.env`，至少选择并配置一种邮箱来源。例如 DuckMail 只填写你自己的值：

```dotenv
GROK_ACCOUNT_MANAGER_EMAIL_SOURCE=duckmail
DUCKMAIL_API_KEY=replace-with-your-api-key
DUCKMAIL_DOMAIN=@example.com
```

Cloud Mail、Outlook、Gmail/Google、代理池和 OAuth 选项见[完整部署与使用指南](deployment-and-usage.md)。真实值只放在 `.env` 或仓库外部的账号池文件中。

## 4. 开发模式：启动前后端

开发模式需要两个终端。

终端一，启动 FastAPI 后端：

```bash
uv run grok-account-manager-api
```

后端默认监听 `127.0.0.1:43187`。开发时需要自动重载，可以使用：

```bash
uv run grok-account-manager-api --reload
```

终端二，启动 React/Vite 前端：

```bash
cd web
pnpm dev
```

打开 <http://127.0.0.1:43188>。Vite 会把 `/api`、`/v1`、`/healthz` 和 `/readyz` 请求代理到 `43187` 后端。

如果后端换了端口，前端也要同步指定：

```bash
# 终端一
uv run grok-account-manager-api --port 43190

# 终端二
cd web
VITE_DEV_API_TARGET=http://127.0.0.1:43190 pnpm dev --port 43191
```

此时打开 <http://127.0.0.1:43191>。

## 5. 日常使用：构建后只运行后端

不修改前端代码时，可以先构建一次静态文件，再只启动 FastAPI：

```bash
cd web
pnpm build
cd ..

uv run grok-account-manager-api
```

打开 <http://127.0.0.1:43187>。后端会直接提供 `web/dist`，不需要再启动 Vite。修改 `web/src/` 后重新执行 `pnpm build`。

## 6. 启动注册任务

1. 先确认后端和前端已启动，打开控制台的“注册任务”。
2. 选择邮箱来源，确认 `.env` 或页面配置中的邮箱服务可以收取验证码。
3. 需要换取 OAuth `refresh_token` 时启用 OAuth 选项；授权页可能需要在可见浏览器中人工操作。
4. 注册流程默认打开可见 Chrome/Chromium。不要把 `--headless` 当作默认模式，Turnstile 或登录页在无头模式下可能失败。
5. 任务停止后检查账号列表和 `output/` 产物；不要把这些产物复制到 Git 暂存区。

需要用 CLI 时，可以在第三个终端运行：

```bash
uv run grok-account-manager grok --count 1 --sink json+txt
```

需要尝试 OAuth 交换：

```bash
uv run grok-account-manager grok --count 1 --sink json --oauth-exchange
```

同一时间不要让 CLI 和控制台同时争用相同的邮箱池、浏览器或输出文件。

## 7. 可选：本地 Grok2API 中转

中转不是 CLI 注册的必需依赖。要使用控制台里的账号测试、模型、图片/视频和 OpenAI 兼容接口：

1. 启动 Docker Desktop。
2. 准备兼容的 `grok2api` 源码，并在 `.env` 设置其绝对路径：

   ```dotenv
   GROK2API_PATH=/absolute/path/to/grok2api-main
   ```

3. 启动 FastAPI 后端。后端会按中转配置准备 Docker 容器。
4. 在控制台的“本地中转”页面检查状态、同步账号和查看模型。

中转配置、管理员密钥和账号数据会写入 `output/`，不要提交或公开。当前默认账号数据库是 SQLite，不需要为了启动注册任务执行 `docker compose up`。

## 8. 停止服务

- 前端终端按 `Ctrl+C` 停止 Vite。
- 后端终端按 `Ctrl+C` 停止 FastAPI；先在控制台停止正在运行的注册任务。
- 不要直接杀掉不确定归属的浏览器或 Docker 容器进程。

## 9. 快速检查

```bash
curl -fsS http://127.0.0.1:43187/healthz
curl -fsS http://127.0.0.1:43187/api/openapi.json >/dev/null
```

端口被占用时，先检查监听者：

```bash
lsof -nP -iTCP:43187 -sTCP:LISTEN
lsof -nP -iTCP:43188 -sTCP:LISTEN
```

如果前端显示无法连接，先确认后端端口和 `VITE_DEV_API_TARGET` 一致，再查看 `output/backend.log` 或 `output/grok2api-relay.log` 的最后几行。日志中可能包含敏感信息，不要整份上传。

## 10. 提交前安全检查

提交前只检查源代码、测试和文档，不要把本地运行数据加入 Git：

```bash
git status --short
git diff --check
git diff --cached --name-only
```

以下内容必须留在本机或仓库外部：`.env`、邮箱 Token/密码、Outlook 或 Google refresh token、浏览器 Cookie、SSO、OAuth 凭证、账号池、代理列表、`output/`、SQLite 数据库和 `relay-config.json`。模板 `.env.example` 可以提交，但只能包含空值或示例占位符。
