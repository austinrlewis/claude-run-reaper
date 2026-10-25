"""The idle-transcript rule: does it kill finished runs sooner without ever
killing a run that is still working?

The four scenarios pinned below are the real ones measured on Aug 18, 2026 by
reading the transcripts of every run reaped in the preceding day. They are the
reason this rule exists, so they are the first thing that has to stay true.

    python claude_run_reaper/tests/test_idle.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_run_reaper import config, reap, transcripts                     # noqa: E402

MT = ZoneInfo("America/Denver")
NOW = datetime(2026, 8, 18, 10, 0, 0, tzinfo=MT)
# The tests build REAL files, and on Windows st_ctime is the real moment of
# creation - os.utime moves mtime only, never ctime. So any assertion that goes
# through transcripts.find(), which matches on CREATION time, has to ask about
# the real clock; only the idle arithmetic gets the frozen NOW.
#
# Using NOW for both is what made four tests here go red on 2026-08-18 at 20:00,
# ten hours after the frozen NOW passed out of the 10-hour match window, and
# they stayed red and unnoticed for a week - found 2026-08-25. A test whose
# failure is scheduled by the calendar is worse than no test: it passes in the
# session that writes it and rots quietly afterwards.
REAL_NOW = datetime.now(MT)

_CODE = r'"C:\Users\me\AppData\Roaming\Claude\claude-code\2.1.229\claude.exe"'

SCHEDULED = (_CODE + " --output-format stream-json --verbose --input-format "
             "stream-json --model default --permission-prompt-tool stdio "
             "--disallowedTools AskUserQuestion --permission-mode default")

INTERACTIVE = (_CODE + " --output-format stream-json --effort high "
               "--model claude-opus-5 --permission-mode auto")


def _proc(pid: int, hours_ago: float, idle: float | None,
          command_line: str = SCHEDULED) -> reap.Proc:
    return reap.Proc(pid=pid, started=NOW - timedelta(hours=hours_ago),
                     command_line=command_line, idle_minutes=idle)


def _killed(proc: reap.Proc) -> bool:
    kill, _ = reap.classify([proc], NOW)
    return len(kill) == 1


def _reason(proc: reap.Proc) -> str:
    kill, keep = reap.classify([proc], NOW)
    return (kill + keep)[0].reason


# --- the four runs that motivated the rule ------------------------------------

def test_pricing_review_finished_in_37s_and_hung_is_killed_at_25min():
    # Would have waited 5 hours under the old age-only rule.
    assert _killed(_proc(17524, hours_ago=0.5, idle=25.0))


def test_spam_sweep_finished_in_6min_and_hung_is_killed():
    assert _killed(_proc(29276, hours_ago=1.0, idle=45.0))


def test_tracker_sync_idle_3h_is_killed_without_waiting_for_the_floor():
    proc = _proc(22816, hours_ago=3.7, idle=200.0)
    assert _killed(proc)
    assert "did not exit" in _reason(proc)


def test_a_run_that_exited_normally_is_simply_not_present():
    # Nothing to assert on a process that is gone - the guard is that an EMPTY
    # process list must never produce a kill.
    kill, keep = reap.classify([], NOW)
    assert kill == [] and keep == []


# --- the rule must not fire early ---------------------------------------------

def test_a_working_run_is_never_killed_however_young():
    assert not _killed(_proc(1, hours_ago=0.1, idle=0.0))


def test_a_run_quiet_but_under_the_idle_threshold_is_spared():
    assert not _killed(_proc(2, hours_ago=0.5,
                             idle=config.IDLE_MINUTES - 1))


def test_exactly_at_the_idle_threshold_is_spared():
    # Strictly greater-than, so the boundary is not a kill.
    assert not _killed(_proc(3, hours_ago=0.5, idle=config.IDLE_MINUTES))


# --- the long-run reprieve, which is the safety half of the change ------------

def test_a_long_run_still_writing_is_spared_past_the_age_floor():
    proc = _proc(4, hours_ago=config.MAX_AGE_HOURS + 2, idle=1.0)
    assert not _killed(proc)
    assert "still writing" in _reason(proc)


def test_the_reprieve_ends_at_the_hard_ceiling():
    assert _killed(_proc(5, hours_ago=config.HARD_MAX_AGE_HOURS + 1, idle=1.0))


def test_a_long_run_that_went_quiet_gets_no_reprieve():
    assert _killed(_proc(6, hours_ago=config.MAX_AGE_HOURS + 2, idle=90.0))


# --- unknown idle must reproduce the old behaviour exactly --------------------

def test_unknown_idle_under_the_floor_is_spared():
    assert not _killed(_proc(7, hours_ago=config.MAX_AGE_HOURS - 1, idle=None))


def test_unknown_idle_over_the_floor_is_killed():
    assert _killed(_proc(8, hours_ago=config.MAX_AGE_HOURS + 1, idle=None))


def test_unknown_idle_never_grants_the_reprieve():
    # A missing transcript must not become a licence to run forever.
    proc = _proc(9, hours_ago=config.MAX_AGE_HOURS + 0.5, idle=None)
    assert _killed(proc)
    assert "still writing" not in _reason(proc)


# --- the older guarantees still hold ------------------------------------------

def test_idle_does_not_override_the_signature():
    # An interactive session sitting idle for hours is the owner thinking, not a leak.
    assert not _killed(_proc(10, hours_ago=8.0, idle=300.0, command_line=INTERACTIVE))


def test_idle_does_not_override_protection():
    remote = reap.Proc(pid=11, started=NOW - timedelta(hours=40),
                       command_line=_CODE + " --remote-control", parent_pid=0)
    child = reap.Proc(pid=12, started=NOW - timedelta(hours=30),
                      command_line=SCHEDULED, parent_pid=11, idle_minutes=999.0)
    kill, _ = reap.classify([remote, child], NOW)
    assert kill == []


def test_idle_does_not_override_the_self_pid_guard():
    kill, _ = reap.classify([_proc(13, hours_ago=9.0, idle=600.0)], NOW,
                            self_pid=13)
    assert kill == []


def test_the_runaway_guard_still_counts_idle_kills():
    procs = [_proc(100 + i, hours_ago=0.5, idle=90.0)
             for i in range(config.MAX_KILLS_PER_RUN + 1)]
    try:
        reap.classify(procs, NOW)
    except reap.TooManyKills:
        pass
    else:
        raise AssertionError("an oversized sweep of idle kills got through")


def test_the_record_carries_idle_minutes():
    kill, _ = reap.classify([_proc(14, hours_ago=1.0, idle=42.4)], NOW)
    assert kill[0].as_dict()["idleMinutes"] == 42.4


def test_the_record_carries_null_when_idle_is_unknown():
    kill, _ = reap.classify([_proc(15, hours_ago=9.0, idle=None)], NOW)
    assert kill[0].as_dict()["idleMinutes"] is None


# --- the conversation carve-out -----------------------------------------------
# A scheduled run the owner replies to keeps the scheduled command line. Without
# this carve-out, a 20-minute pause while he reads would reap the session he is
# talking to. Found on 2026-08-18 against PID 24624, a live example.

def _chat(pid: int, hours_ago: float, idle: float | None) -> reap.Proc:
    return reap.Proc(pid=pid, started=NOW - timedelta(hours=hours_ago),
                     command_line=SCHEDULED, idle_minutes=idle,
                     is_conversation=True)


def test_a_conversation_is_not_reaped_for_a_long_pause():
    assert not _killed(_chat(20, hours_ago=2.0, idle=240.0))


def test_a_conversation_still_faces_the_age_floor():
    # No regression in the other direction: the carve-out must not make a
    # conversation immortal, only restore the pre-idle-rule behaviour.
    assert _killed(_chat(21, hours_ago=config.MAX_AGE_HOURS + 1, idle=240.0))


def test_a_conversation_under_the_floor_is_spared_and_says_why():
    proc = _chat(22, hours_ago=1.0, idle=90.0)
    assert not _killed(proc)
    assert "conversation" in _reason(proc)


# --- a conversation earns the still-writing reprieve (added 2026-09-03) -------
# Before this, a conversation was given no idle reading at all, which made the
# reprieve unreachable for it: the one category where a person is provably
# present was the only one that could never be spared for being actively alive.
# Ten of the reaper's first forty kills were runs the owner had opened to thaw a
# freeze, killed 5.4-5.9h later while he was working in them.

def test_a_working_conversation_past_the_floor_is_spared():
    proc = _chat(23, hours_ago=config.MAX_AGE_HOURS + 1, idle=2.0)
    assert not _killed(proc)
    assert "still writing" in _reason(proc)


def test_a_working_conversation_still_dies_at_the_hard_ceiling():
    # A reprieve, not an exemption - identical to the plain long-run rule.
    assert _killed(_chat(24, hours_ago=config.HARD_MAX_AGE_HOURS + 1, idle=2.0))


def test_a_quiet_conversation_past_the_floor_forfeits_the_reprieve():
    # Quiet past the idle threshold means he walked away, or never unstuck the
    # freeze. Either way the age floor gets it.
    assert _killed(_chat(25, hours_ago=config.MAX_AGE_HOURS + 1,
                         idle=config.IDLE_MINUTES + 1))


def test_a_conversation_with_no_idle_reading_still_dies_on_the_floor():
    # Unknown idle grants no reprieve to anyone, conversation included.
    assert _killed(_chat(26, hours_ago=config.MAX_AGE_HOURS + 1, idle=None))


def test_annotate_gives_a_conversation_an_idle_reading():
    # The reprieve above is unreachable unless annotate actually supplies one.
    root = _jsonl([_user_text("the task prompt"),
                   _tool_result("some output"),
                   _user_text("why is this still running?")])
    procs = [reap.Proc(pid=1, started=REAL_NOW, command_line=SCHEDULED)]
    out = transcripts.annotate(procs, REAL_NOW, root=root)
    assert out[0].is_conversation
    assert out[0].idle_minutes is not None


def test_one_human_turn_is_a_plain_scheduled_run_not_a_conversation():
    root = _jsonl([_user_text("the task prompt"),
                   _tool_result("some output"),
                   _tool_result("more output")])
    path = transcripts.find(REAL_NOW, root=root, match_seconds=10 * 3600)
    assert transcripts.human_turns(path) == 1


def test_a_reply_makes_it_a_conversation():
    root = _jsonl([_user_text("the task prompt"),
                   _tool_result("some output"),
                   _user_text("so why are these running so long?")])
    path = transcripts.find(REAL_NOW, root=root, match_seconds=10 * 3600)
    assert transcripts.human_turns(path) == 2


def test_tool_results_never_count_as_human_turns():
    root = _jsonl([_user_text("the task prompt")]
                  + [_tool_result("output %d" % i) for i in range(200)])
    path = transcripts.find(REAL_NOW, root=root, match_seconds=10 * 3600)
    assert transcripts.human_turns(path) == 1


def test_an_unreadable_transcript_reports_no_human_turns():
    assert transcripts.human_turns(Path("Z:/nope/missing.jsonl")) == 0


# --- the transcript lookup itself ---------------------------------------------

def _user_text(text: str) -> dict:
    return {"type": "user",
            "message": {"content": [{"type": "text", "text": text}]}}


def _tool_result(text: str) -> dict:
    return {"type": "user",
            "message": {"content": [{"type": "tool_result", "content": text}]}}


def _jsonl(entries: list[dict]) -> Path:
    """A transcript tree holding one session file built from `entries`."""
    root = Path(tempfile.mkdtemp(prefix="reaper_tx_chat_"))
    path = root / "projectA" / "s1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n",
                    encoding="utf-8")
    return root


def _tree(files: dict[str, tuple[float, float]]) -> Path:
    """Build a transcript tree. Values are (created_offset_s, mtime_offset_s)
    relative to NOW, negative meaning earlier."""
    root = Path(tempfile.mkdtemp(prefix="reaper_tx_"))
    for name, (c_off, m_off) in files.items():
        path = root / "projectA" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        stamp = NOW.timestamp()
        os.utime(path, (stamp + m_off, stamp + m_off))
    return root


def test_idle_minutes_reads_the_matching_transcript():
    root = _tree({"s1.jsonl": (0, -1800)})     # last written 30 min ago
    # Matched on the REAL creation clock, measured against the frozen NOW - see
    # the REAL_NOW note at the top.
    idle = transcripts.idle_minutes(REAL_NOW, NOW, root=root,
                                    match_seconds=10 * 3600)
    assert idle is not None and 29.0 < idle < 31.0


def test_a_missing_root_returns_no_opinion():
    assert transcripts.idle_minutes(NOW, NOW,
                                    root=Path("Z:/nope/does/not/exist")) is None


def test_an_empty_tree_returns_no_opinion():
    root = Path(tempfile.mkdtemp(prefix="reaper_tx_empty_"))
    assert transcripts.idle_minutes(NOW, NOW, root=root) is None


def test_an_ambiguous_match_returns_no_opinion():
    # Two transcripts inside the window: guessing between them could spare a
    # leak because a DIFFERENT run is still writing.
    root = _tree({"a.jsonl": (0, -60), "b.jsonl": (0, -60)})
    assert transcripts.find(REAL_NOW, root=root, match_seconds=10 * 3600) is None


def test_annotate_leaves_non_scheduled_processes_alone():
    root = _tree({"s1.jsonl": (0, -60)})
    procs = [reap.Proc(pid=1, started=NOW, command_line=INTERACTIVE)]
    out = transcripts.annotate(procs, NOW, root=root)
    assert out[0].idle_minutes is None


# --- STALLED vs FINISHED (added 2026-08-25) -----------------------------------
# Idle time alone cannot tell them apart, and this program spent a week claiming
# it could. On 2026-08-25 two runs frozen part way through - airbnb-tracker-sync
# 5 minutes in, task-monitor-refresh 3 minutes in - were both recorded as
# "finished scheduled run that did not exit". task-monitor-refresh stays SILENT
# on that label, so both real failures were filed as non-events. The evidence
# that settles it is whether a tool call is still unanswered.

def _tool_use(call_id: str, name: str) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": call_id,
                                     "name": name, "input": {}}]}}


def _answer(call_id: str) -> dict:
    return {"type": "user",
            "message": {"content": [{"type": "tool_result",
                                     "tool_use_id": call_id,
                                     "content": "ok"}]}}


def _one(root: Path) -> Path:
    return next(root.rglob("*.jsonl"))


def test_pending_tool_names_the_call_that_never_came_back():
    root = _jsonl([_user_text("the task prompt"),
                   _tool_use("t1", "Read"), _answer("t1"),
                   _tool_use("t2", "Bash")])
    assert transcripts.pending_tool(_one(root)) == "Bash"


def test_pending_tool_is_none_when_every_call_was_answered():
    root = _jsonl([_user_text("the task prompt"),
                   _tool_use("t1", "Read"), _answer("t1"),
                   _tool_use("t2", "Bash"), _answer("t2")])
    assert transcripts.pending_tool(_one(root)) is None


def test_pending_tool_ignores_the_order_calls_are_answered_in():
    # A run issuing several calls in one block gets them back in any order.
    root = _jsonl([_user_text("the task prompt"),
                   _tool_use("t1", "Read"), _tool_use("t2", "Grep"),
                   _answer("t2"), _answer("t1")])
    assert transcripts.pending_tool(_one(root)) is None


def test_pending_tool_is_none_on_an_unreadable_file():
    # Unknown is NO OPINION, exactly as it is for idle - it must never read as
    # "stalled", or an unreadable transcript would manufacture a false alarm.
    assert transcripts.pending_tool(Path("Z:/nope/does/not/exist.jsonl")) is None


def _stalled(pid: int, hours_ago: float, idle: float, tool: str) -> reap.Proc:
    return reap.Proc(pid=pid, started=NOW - timedelta(hours=hours_ago),
                     command_line=SCHEDULED, idle_minutes=idle,
                     blocked_on=tool)


def test_a_stalled_run_is_killed_but_never_called_finished():
    proc = _stalled(30, hours_ago=0.5, idle=26.0, tool="Bash")
    assert _killed(proc)
    reason = _reason(proc)
    assert "STALLED" in reason and "Did NOT finish" in reason
    assert "finished scheduled run" not in reason


def test_a_stalled_runs_reason_names_the_tool_it_is_stuck_on():
    assert "PowerShell" in _reason(_stalled(31, 0.5, 30.0, "PowerShell"))


def test_a_genuinely_finished_run_keeps_the_finished_label():
    # The silence rule in task-monitor-refresh depends on this staying true for
    # the ordinary case, or every clean kill starts emailing again.
    proc = _proc(32, hours_ago=0.5, idle=26.0)          # blocked_on defaults None
    assert _killed(proc)
    assert "finished scheduled run that did not exit" in _reason(proc)


def test_the_record_carries_blocked_on():
    kill, _ = reap.classify([_stalled(33, 0.5, 26.0, "Bash")], NOW)
    assert kill[0].as_dict()["blockedOn"] == "Bash"


def test_the_record_carries_null_blocked_on_for_a_finished_run():
    kill, _ = reap.classify([_proc(34, hours_ago=0.5, idle=26.0)], NOW)
    assert kill[0].as_dict()["blockedOn"] is None


def test_a_stalled_run_under_the_idle_threshold_is_still_spared():
    # blocked_on must not become a new reason to kill - it only relabels a kill
    # the idle rule was already making.
    assert not _killed(_stalled(35, 0.5, config.IDLE_MINUTES - 1, "Bash"))


def test_annotate_fills_blocked_on_from_the_transcript():
    root = _jsonl([_user_text("the task prompt"), _tool_use("t1", "Bash")])
    procs = [reap.Proc(pid=1, started=REAL_NOW, command_line=SCHEDULED)]
    out = transcripts.annotate(procs, REAL_NOW, root=root)
    assert out[0].blocked_on == "Bash"


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
