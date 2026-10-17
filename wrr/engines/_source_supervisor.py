"""Private POSIX child supervisor; no protocol parsing or persistent state.

The leader stays alive until its entire group receives SIGKILL. This pins the
process-group identity even when the configured executable exits first.
"""
import json
import os
import signal
import subprocess
import sys
import time


def main():
    receipt, *command = sys.argv[1:]
    if os.name != "posix" or os.getpid() != os.getpgrp() or not command:
        return 126
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    child = None
    try:
        child = subprocess.Popen(command)
        while child.poll() is None and not stopping:
            time.sleep(0.01)
        if child.returncode is not None:
            with open(receipt, "x", encoding="utf-8") as stream:
                json.dump({"returncode": child.returncode}, stream)
    except (OSError, ValueError):
        with open(receipt, "x", encoding="utf-8") as stream:
            json.dump({"returncode": 127}, stream)
    finally:
        # Signal our own, still-owned group; never use a persisted/reaped PID.
        os.killpg(os.getpgrp(), signal.SIGTERM)
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            if child is not None:
                child.poll()
            time.sleep(0.01)
        os.killpg(os.getpgrp(), signal.SIGKILL)
    return 126


if __name__ == "__main__":
    raise SystemExit(main())
