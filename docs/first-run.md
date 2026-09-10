# 首次运行

本地中转默认使用 `ghcr.io/chenyme/grok2api:latest` 作为官方候选镜像，并已内置
grok2api v3.1.4 源码和 Dockerfile 作为兼容回退。新用户不需要另外克隆或配置
`grok2api` 项目。

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

`start.sh` 会按顺序拉取本项目最新代码、同步 Python 依赖、构建 React 控制台、
预拉取官方候选镜像，并从仓库内 `gateway/` 构建 v3.1.4 回退镜像，然后启动
FastAPI。启动本地中转时还会解析候选的 `RepoDigest` 并执行隔离探针；候选验证
失败时自动使用回退镜像。首次准备镜像和依赖需要联网。

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

脚本会预拉取当前 shell 环境中 `GROK_ACCOUNT_MANAGER_GATEWAY_IMAGE` 指定的候选；
未设置时使用 `ghcr.io/chenyme/grok2api:latest`。要预拉取固定版本，可执行
`GROK_ACCOUNT_MANAGER_GATEWAY_IMAGE=... ./scripts/update.sh`；运行时也可在 `.env`
填写版本 tag 或 digest。无论候选预拉取是否成功，脚本都会继续构建仓库内置的
v3.1.4 回退镜像；实际启动仍以 `RepoDigest` 和隔离探针结果为准。脚本不会读取或
删除电脑上其他项目的源码。

## 数据与停止

- 账号数据库和凭证位于 `output/`；网关数据固定在 `output/grok2api-v2-data/`，更新或镜像回退不会删除它们。
- 按 `Ctrl+C` 停止 FastAPI；再按 `Ctrl+C` 停止 Vite。
- 需要清理本项目容器时，只处理名称以 `grok-account-manager-` 开头的资源，避免误删其他项目。
