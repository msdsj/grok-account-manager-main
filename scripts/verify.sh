#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

printf '%s\n' '[1/5] 同步 Python 依赖'
uv sync

printf '%s\n' '[2/5] Python 编译检查'
uv run python -m compileall -q serve tests

printf '%s\n' '[3/5] 后端单元测试'
uv run python -m unittest discover -s tests

printf '%s\n' '[4/5] 前端构建'
(cd web && npm run build)

printf '%s\n' '[5/5] 启动冒烟检查'
SMOKE_DB=$(mktemp -t grok-account-manager-smoke.XXXXXX)
rm -f "$SMOKE_DB"
GROK_ACCOUNT_MANAGER_DB_PATH="$SMOKE_DB" ADMIN_PASSWORD=admin uv run python -m uvicorn \
  grok_account_manager.api.app:app --host 127.0.0.1 --port 43999 --log-level warning &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  rm -f "$SMOKE_DB" "$SMOKE_DB-wal" "$SMOKE_DB-shm"
}
trap cleanup EXIT INT TERM

ok=0
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:43999/healthz >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 1
done
if [ "$ok" != "1" ]; then
  printf '%s\n' '错误：后端未能在 30 秒内通过 /healthz 检查。' >&2
  exit 1
fi

auth_args=()
default_key=$(curl -s http://127.0.0.1:43999/api/gateway/keys/default | sed -n 's/.*"key":"\([^"]*\)".*/\1/p')
if [ -n "$default_key" ]; then
  auth_args=(-H "Authorization: Bearer $default_key")
fi

for path in /healthz /readyz /v1/models /api/gateway/models /api/admin/v1/dashboard; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "${auth_args[@]}" "http://127.0.0.1:43999$path")
  printf '  %-32s %s\n' "$path" "$code"
  if [ "$code" != "200" ]; then
    printf '%s\n' "错误：$path 返回 $code。" >&2
    exit 1
  fi
done

printf '%s\n' '验证完成：后端、数据库、内置网关与前端构建均正常。'
