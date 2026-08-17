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
if command -v pnpm >/dev/null 2>&1; then
  (cd web && pnpm install --frozen-lockfile && pnpm build)
elif command -v npm >/dev/null 2>&1; then
  (cd web && npm install --no-package-lock && npm run build)
else
  printf '%s\n' '错误：需要 pnpm 或 npm。' >&2
  exit 1
fi

printf '%s\n' '[4/4] 构建本项目自有网关镜像'
GATEWAY_REVISION=$(tr -d '[:space:]' < gateway/UPSTREAM_REVISION)
docker build --pull --no-cache \
  --build-arg "GATEWAY_SOURCE_REVISION=$GATEWAY_REVISION" \
  --tag grok-account-manager-gateway:local \
  gateway

printf '%s\n' '更新完成。运行 ./scripts/update-and-run.sh 启动 FastAPI；注册机位于控制台侧边栏。'
