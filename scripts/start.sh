#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' '错误：未找到 uv。请先执行一次：curl -LsSf https://astral.sh/uv/install.sh | sh' >&2
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  printf '%s\n' '已根据 .env.example 生成 .env，可直接修改邮箱与代理配置。'
fi

printf '%s\n' '[1/3] 同步 Python 依赖'
uv sync

printf '%s\n' '[2/3] 准备前端'
WEB_DIR="$ROOT_DIR/web"
if [ ! -d "$WEB_DIR/node_modules" ]; then
  if command -v pnpm >/dev/null 2>&1; then
    (cd "$WEB_DIR" && pnpm install --frozen-lockfile)
  elif command -v npm >/dev/null 2>&1; then
    (cd "$WEB_DIR" && npm install --no-package-lock)
  else
    printf '%s\n' '错误：需要 pnpm 或 npm 来构建前端。' >&2
    exit 1
  fi
fi
if [ ! -d "$WEB_DIR/dist" ] || [ -n "$(find "$WEB_DIR/src" -newer "$WEB_DIR/dist/index.html" -type f 2>/dev/null | head -1)" ]; then
  if command -v pnpm >/dev/null 2>&1; then
    (cd "$WEB_DIR" && pnpm build)
  else
    (cd "$WEB_DIR" && npm run build)
  fi
fi

printf '%s\n' '[3/3] 启动完整应用（后端 + 内置网关 + 已构建前端）'
exec uv run grok-account-manager-api --open "$@"
