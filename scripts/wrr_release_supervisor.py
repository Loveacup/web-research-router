"""Run one finite isolated WRR release-supervisor operation."""

import sys
from pathlib import Path

if not sys.flags.isolated or not sys.flags.no_site:
    raise SystemExit("refusing non-isolated interpreter startup")

SUPERVISOR_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUPERVISOR_ROOT))

from wrr.cli import release_supervisor

if not Path(release_supervisor.__file__).resolve().is_relative_to(SUPERVISOR_ROOT):
    raise SystemExit("refusing ambient release-supervisor import")


if __name__ == "__main__":
    raise SystemExit(release_supervisor.main())
