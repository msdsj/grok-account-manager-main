# 首次运行

本项目已经内置网关源码和 Dockerfile。新用户不需要安装或拉取其他
`grok2api` 项目，也不需要预先准备同名 Docker 镜像。

## 环境

- macOS、Linux 或 Windows WSL
- Python 3.12 或 3.13
- [uv](https://docs.astral.sh/uv/)
- Node.js 20+，以及 pnpm 11+ 或 npm
- Docker Desktop，并确认 Docker daemon 已启动
- 注册功能需要 Chrome/Chromium

## 一键启动

```bash
git clone <你的项目 GitHub 地址> grok-account-manager
cd grok-account-manager
cp .env.example .env
./scripts/start.sh
```

`start.sh` 会按顺序拉取本项目最新代码、同步 Python 依赖、构建本项目 React
控制台、从仓库内 `gateway/` 构建 `grok-account-manager-gateway:local`，然后启动
FastAPI。首次构建需要联网下载基础镜像和 Go/Node 依赖。

打开：<http://127.0.0.1:43187>

登录后，侧边栏的“注册机”进入本项目内置的注册任务页面。注册机的邮箱、代理、
并发和 OAuth 设置都由 FastAPI 本地 API 处理，不调用其他项目的注册功能。

## 开发模式

需要修改前端时，另开一个终端：

```bash
cd grok-account-manager
uv run grok-account-manager-api
cd web
npm install
npm run dev
```

打开 <http://localhost:43188>。Vite 只代理本项目的 FastAPI，注册机路由仍然是
`/register`。

## 后续更新

```bash
./scripts/update.sh
```

脚本只更新当前仓库及其 `gateway/` 内置源码。如果 Docker Hub 暂时不可达，镜像
构建会失败并保留源码；网络恢复后重新执行即可。脚本不会读取或删除电脑上其他
项目的镜像和源码。

## 数据与停止

- 账号数据库、凭证和网关数据在 `output/`，更新脚本不会删除它们。
- 按 `Ctrl+C` 停止 FastAPI；再按 `Ctrl+C` 停止 Vite。
- 需要清理本项目容器时，只处理名称以 `grok-account-manager-` 开头的资源，避免误删其他项目。
