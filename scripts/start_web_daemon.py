#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: start_web_daemon.py <root_dir> <pid_file> <log_file>", file=sys.stderr)
        return 2

    root_dir = Path(sys.argv[1]).resolve()
    pid_file = Path(sys.argv[2]).resolve()
    log_file = Path(sys.argv[3]).resolve()
    command = root_dir / ".venv" / "bin" / "grok-account-manager-api"
    if not command.exists():
        command = root_dir / ".venv" / "bin" / "grok-account-manager-web"

    if not command.exists():
        print(f"missing command: {command}", file=sys.stderr)
        return 1

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    log_handle = log_file.open("ab", buffering=0)
    process = subprocess.Popen(
        [str(command)],
        cwd=str(root_dir),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    pid_file.write_text(str(process.pid), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
