from pathlib import Path
import argparse
import sys
import time
from .sandbox import Sandbox
from .store import atomic_json


def main():
    parser = argparse.ArgumentParser(description="Remove only expired sandboxes belonging to this Rewind instance.")
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/rewind"))
    args = parser.parse_args()
    sandbox = Sandbox(args.state_dir / "work")
    result = sandbox.collect()
    atomic_json(args.state_dir / "cleanup.json", {"at": int(time.time() * 1000), **result})
    if not result["confirmed"]:
        print("Rewind-owned sandbox removal could not be confirmed.", file=sys.stderr)
        return 1
    print("Rewind-owned expired sandbox cleanup confirmed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
