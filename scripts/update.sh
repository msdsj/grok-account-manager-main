#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

if ! command -v git >/dev/null 2>&1; then
  printf '%s\n' '错误：需要 Git。' >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' '错误：需要 Docker Desktop。' >&2
  exit 1
fi

printf '%s\n' '[1/4] 拉取本仓库最新代码'
git pull --ff-only

printf '%s\n' '[2/4] 同步 Python 依赖'
if command -v uv >/dev/null 2>&1; then
  uv sync
else
  printf '%s\n' '错误：需要 uv。' >&2
  exit 1
fi

printf '%s\n' '[3/4] 构建本项目控制台前端'
WEB_DIR="$ROOT_DIR/web"
FRONTEND_LOCK_STAMP=$(cksum "$WEB_DIR/pnpm-lock.yaml")
FRONTEND_INSTALL_MARKER="$WEB_DIR/node_modules/.grok-account-manager-lock"
if [ "${GROK_ACCOUNT_MANAGER_FORCE_FRONTEND_INSTALL:-0}" = "1" ] || [ ! -f "$FRONTEND_INSTALL_MARKER" ] || [ "$(cat "$FRONTEND_INSTALL_MARKER" 2>/dev/null || true)" != "$FRONTEND_LOCK_STAMP" ]; then
  if command -v pnpm >/dev/null 2>&1; then
    (cd "$WEB_DIR" && pnpm install --frozen-lockfile)
  elif command -v npm >/dev/null 2>&1; then
    (cd "$WEB_DIR" && npm install --no-package-lock)
  else
    printf '%s\n' '错误：需要 pnpm 或 npm。' >&2
    exit 1
  fi
  printf '%s\n' "$FRONTEND_LOCK_STAMP" > "$FRONTEND_INSTALL_MARKER"
else
  printf '%s\n' '前端依赖未变化，跳过依赖安装（如需强制安装可设置 GROK_ACCOUNT_MANAGER_FORCE_FRONTEND_INSTALL=1）。'
fi
if command -v pnpm >/dev/null 2>&1; then
  (cd "$WEB_DIR" && pnpm build)
elif command -v npm >/dev/null 2>&1; then
  (cd "$WEB_DIR" && npm run build)
else
  printf '%s\n' '错误：需要 pnpm 或 npm。' >&2
  exit 1
fi

printf '%s\n' '[4/4] 构建本项目自有网关镜像'
GATEWAY_REVISION=$(tr -d '[:space:]' < gateway/UPSTREAM_REVISION)
if [ "${GROK_ACCOUNT_MANAGER_FORCE_REBUILD:-0}" = "1" ]; then
  printf '%s\n' '执行全量 Docker 重建（GROK_ACCOUNT_MANAGER_FORCE_REBUILD=1）。'
  docker build --pull --no-cache \
    --build-arg "GATEWAY_SOURCE_REVISION=$GATEWAY_REVISION" \
    --tag grok-account-manager-gateway:local \
    gateway
elif [ "${GROK_ACCOUNT_MANAGER_PULL:-0}" = "1" ]; then
  docker build --pull \
    --build-arg "GATEWAY_SOURCE_REVISION=$GATEWAY_REVISION" \
    --tag grok-account-manager-gateway:local \
    gateway
else
  printf '%s\n' '执行增量 Docker 构建（如需拉取基础镜像可设置 GROK_ACCOUNT_MANAGER_PULL=1）。'
  docker build \
    --build-arg "GATEWAY_SOURCE_REVISION=$GATEWAY_REVISION" \
    --tag grok-account-manager-gateway:local \
    gateway
fi

printf '%s\n' '更新完成。运行 ./scripts/update-and-run.sh 启动 FastAPI；注册机位于控制台侧边栏。'
