"""Orchestration and CLI.

    python -m claude_run_reaper.run              reap, write a run record
    python -m claude_run_reaper.run --dry-run    report what it would kill, kill nothing
    python -m claude_run_reaper.run --list       just print every claude.exe and its verdict

Silent by design about its OWN work: it never emails about a kill. The run
records carry that, and task-monitor-refresh reads them and reports, so this
program stays the thing that ACTS and the monitor stays the thing that TELLS.
Two emailers on one problem is how a daily alert gets ignored.

THE ONE EXCEPTION IS THE MONITOR ITSELF (added 2026-08-17). Every health signal
in the system reaches the owner through that one daily task, and it has no exit code
- a hung run still stamps lastRunAt and still looks green, so a dead pipeline and
a healthy week are the same empty inbox. This program is the only other thing that
runs unattended and hourly, so it carries the dead-man's switch: `heartbeat.py`
mails the owner when the monitor stops completing runs, and about nothing else. That
module's docstring holds the reasoning; keep the exception that narrow.

Exit codes, matching the convention in Automation\\tools\\output_check.py:
    0  ran fine (killed nothing, or killed cleanly)
    1  a kill was attempted and failed
    2  the check itself is broken - could not enumerate, or the runaway guard tripped
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, heartbeat, reap, transcripts

_LINES: list[str] = []

# Second resolution, with the offset, so datetime.fromisoformat takes it as-is.
# The default 'o' round-trip format carries 7 fractional digits, which is one
# more than Python's parser accepts.
_PS_SNAPSHOT = (
    "Get-CimInstance Win32_Process -Filter \"Name='%s'\" | "
    "Select-Object ProcessId,ParentProcessId,"
    "@{n='Start';e={$_.CreationDate.ToString('yyyy-MM-ddTHH:mm:sszzz')}},"
    "CommandLine | ConvertTo-Json -Depth 2"
)


def _now() -> datetime:
    return datetime.now(ZoneInfo(config.TIMEZONE))


def _log(message: str) -> None:
    line = "%s  %s" % (_now().strftime("%H:%M:%S"), message)
    _LINES.append(line)
    print(line, flush=True)


def snapshot() -> list[reap.Proc]:
    """Every claude.exe on the machine, with its command line.

    Shelled out to PowerShell rather than psutil: no third-party dependency, and
    CommandLine is the one field this program cannot work without.
    """
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         _PS_SNAPSHOT % config.PROCESS_NAME],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("could not list processes: %s" % proc.stderr.strip())

    raw = (proc.stdout or "").strip()
    if not raw:
        return []                      # no claude.exe at all is a legitimate state
    # Windows PowerShell 5.1's ConvertTo-Json leaves raw control characters
    # (anything outside \n \r \t) unescaped inside strings, and a claude.exe
    # command line carrying a prompt can contain them. strict=False accepts
    # them; the command line is only ever substring-matched, so a stray
    # control byte inside it changes no verdict. Without this the whole
    # snapshot raises and the reaper exits 2 - which it did silently every
    # hour from 2026-09-02 22:37 until 2026-09-03.
    parsed = json.loads(raw, strict=False)
    if isinstance(parsed, dict):
        parsed = [parsed]              # ConvertTo-Json unwraps a single result

    procs = []
    for row in parsed:
        started = row.get("Start")
        if not started:
            continue                   # a process that exited mid-enumeration
        procs.append(reap.Proc(pid=int(row["ProcessId"]),
                               started=datetime.fromisoformat(started),
                               command_line=row.get("CommandLine") or "",
                               parent_pid=int(row.get("ParentProcessId") or 0)))
    return procs


def kill(pid: int) -> bool:
    """Kill the process AND its children.

    /T matters: a hung run owns its MCP server subprocesses, and terminating the
    parent alone leaves those behind still holding memory.
    """
    proc = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                          capture_output=True, text=True)
    if proc.returncode == 0:
        return True
    # 128 is "no such process" - it exited between the snapshot and now, which
    # is the outcome this program wanted anyway.
    if proc.returncode == 128 or "not found" in (proc.stdout + proc.stderr).lower():
        _log("  PID %d had already exited" % pid)
        return True
    _log("  could not kill PID %d: %s" % (pid, (proc.stderr or proc.stdout).strip()))
    return False


def _write_record(outcome: str, killed: list[reap.Verdict],
                  spared: list[reap.Verdict], beat: dict | None = None) -> None:
    """One timestamped JSON per run - never date-keyed, so an hourly cadence
    cannot have a later run destroy the record of an earlier one."""
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = _now().strftime("%Y-%m-%d_%H%M%S")
        # Only the NEAR-MISSES are named: a scheduled run spared for being young
        # is worth explaining, the thirty desktop-app processes are not. The rest
        # are a count, so the record still proves the whole machine was examined.
        near = [v for v in spared if v.matched]
        (config.LOG_DIR / ("%s.json" % stamp)).write_text(
            json.dumps({"ranAt": _now().isoformat(), "outcome": outcome,
                        "killed": [v.as_dict() for v in killed],
                        "spared": [v.as_dict() for v in near],
                        "unrelatedProcesses": len(spared) - len(near),
                        "heartbeat": beat or {},
                        "output": _LINES}, indent=1), encoding="utf-8")
        _prune()
    except Exception as exc:            # never let logging break the reap
        print("could not write the run record: %s" % exc, flush=True)


def _prune() -> None:
    records = sorted(config.LOG_DIR.glob("*.json"))
    for stale in records[:-config.KEEP_RECORDS]:
        try:
            stale.unlink()
        except OSError:
            pass


def check_heartbeat(now: datetime, send_mail: bool = True) -> dict:
    """The dead-man's switch. Never raises, never changes this program's exit code.

    Deliberately isolated from the reap. Killing leaked runs is this program's
    job and a broken mailer must not stop it - so every failure path here logs
    and returns, and the reap's exit code keeps meaning what it has always meant.
    The cost of that choice is that a mail failure is only visible in the run
    record; it is the right trade, because the alternative is a token expiry
    silently switching off the killer.

    State is written ONLY after a send confirms. A send that fails therefore
    retries on the next hourly run rather than marking itself done.

    TWO WATCHES (2026-10-21): task-monitor-refresh, always, and failure-fixer,
    only while its ARMED marker exists (config.WATCHED). Each is evaluated and
    mailed in its own try, so a broken read, a broken send or a broken state file
    on one never stops the other. The monitor's record is what this returns, flat,
    exactly as before; any other watch's record rides under "watches".

    The mailer is imported HERE, once, on the first send that is actually needed,
    and handed down to _deliver. That keeps test_wiring's guarantee literal: the
    only place in the reap package that reaches for the mailer is this function.
    """
    try:
        watches = config.WATCHED
    except Exception as exc:        # belt and braces: never take the reap down
        _log("HEARTBEAT CHECK BROKE - %s: %s" % (type(exc).__name__, exc))
        return {"kind": "check-failed", "detail": "%s: %s" % (type(exc).__name__, exc)}

    records: dict[str, dict] = {}
    mail = None                     # (gmail service, mailer), built on first send
    for watch in watches:
        label = "heartbeat" if watch.task == config.MONITORED_TASK \
            else "heartbeat (%s)" % watch.task
        try:
            # RULING R4: no marker, no watch. Not read, not mailed, not recorded.
            if watch.armed_marker is not None and not watch.armed_marker.exists():
                continue
            reading = heartbeat.read(watch.file, now, watch.max_age_hours)
            _log("%s %s - %s" % (label, reading.kind, reading.detail))
            record = reading.as_dict()
            record["emailed"] = False
            decision = heartbeat.decide(
                reading, heartbeat.load_state(watch.state), now,
                task=watch.task, max_age_hours=watch.max_age_hours)
        except Exception as exc:    # read() promises not to raise; belt and braces
            _log("%s CHECK BROKE - %s: %s" % (label.upper(), type(exc).__name__, exc))
            records[watch.task] = {"kind": "check-failed",
                                   "detail": "%s: %s" % (type(exc).__name__, exc)}
            continue
        records[watch.task] = record
        if not decision.send:
            continue
        if not send_mail:
            _log("  would email: %s" % decision.subject)
            record["wouldEmail"] = decision.subject
            continue

        try:
            if mail is None:
                from common import google_auth, mailer
                mail = (google_auth.gmail_service(), mailer)
            _deliver(watch, decision, record, mail[0], mail[1])
        except Exception as exc:
            record["mailError"] = "%s: %s" % (type(exc).__name__, exc)
            _log("  COULD NOT EMAIL - %s: %s" % (type(exc).__name__, exc))

    primary = records.pop(config.MONITORED_TASK, None)
    if primary is None:
        primary = {"kind": "check-failed", "detail": "the monitor watch is not configured"}
    if records:
        primary["watches"] = records
    return primary


def _deliver(watch, decision, record: dict, service, mailer) -> None:
    """Send one watch's alert (or RESOLVED) and record it. Raises on failure.

    Takes the mailer as an argument rather than importing it - see the note on
    check_heartbeat. State is written to THIS watch's own file, and only after the
    send confirms.
    """
    # Every message about one outage rides the first alert's thread, so the
    # inbox shows one row at its current state rather than an alert and a
    # RESOLVED sitting side by side. Passing threadId alone is not enough -
    # without In-Reply-To the message is an orphan outside Gmail.
    thread, in_reply_to = decision.thread_id, ""
    if thread:
        try:
            _, in_reply_to = mailer.latest_message(service, thread)
        except Exception as exc:
            # The thread is gone. A deleted alert must not silence the
            # switch, so this falls back to a fresh, unthreaded send.
            _log("  thread %s unreadable, sending unthreaded - %s"
                 % (thread, exc))
            thread = None
    if decision.resolved and thread:
        # BEFORE the send: the old alert goes read, and the RESOLVED reply
        # still lands unread on top of it.
        mailer.label_thread(service, thread, [], remove=["UNREAD"])

    _msg_id, sent_thread = mailer.send(decision.subject, "", decision.body,
                                       thread_id=thread,
                                       in_reply_to=in_reply_to)
    state = dict(decision.state)
    if state and sent_thread:
        state["threadId"] = sent_thread
    heartbeat.save_state(watch.state, state)
    record["emailed"] = True
    record["threadId"] = sent_thread
    _log("  EMAILED the owner: %s (thread %s)" % (decision.subject, sent_thread))
    if not decision.resolved and not thread and sent_thread:
        record["trashedPrior"] = _trash_prior_outages(service, sent_thread, watch.task)


def _trash_prior_outages(service, keep_thread_id: str,
                         task: str = config.MONITORED_TASK) -> int:
    """Trash every earlier heartbeat thread FOR THIS TASK once a new outage has
    its own.

    Called only after a fresh outage's alert has confirmed sent, so the sweep
    can never leave the inbox without a live alert. Best effort: a failure here
    is logged and costs nothing but a leftover thread.

    Scoped to one watched task (2026-10-21). The phrase "Monitor Heartbeat" alone
    matches EVERY watch's threads, so without the task name a new monitor outage
    would trash the failure-fixer's live outage thread, and the reverse - the one
    message the owner needs, deleted by the other alarm. Every subject for a task
    carries its name (alerts, `Re:` follow-ups and the RESOLVED fallback alike), so
    the narrower query still finds all of that task's older threads.
    """
    query = 'from:owner@example.com subject:"%s" subject:"%s"' % (
        config.HEARTBEAT_SWEEP_PHRASE, task)
    try:
        found = service.users().threads().list(
            userId="me", q=query, maxResults=50).execute().get("threads", [])
    except Exception as exc:
        _log("  prior outages not swept - %s: %s" % (type(exc).__name__, exc))
        return 0
    trashed = 0
    for thread in found:
        if thread["id"] == keep_thread_id:
            continue
        try:
            service.users().threads().modify(
                userId="me", id=thread["id"],
                body={"addLabelIds": ["TRASH"]}).execute()
            trashed += 1
        except Exception as exc:
            _log("  could not trash %s - %s" % (thread["id"], exc))
    if trashed:
        _log("  trashed %d prior outage thread(s)" % trashed)
    return trashed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reap leaked Claude scheduled runs")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be killed, kill nothing, write no record")
    parser.add_argument("--list", action="store_true",
                        help="print every claude.exe and its verdict, then exit")
    parser.add_argument("--max-age-hours", type=float, default=None,
                        help="override the %.1fh floor (testing only)" % config.MAX_AGE_HOURS)
    args = parser.parse_args(argv)

    _LINES.clear()
    killed: list[reap.Verdict] = []
    spared: list[reap.Verdict] = []
    beat: dict = {}
    outcome = "crashed"
    try:
        rc, killed, spared = _run(args)
        outcome = "exit %d" % rc
        return rc
    finally:
        # AFTER the reap, and in its own try, so a heartbeat problem can never
        # cost a kill. --dry-run and --list evaluate it and print what they would
        # send, but send nothing and write no state.
        try:
            beat = check_heartbeat(_now(), send_mail=not (args.dry_run or args.list))
        except Exception as exc:
            beat = {"kind": "check-failed", "detail": str(exc)}
        if not args.dry_run and not args.list:
            _write_record(outcome, killed, spared, beat)


def _run(args) -> tuple[int, list[reap.Verdict], list[reap.Verdict]]:
    try:
        procs = snapshot()
    except Exception as exc:
        _log("SNAPSHOT FAILED - %s: %s" % (type(exc).__name__, exc))
        return 2, [], []
    _log("%d %s process(es) on the machine" % (len(procs), config.PROCESS_NAME))

    # Fill in how long each scheduled-looking run has been quiet. Wrapped
    # because a transcript tree that cannot be read must not stop the reap -
    # classify() falls back to the age floor on a None, so the worst case here
    # is the behaviour this program had before the idle test existed.
    now = _now()
    try:
        procs = transcripts.annotate(procs, now)
    except Exception as exc:
        _log("TRANSCRIPT CHECK UNAVAILABLE (%s: %s) - falling back to age only"
             % (type(exc).__name__, exc))

    try:
        to_kill, to_keep = reap.classify(procs, now,
                                         max_age_hours=args.max_age_hours)
    except reap.TooManyKills as exc:
        _log("GUARD TRIPPED - %s" % exc)
        return 2, [], []

    if args.list:
        for verdict in sorted(to_kill + to_keep, key=lambda v: v.started):
            _log("  PID %-6d %-6s %s" % (verdict.pid,
                                         "KILL" if verdict in to_kill else "keep",
                                         verdict.reason))
        return 0, [], to_keep

    if not to_kill:
        _log("no leaked runs - nothing to do")
        return 0, [], to_keep

    _log("%d leaked run(s) to kill" % len(to_kill))
    for verdict in to_kill:
        _log("  PID %d - %s" % (verdict.pid, verdict.reason))

    if args.dry_run:
        _log("dry run - killed nothing, wrote no record")
        return 0, [], to_keep

    failed = [v for v in to_kill if not kill(v.pid)]
    done = [v for v in to_kill if v not in failed]
    _log("killed %d, failed on %d" % (len(done), len(failed)))
    return (1 if failed else 0), done, to_keep


if __name__ == "__main__":
    sys.exit(main())
