"""Correct purchases misrecorded as shorts, while the dashboard is stopped."""
import argparse
import json
from pathlib import Path
import time

from .state_paths import open_positions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path, help="The dashboard's base state path")
    parser.add_argument("--state-backend", choices=("auto", "json", "sqlite"), default="auto")
    parser.add_argument("--settings", type=Path, help="Settings file to include in backup (defaults beside state)")
    args = parser.parse_args()
    with open_positions(args.state, args.state_backend, settings_path=args.settings) as store:
        ids = store.correct_spot_buys(int(time.time()*1000))
        print(json.dumps({"corrected_count": len(ids), "position_ids": ids}))


if __name__ == "__main__":
    main()
