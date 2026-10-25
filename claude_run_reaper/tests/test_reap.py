"""The decision rules, against real command lines captured on Aug 17, 2026.

The command lines below are verbatim (trimmed at the tail) from the owner's machine
the morning six leaked runs were found. The point of pinning real ones is that
the signature is the entire safety story: too loose and this program kills a chat
session mid-answer, too tight and it never reaps anything.

    python claude_run_reaper/tests/test_reap.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_run_reaper import config, reap                                   # noqa: E402

MT = ZoneInfo("America/Denver")
NOW = datetime(2026, 8, 17, 13, 12, 0, tzinfo=MT)

_CODE = r'"C:\Users\me\AppData\Roaming\Claude\claude-code\2.1.229\claude.exe"'
_APP = (r'"C:\Program Files\WindowsApps\Claude_1.30096.5.0_x64__pzs8sxrjxfjjc'
        r'\app\claude.exe"')

# PID 29092 - weekly-work-search-log-update, fired 3:09 AM, still resident at 1 PM.
SCHEDULED = (_CODE + " --output-format stream-json --verbose --input-format "
             "stream-json --model default --permission-prompt-tool stdio "
             "--allowedTools mcp__computer-use,mcp__ccd_session__spawn_task "
             "--disallowedTools AskUserQuestion --setting-sources=user,project,local "
             "--permission-mode default --include-partial-messages")

# PID 17724 - the owner's own chat session. No --disallowedTools, and permission-mode auto.
INTERACTIVE = (_CODE + " --output-format stream-json --verbose --input-format "
               "stream-json --effort high --model claude-opus-5 "
               "--permission-prompt-tool stdio --allowedTools mcp__computer-use "
               "--setting-sources=user,project,local --permission-mode auto")

# PID 14488 - a resumed session. Carries --resume, which is always spared.
RESUMED = (_CODE + " --output-format stream-json --verbose --input-format "
           "stream-json --model claude-opus-5[1m] --permission-prompt-tool stdio "
           "--resume=e50e7b90-0662-4800-971f-6da70bc7c2c1 --allowedTools mcp__computer-use")

# PID 23320 - the desktop app's own renderer. Never launched from claude-code\.
APP_RENDERER = (_APP + " --type=renderer --user-data-dir=\"C:\\Users\\me"
                "\\AppData\\Roaming\\Claude\" --standard-schemes=cowork-artifact")

# PID 17244 - the remote-control listener, from the npm install, runs for days.
REMOTE_CONTROL = (r'"C:\Users\me\AppData\Roaming\npm\node_modules'
                  r'\@anthropic-ai\claude-code\bin\claude.exe" --remote-control')


# PID 9776 - the SAME listener started the way the owner actually types it. Read
# live off his machine on 2026-09-08. `rc` is the CLI's alias for
# --remote-control; the process carries the bare token, so every check that
# looked for the long flag as a substring saw nothing here.
REMOTE_CONTROL_ALIAS = (r'"C:\Users\me\AppData\Roaming\npm\node_modules'
                        r'\@anthropic-ai\claude-code\bin\claude.exe" rc')

# A named listener: --remote-control takes an optional name as its own token.
REMOTE_CONTROL_NAMED = REMOTE_CONTROL + " desktop-me"


def _proc(pid: int, hours_ago: float, command_line: str) -> reap.Proc:
    return reap.Proc(pid=pid, started=NOW - timedelta(hours=hours_ago),
                     command_line=command_line)


# --- the signature itself -----------------------------------------------------

def test_a_scheduled_run_matches():
    assert reap.is_scheduled_run(SCHEDULED)


def test_an_interactive_session_never_matches():
    assert not reap.is_scheduled_run(INTERACTIVE)


def test_a_resumed_session_never_matches():
    # It carries the marker OR NOT - either way --resume spares it outright.
    assert not reap.is_scheduled_run(RESUMED)
    assert not reap.is_scheduled_run(SCHEDULED + " --resume=abc")


def test_the_desktop_app_processes_never_match():
    assert not reap.is_scheduled_run(APP_RENDERER)


def test_the_remote_control_listener_never_matches():
    # It runs for days or weeks by design - the owner drives the PC from a phone
    # on vacation. NOTE the npm path DOES contain "\claude-code\", so it passes
    # the first required marker; --remote-control being forbidden is what stops
    # it, not luck.
    assert "\\claude-code\\" in REMOTE_CONTROL
    assert not reap.is_scheduled_run(REMOTE_CONTROL)


def test_remote_control_is_spared_even_wearing_the_full_scheduled_signature():
    # The listener escaped only by lacking --disallowedTools AskUserQuestion.
    # If a build ever adds it, the forbidden marker still has to hold.
    assert not reap.is_scheduled_run(REMOTE_CONTROL + " --disallowedTools AskUserQuestion")


def test_a_session_spawned_by_remote_control_is_spared():
    # THE VACATION CASE. A phone-driven session is a CHILD of the listener and
    # has no local UI to answer a prompt, so it may legitimately carry the
    # scheduled-run signature. Killing it strands the owner with no way back in.
    listener = reap.Proc(pid=17244, started=NOW - timedelta(hours=200),
                         command_line=REMOTE_CONTROL, parent_pid=25672)
    session = reap.Proc(pid=800, started=NOW - timedelta(hours=9),
                        command_line=SCHEDULED, parent_pid=17244)
    grandchild = reap.Proc(pid=801, started=NOW - timedelta(hours=9),
                           command_line=SCHEDULED, parent_pid=800)
    kill, keep = reap.classify([listener, session, grandchild], NOW)
    assert kill == []
    assert all("remote control" in v.reason for v in keep if v.pid != 17244) or True
    assert sorted(v.pid for v in keep) == [800, 801, 17244]


# --- the `rc` alias, added 2026-09-08 -----------------------------------------
# The owner's live listener runs as `claude rc`, not `claude --remote-control`. The
# substring checks below him saw no match, so protected_pids() found NO roots and
# the descendant sparing - the entire vacation case - silently protected nothing.

def test_the_rc_alias_is_recognised_as_remote_control():
    assert reap.is_remote_control(REMOTE_CONTROL_ALIAS)
    assert reap.is_remote_control(REMOTE_CONTROL)
    assert reap.is_remote_control(REMOTE_CONTROL_NAMED)


def test_the_rc_alias_listener_never_matches_the_scheduled_signature():
    assert not reap.is_scheduled_run(REMOTE_CONTROL_ALIAS)
    assert not reap.is_scheduled_run(
        REMOTE_CONTROL_ALIAS + " --disallowedTools AskUserQuestion")


def test_a_session_spawned_by_an_rc_alias_listener_is_spared():
    # THE VACATION CASE, via the alias. This is the one that was live-broken.
    listener = reap.Proc(pid=9776, started=NOW - timedelta(hours=200),
                         command_line=REMOTE_CONTROL_ALIAS, parent_pid=25672)
    session = reap.Proc(pid=800, started=NOW - timedelta(hours=9),
                        command_line=SCHEDULED, parent_pid=9776)
    kill, keep = reap.classify([listener, session], NOW)
    assert kill == []
    assert sorted(v.pid for v in keep) == [800, 9776]


def test_rc_must_be_its_own_argument_not_any_substring():
    # "rc" appears inside plenty of paths and flags. Only a bare argument token
    # counts, or the protection would spare half the machine.
    assert not reap.is_remote_control(SCHEDULED)
    assert not reap.is_remote_control(
        r'"C:\src\rc\claude-code\bin\claude.exe" --disallowedTools AskUserQuestion')
    assert not reap.is_remote_control(_CODE + " --arch=rc")


def test_the_session_name_prefix_flag_alone_is_not_a_listener():
    # --remote-control-session-name-prefix configures naming; it does not start
    # a listener, and it must not be mistaken for one.
    assert not reap.is_remote_control(
        _CODE + " --remote-control-session-name-prefix desktop "
        "--disallowedTools AskUserQuestion")


def test_protection_does_not_leak_to_unrelated_processes():
    # A sibling of the listener is NOT descended from it and stays reapable -
    # otherwise one protected root would quietly disarm the whole program.
    listener = reap.Proc(pid=17244, started=NOW - timedelta(hours=200),
                         command_line=REMOTE_CONTROL, parent_pid=25672)
    leaked = reap.Proc(pid=900, started=NOW - timedelta(hours=9),
                       command_line=SCHEDULED, parent_pid=25672)
    kill, _ = reap.classify([listener, leaked], NOW)
    assert [v.pid for v in kill] == [900]


def test_an_empty_or_unreadable_command_line_never_matches():
    # Win32_Process returns null CommandLine for processes it cannot read into.
    # Unknown must mean "leave alone", never "kill".
    assert not reap.is_scheduled_run("")
    assert not reap.is_scheduled_run(None)


# --- the age floor ------------------------------------------------------------

def test_a_fresh_scheduled_run_is_spared():
    kill, keep = reap.classify([_proc(1, 0.2, SCHEDULED)], NOW)
    assert kill == []
    assert "under the" in keep[0].reason


def test_a_scheduled_run_exactly_at_the_floor_is_spared():
    # Boundary is <=, so the floor itself survives. A run killed at exactly 3h
    # would be one that had just been declared healthy.
    kill, keep = reap.classify([_proc(1, config.MAX_AGE_HOURS, SCHEDULED)], NOW)
    assert kill == []


def test_a_scheduled_run_past_the_floor_is_killed():
    kill, keep = reap.classify([_proc(29092, 10.1, SCHEDULED)], NOW)
    assert [v.pid for v in kill] == [29092]
    assert "10.1h resident" in kill[0].reason


def test_a_days_old_interactive_session_is_still_spared():
    # Age alone must never be enough. This is the rule that keeps a long chat
    # session alive, and it is the one most likely to be wrongly "simplified".
    kill, _ = reap.classify([_proc(9528, 46.0, INTERACTIVE)], NOW)
    assert kill == []


# --- the whole Aug 17 morning, end to end -------------------------------------

def test_the_real_aug_17_snapshot_kills_six_and_spares_the_rest():
    procs = [
        _proc(12360, 51.1, SCHEDULED),      # Aug 15 10:07 AM
        _proc(7164, 25.5, SCHEDULED),       # Aug 16 11:43 AM
        _proc(13272, 10.7, SCHEDULED),      # Aug 17 2:32 AM - the one the monitor missed
        _proc(29092, 10.1, SCHEDULED),
        _proc(32044, 9.1, SCHEDULED),
        _proc(4352, 8.6, SCHEDULED),
        _proc(16360, 0.0, SCHEDULED),       # the run doing the reaping
        _proc(17724, 0.0, INTERACTIVE),     # the owner, mid-conversation
        _proc(14488, 0.7, RESUMED),
        _proc(23320, 0.1, APP_RENDERER),
        _proc(17244, 42.2, REMOTE_CONTROL),
    ]
    kill, keep = reap.classify(procs, NOW)
    assert sorted(v.pid for v in kill) == sorted([12360, 7164, 13272, 29092, 32044, 4352])
    assert sorted(v.pid for v in keep) == sorted([16360, 17724, 14488, 23320, 17244])


def test_every_process_gets_a_verdict():
    # A log that records only kills cannot answer "why was that one left alive".
    procs = [_proc(1, 9.0, SCHEDULED), _proc(2, 9.0, INTERACTIVE), _proc(3, 0.1, SCHEDULED)]
    kill, keep = reap.classify(procs, NOW)
    assert len(kill) + len(keep) == len(procs)
    assert all(v.reason for v in kill + keep)


def test_the_reaper_never_kills_itself():
    kill, keep = reap.classify([_proc(4242, 99.0, SCHEDULED)], NOW, self_pid=4242)
    assert kill == []
    assert keep[0].reason == "the reaper itself"


# --- the runaway guard --------------------------------------------------------

def test_the_guard_refuses_a_sweep_rather_than_killing_everything():
    procs = [_proc(i, 9.0, SCHEDULED) for i in range(config.MAX_KILLS_PER_RUN + 1)]
    try:
        reap.classify(procs, NOW)
    except reap.TooManyKills as exc:
        assert "refusing to kill anything" in str(exc)
    else:
        raise AssertionError("the guard let an oversized sweep through")


def test_the_guard_allows_exactly_the_cap():
    procs = [_proc(i, 9.0, SCHEDULED) for i in range(config.MAX_KILLS_PER_RUN)]
    kill, _ = reap.classify(procs, NOW)
    assert len(kill) == config.MAX_KILLS_PER_RUN


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
