#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

if ! command -v git >/dev/null 2>&1; then
  printf '%s\n' '错误：需要 Git。' >&2
  exit 1
fi

printf '%s\n' '[1/3] 拉取本仓库最新代码'
git pull --ff-only

printf '%s\n' '[2/3] 同步 Python 依赖'
if command -v uv >/dev/null 2>&1; then
  uv sync
else
  printf '%s\n' '错误：需要 uv。' >&2
  exit 1
fi

printf '%s\n' '[3/3] 构建控制台前端'
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

printf '%s\n' '更新完成。运行 ./scripts/update-and-run.sh 启动 FastAPI；注册机与网关均在同一条 Python 进程内。'
