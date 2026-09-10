#!/bin/sh
set -eu

# The first-run entry point: update the repository, build the UI, prepare the
# official candidate plus bundled fallback, then start the production FastAPI.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$SCRIPT_DIR/update-and-run.sh" "$@"
