"""Start one interactive gate lane for a seeded gate: `make lane GATE=N1` (or `python scripts/run_lane.py N1`).

Reads the gate's device key and direction from .devices.local.json (written by `make seed`) and runs gatectl.
"""

import json
import os
import sys
from pathlib import Path

DEVICES_FILE = Path(__file__).resolve().parents[1] / ".devices.local.json"


def main() -> int:
    gate = sys.argv[1] if len(sys.argv) > 1 else "N1"
    if not DEVICES_FILE.exists():
        print("No .devices.local.json yet: run `make seed` first.", file=sys.stderr)
        return 1
    devices = json.loads(DEVICES_FILE.read_text())["devices"]
    device = next((d for d in devices if d["gate_code"] == gate), None)
    if device is None:
        print(f"Unknown gate {gate!r}; seeded gates: {', '.join(d['gate_code'] for d in devices)}", file=sys.stderr)
        return 1
    env = {**os.environ, "GATE_API_KEY": device["api_key"], "GATE_LANE": device["direction"].lower()}
    project = str(DEVICES_FILE.parent / "gate-controller")
    os.execvpe("uv", ["uv", "run", "--project", project, "gatectl"], env)  # noqa: S606, S607 (dev tool, fixed args)


if __name__ == "__main__":
    sys.exit(main())
