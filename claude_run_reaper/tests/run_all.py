"""Run every test suite. Exit non-zero if anything fails.

    python claude_run_reaper/tests/run_all.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

SUITES = ["test_reap", "test_idle", "test_record", "test_wiring", "test_heartbeat"]


def main() -> int:
    failures = []
    for suite in SUITES:
        proc = subprocess.run(
            [sys.executable, str(HERE / (suite + ".py"))],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        tail = [ln for ln in proc.stdout.strip().splitlines() if "passed" in ln]
        summary = tail[-1].strip() if tail else "NO OUTPUT"
        mark = "ok  " if proc.returncode == 0 else "FAIL"
        print("  %s %-14s %s" % (mark, suite, summary))
        if proc.returncode != 0:
            failures.append((suite, proc.stdout, proc.stderr))

    for suite, out, err in failures:
        print("\n--- %s ---\n%s\n%s" % (suite, out.strip(), err.strip()))
    print("\n%s" % ("all suites passed" if not failures
                    else "%d suite(s) failed" % len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
