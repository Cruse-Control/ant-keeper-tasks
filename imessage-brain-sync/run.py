#!/usr/bin/env python3
"""
iMessage Brain Sync — scheduled task (run.py)

Calls brain.py from the antfarm-brain project to fetch iMessages between
Wyler and Flynn Cruse and post them to Discord #imessages.

Watermark-based: brain.py tracks the last message timestamp per contact,
so only new messages since the last run are posted. Safe to run multiple
times — no duplicate posts.

Exit 0 = success (including "no new messages"). Exit 1 = error.
"""

import subprocess
import sys
from pathlib import Path

ANTFARM_DIR = Path("/Users/wylerzahm/Desktop/CruseControl/antfarm-brain")
BRAIN_SCRIPT = ANTFARM_DIR / "skills" / "imessage-brain" / "brain.py"

CONTACT = "Flynn Cruse"
CHANNEL = "imessages"


def main():
    if not ANTFARM_DIR.exists():
        print(f"ERROR: antfarm-brain directory not found: {ANTFARM_DIR}")
        sys.exit(1)

    if not BRAIN_SCRIPT.exists():
        print(f"ERROR: brain.py not found: {BRAIN_SCRIPT}")
        sys.exit(1)

    print(f"Syncing iMessages with {CONTACT} → #{CHANNEL}")

    result = subprocess.run(
        [
            sys.executable, str(BRAIN_SCRIPT),
            "--contact", CONTACT,
            "--channel", CHANNEL,
        ],
        cwd=str(ANTFARM_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr, file=sys.stderr)

    if result.returncode != 0:
        print(f"brain.py exited with code {result.returncode}", file=sys.stderr)
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
