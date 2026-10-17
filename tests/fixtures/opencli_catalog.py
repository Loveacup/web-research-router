#!/usr/bin/env python3
"""Finite standalone OpenCLI-shaped catalog fixture for process-boundary tests."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

_CATALOG = (
    {
        "title": "Mandarin Guide",
        "url": "https://fixture.invalid/mandarin",
        "snippet": "Citrus catalog entry",
    },
    {
        "title": "Cedar Guide",
        "url": "https://fixture.invalid/cedar",
        "snippet": "Evergreen catalog entry",
    },
)


def _write_text(path_text: str | None, text: str) -> None:
    if path_text:
        Path(path_text).write_text(text, encoding="utf-8")


def _event(path_text: str | None, name: str, **fields: object) -> None:
    if not path_text:
        return
    record = {"event": name, "pid": os.getpid(), **fields}
    with Path(path_text).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()


def _spawn_owned_child(pid_file: str | None, event_file: str | None) -> subprocess.Popen[bytes]:
    code = """
import json
import os
from pathlib import Path
import signal
import sys
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
event_file = sys.argv[1]
if event_file:
    with Path(event_file).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'event': 'child_started', 'pid': os.getpid()}, sort_keys=True) + '\\n')
        stream.flush()
while True:
    time.sleep(1)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, event_file or ""],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    _write_text(pid_file, str(child.pid))
    _event(event_file, "child_spawned", child_pid=child.pid)
    return child


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    failure = parser.add_mutually_exclusive_group()
    failure.add_argument("--nonzero", action="store_true")
    failure.add_argument("--malformed", action="store_true")
    failure.add_argument("--auth-required", action="store_true")
    failure.add_argument("--pending", action="store_true")
    parser.add_argument("--spawn-child", action="store_true")
    parser.add_argument("--pid-file")
    parser.add_argument("--child-pid-file")
    parser.add_argument("--event-file")
    parser.add_argument("--environment-file")
    parser.add_argument("query")
    parser.add_argument("-f", "--format", choices=("json",), required=True)
    parser.add_argument("--limit", type=int, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    _write_text(args.pid_file, str(os.getpid()))
    _event(args.event_file, "started", query=args.query, limit=args.limit)

    if args.environment_file:
        observation = {
            "home": os.environ.get("HOME"),
            "cwd": os.getcwd(),
            "shared_present": os.environ.get("FIXTURE_SHARED_SENTINEL") == "shared-ok",
            "private_present": "WRR_FIXTURE_PRIVATE_SENTINEL" in os.environ,
            "pythonpath_present": "PYTHONPATH" in os.environ,
            "shell_present": "SHELL" in os.environ,
        }
        _write_text(args.environment_file, json.dumps(observation, sort_keys=True))

    if args.spawn_child:
        _spawn_owned_child(args.child_pid_file, args.event_file)

    def stop(signum: int, _frame: object) -> None:
        _event(args.event_file, "stopping", signal=signum)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    if args.pending:
        _event(args.event_file, "pending")
        while True:
            time.sleep(1)
    if args.auth_required:
        print("AUTH_REQUIRED", file=sys.stderr)
        _event(args.event_file, "auth_required")
        return 3
    if args.nonzero:
        print("fixture requested failure", file=sys.stderr)
        _event(args.event_file, "nonzero")
        return 7
    if args.malformed:
        print("{not-json")
        _event(args.event_file, "malformed")
        return 0

    needle = args.query.casefold()
    matches = [
        record
        for record in _CATALOG
        if needle in " ".join(record.values()).casefold()
    ][: max(0, args.limit)]
    print(json.dumps(matches, sort_keys=True))
    _event(args.event_file, "completed", count=len(matches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
