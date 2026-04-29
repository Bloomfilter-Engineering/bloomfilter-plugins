#!/usr/bin/env python3
"""Fail if Cursor plugin Python hook scripts drift between OS packages."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
POSIX_PLUGIN = ROOT / "plugins" / "agent-miner-cursor"
WINDOWS_PLUGIN = ROOT / "plugins" / "agent-miner-cursor-windows"
SYNCED_FILES = (
    "scripts/collect_hook.py",
    "scripts/bloomfilter_common.py",
)


def main() -> int:
    failures = []

    for relative_path in SYNCED_FILES:
        posix_file = POSIX_PLUGIN / relative_path
        windows_file = WINDOWS_PLUGIN / relative_path

        if not posix_file.is_file():
            failures.append(f"Missing source file: {posix_file.relative_to(ROOT)}")
            continue
        if not windows_file.is_file():
            failures.append(f"Missing Windows file: {windows_file.relative_to(ROOT)}")
            continue

        if posix_file.read_bytes() != windows_file.read_bytes():
            failures.append(
                "Out of sync: "
                f"{posix_file.relative_to(ROOT)} != {windows_file.relative_to(ROOT)}"
            )

    if failures:
        print("Cursor plugin sync check failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(
            "Copy the shared Python hook scripts from agent-miner-cursor to "
            "agent-miner-cursor-windows, then rerun this check.",
            file=sys.stderr,
        )
        return 1

    print("Cursor plugin Python scripts are in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
