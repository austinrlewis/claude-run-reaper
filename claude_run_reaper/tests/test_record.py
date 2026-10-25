"""What lands in the run record.

The record is the ONLY thing task-monitor-refresh reads, so what it omits is
invisible forever.

    python claude_run_reaper/tests/test_record.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_run_reaper import config, reap, run                             # noqa: E402
from claude_run_reaper.tests.test_reap import (APP_RENDERER, INTERACTIVE,   # noqa: E402
                                               SCHEDULED)

MT = ZoneInfo("America/Denver")
NOW = datetime(2026, 8, 17, 13, 12, 0, tzinfo=MT)


def _proc(pid: int, hours_ago: float, command_line: str) -> reap.Proc:
    return reap.Proc(pid=pid, started=NOW - timedelta(hours=hours_ago),
                     command_line=command_line)


def _record(killed, spared) -> dict:
    original = config.LOG_DIR
    with tempfile.TemporaryDirectory() as tmp:
        config.LOG_DIR = Path(tmp)
        try:
            run._write_record("exit 0", killed, spared)
            written = sorted(config.LOG_DIR.glob("*.json"))
            assert len(written) == 1, "expected exactly one record"
            return json.loads(written[0].read_text(encoding="utf-8"))
        finally:
            config.LOG_DIR = original


def test_a_near_miss_is_named_and_the_app_processes_are_only_counted():
    kill, keep = reap.classify(
        [_proc(1, 0.5, SCHEDULED),        # near miss: real run, too young
         _proc(2, 40.0, INTERACTIVE),     # unrelated
         _proc(3, 40.0, APP_RENDERER)],   # unrelated
        NOW)
    record = _record(kill, keep)
    assert [row["pid"] for row in record["spared"]] == [1]
    assert record["unrelatedProcesses"] == 2


def test_a_kill_is_recorded_with_its_age_and_reason():
    kill, keep = reap.classify([_proc(29092, 10.1, SCHEDULED)], NOW)
    record = _record(kill, keep)
    assert len(record["killed"]) == 1
    row = record["killed"][0]
    assert row["pid"] == 29092
    assert row["ageHours"] == 10.1
    assert "resident" in row["reason"]


def test_a_quiet_run_still_writes_a_record():
    # Silence has to be provable. A run that wrote nothing is indistinguishable
    # from a run that never fired, which is the failure this whole thing exists
    # to stop being invisible.
    record = _record([], [])
    assert record["killed"] == []
    assert record["outcome"] == "exit 0"
    assert record["ranAt"]


def test_the_record_prunes_to_the_cap():
    original = config.LOG_DIR
    with tempfile.TemporaryDirectory() as tmp:
        config.LOG_DIR = Path(tmp)
        try:
            for i in range(config.KEEP_RECORDS + 5):
                (config.LOG_DIR / ("2026-01-01_%06d.json" % i)).write_text("{}", encoding="utf-8")
            run._prune()
            assert len(list(config.LOG_DIR.glob("*.json"))) == config.KEEP_RECORDS
        finally:
            config.LOG_DIR = original


if __name__ == "__main__":
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("  PASS  %s" % name)
        except Exception as exc:
            failed += 1
            print("  FAIL  %s\n          %s: %s" % (name, type(exc).__name__, exc))
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
