# 首次运行

项目后端是单一 Python FastAPI 进程，注册任务、账号管理、三网关 `/v1` 调用与审计都在其中完成，不依赖 Docker、Go 网关或 Node sidecar。

## 环境

- macOS、Linux 或 Windows WSL
- Python 3.12 或 3.13
- [uv](https://docs.astral.sh/uv/)
- Node.js 20+，以及 pnpm 11+ 或 npm
- 注册功能需要 Chrome/Chromium

## 一键启动

```bash
git clone <你的项目 GitHub 地址> grok-account-manager
cd grok-account-manager
cp .env.example .env
./scripts/start.sh
```

`start.sh` 会拉取最新代码、同步 Python 依赖、安装并构建 React 控制台，然后启动 FastAPI。首次安装依赖需要联网。

打开：<http://127.0.0.1:43187>

默认管理员账号是 `admin` / `admin`，可在 `.env` 中用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 覆盖首次初始化的值；登录后也可以在控制台修改密码。

登录后：

- 侧边栏“注册机”进入注册任务页面，邮箱、代理、并发和 OAuth 设置都由本地 API 处理。
- “账号标签”“模型路由”“密钥”“请求审计”管理账号与内置网关。
- “设置”管理邮箱配置、网关状态和界面主题。

## 开发模式

需要修改前端时，另开一个终端：

```bash
cd grok-account-manager
uv run grok-account-manager-api
cd web
npm install
npm run dev
```

打开 <http://localhost:43188>。Vite 会把 `/api`、`/v1`、`/healthz`、`/readyz` 代理到 FastAPI。

## 后续更新

```bash
./scripts/update.sh
```

脚本只做三件事：拉取代码、同步 Python 依赖、构建前端。

## 数据与停止

- 账号数据库位于 `output/grok-account-manager.db`，是账号、标签、日志与网关审计的唯一来源。
- 按 `Ctrl+C` 停止 FastAPI；再按 `Ctrl+C` 停止 Vite。
- 备份时以 SQLite 数据库和 `output/` 下的邮箱/代理池文件为准，不要提交到 Git。
