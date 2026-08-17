#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$SCRIPT_DIR/update.sh"
cd "$SCRIPT_DIR/.."
exec uv run grok-account-manager-api "$@"
