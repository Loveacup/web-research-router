#!/bin/sh
""":"
# Prefer the repository virtual environment, then a modern interpreter on PATH.
case "$0" in
  */*) WRR_ROOT=${0%/*} ;;
  *) WRR_ROOT=. ;;
esac
WRR_ROOT=$(cd "$WRR_ROOT" && pwd -P)
for WRR_PYTHON in \
  "$WRR_ROOT/.venv/bin/python" \
  "$WRR_ROOT/venv/bin/python" \
  python3.14 python3.13 python3.12 python3.11 python3
do
  if [ -x "$WRR_PYTHON" ]; then
    :
  else
    WRR_PYTHON=$(command -v "$WRR_PYTHON" 2>/dev/null) || continue
  fi
  if "$WRR_PYTHON" -c 'import sys; raise SystemExit(not sys.version_info >= (3, 11))' 2>/dev/null
  then
    exec "$WRR_PYTHON" "$0" "$@"
  fi
done
printf '%s\n' 'wrr-cli.py requires Python 3.11+; create .venv/venv or install python3.11+ on PATH.' >&2
exit 126
":"""
"""wrr-cli: Web Research Router command-line entrypoint."""

import os
import sys

if sys.version_info < (3, 11):
    print(
        "wrr-cli.py requires Python 3.11+; create .venv/venv or install "
        "python3.11+ on PATH.",
        file=sys.stderr,
    )
    raise SystemExit(126)

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from wrr._cli import main

if __name__ == "__main__":
    sys.exit(main())
