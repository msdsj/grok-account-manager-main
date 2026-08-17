#!/bin/sh
set -eu

# The first-run entry point: update the repository, build the UI and bundled
# gateway image, then start the FastAPI service that serves the production UI.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$SCRIPT_DIR/update-and-run.sh" "$@"
