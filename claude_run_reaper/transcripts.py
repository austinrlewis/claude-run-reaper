"""Find a scheduled run's session transcript, and say how long it has been quiet.

WHY THIS EXISTS
A process age says how long a run has been ALIVE. It says nothing about whether
the run is still doing anything, and on Aug 18, 2026 that turned out to be the
whole question: every reaped run had finished its work in minutes and then hung
waiting for a stdin close that never came. See the long note in config.py.

A session transcript answers directly. Claude Code appends to
`~\\.claude\\projects\\<project>\\<sessionId>.jsonl` as a run works, so the file's
mtime is the last moment that run did anything at all. Quiet for a while means
finished; still moving means still working.

WHY MATCHING ON CREATION TIME IS SAFE
The transcript is created by the same launch that starts the process, within a
second. Matching on it needs no session id, no parsing, and no cooperation from
the run itself. It is NOT the timestamp correlation that this program refuses to
use for DETECTION - that one matched a process against a task's mutable
`lastRunAt`, which moves when a task runs again and made the earlier leak
invisible. A transcript's CreationTime is immutable and belongs to exactly one
process, so there is no second run to be confused with.

EVERY FAILURE RETURNS None, WHICH MEANS "NO OPINION".
Unknown must never read as "idle", or a missing transcript would license a kill.
A None sends the caller back to the age floor, which is the behaviour this
program had before this module existed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from . import config


def find(started: datetime, root: Path | None = None,
         match_seconds: float | None = None) -> Path | None:
    """The transcript belonging to a process that started at `started`.

    None when nothing matches closely enough, when the root does not exist, or
    when more than one file matches - an ambiguous match is not a match, and
    guessing between two would put the wrong run's activity behind a kill.
    """
    root = config.TRANSCRIPT_ROOT if root is None else root
    window = (config.TRANSCRIPT_MATCH_SECONDS if match_seconds is None
              else match_seconds)
    try:
        if not root.is_dir():
            return None
        candidates = []
        for path in root.rglob("*.jsonl"):
            try:
                created = datetime.fromtimestamp(path.stat().st_ctime,
                                                 tz=started.tzinfo)
            except OSError:
                continue
            if abs((created - started).total_seconds()) <= window:
                candidates.append(path)
    except OSError:
        return None
    return candidates[0] if len(candidates) == 1 else None


def idle_minutes(started: datetime, now: datetime, root: Path | None = None,
                 match_seconds: float | None = None) -> float | None:
    """Minutes since this run last wrote anything, or None if unknowable.

    Negative drift (a file mtime slightly ahead of `now`) is clamped to 0.0
    rather than returned, so a clock skew can never read as "long idle".
    """
    path = find(started, root=root, match_seconds=match_seconds)
    if path is None:
        return None
    try:
        touched = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
    except OSError:
        return None
    return max(0.0, (now - touched).total_seconds() / 60.0)


def human_turns(path: Path, cap: int = 400) -> int:
    """How many times a PERSON typed something into this run.

    WHY THIS IS NEEDED (found 2026-08-18, before the idle rule was ever armed)
    A scheduled run and a conversation the owner is having are the SAME PROCESS when
    he replies to a run instead of starting a fresh session - the command line
    still carries `--disallowedTools AskUserQuestion`, so the signature cannot
    tell them apart. Without this check, a 20-minute pause while he reads or
    steps away would reap the session he is talking to, mid-conversation. The
    old age-only rule never did that, so shipping the idle rule without this
    would have been a straight regression.

    A scheduled run has exactly ONE human turn: the task prompt that launched it.
    More than one means a person is in the loop.

    Counts only genuinely typed input. `type: "user"` also covers every
    tool_result the run feeds itself, which on a busy run is hundreds of entries
    and would make every run look like a conversation.

    Returns 0 when the file cannot be read - callers treat that as "no evidence
    of a person", which is the safe direction here: it falls through to the
    normal reaping rules rather than sparing an unknown process forever.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0

    turns = 0
    for line in text.splitlines():
        if '"user"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") != "user":
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            turns += 1
        elif isinstance(content, list):
            # A typed message carries text blocks; a fed-back tool result carries
            # tool_result blocks. Mixed is not a thing, so any tool_result wins.
            kinds = {block.get("type") for block in content
                     if isinstance(block, dict)}
            if "tool_result" not in kinds and kinds & {"text", "image"}:
                turns += 1
        if turns >= cap:
            break
    return turns


def is_conversation(started: datetime, root: Path | None = None,
                    match_seconds: float | None = None) -> bool:
    """True when a person has typed into this run since it was launched."""
    path = find(started, root=root, match_seconds=match_seconds)
    return path is not None and human_turns(path) > 1


def pending_tool(path: Path) -> str | None:
    """The tool this run is still waiting on, or None if it is waiting on none.

    WHY THIS EXISTS (found 2026-08-25, and it corrects a claim this program has
    been making since Aug 18)
    Idle time alone CANNOT tell "finished, and the process will not exit" apart
    from "frozen part way through". Both look identical from a file mtime: the
    transcript simply stopped growing. The reaper has been asserting the first
    one - `finished scheduled run that did not exit` - purely from an idle
    reading, and on 2026-08-25 it applied that label to two runs that had NOT
    finished: airbnb-tracker-sync 5 minutes into its work and task-monitor-refresh
    3 minutes into its own. Both were frozen mid tool call.

    That mislabel is not cosmetic. task-monitor-refresh stays SILENT on any kill
    the reaper calls "finished", on the reasoning that the work already landed
    and there is nothing for the owner to do. A frozen run wearing that label is a
    real failure filed as a non-event.

    The transcript settles it, and the fingerprint is exact rather than
    heuristic. Claude Code writes a `tool_use` block when it calls a tool and a
    matching `tool_result` when the call returns. A run that finished has an
    answer for every call it made. A run frozen part way through has one call
    with no answer, and the tool named in it IS what it is stuck on.

    Returns the tool's name when exactly that is true, else None. None on an
    unreadable or unparseable file, same as every other function here: unknown
    is NO OPINION, and it must never read as "stalled" any more than it reads as
    "idle" - see the module docstring.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    called: dict[str, str] = {}      # tool_use id -> tool name
    answered: set[str] = set()
    for line in text.splitlines():
        if '"tool_use"' not in line and '"tool_result"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                if block.get("id"):
                    called[block["id"]] = block.get("name") or "unknown tool"
            elif block.get("type") == "tool_result":
                if block.get("tool_use_id"):
                    answered.add(block["tool_use_id"])

    # Last one wins: a run issuing several calls in one block has them answered
    # in any order, and the one still outstanding at the end is the block.
    for call_id, name in reversed(list(called.items())):
        if call_id not in answered:
            return name
    return None


def annotate(procs, now: datetime, root: Path | None = None):
    """Return `procs` with idle_minutes filled in where it could be determined.

    Only scheduled-looking runs are looked up. The desktop app keeps a permanent
    cluster of processes that will never have a matching transcript, and walking
    the transcript tree once per process is wasted work that would also grow the
    chance of a spurious match.
    """
    from . import reap

    out = []
    for proc in procs:
        if not reap.is_scheduled_run(proc.command_line):
            out.append(proc)
            continue
        path = find(proc.started, root=root)
        idle = None
        conversation = False
        blocked = None
        if path is not None:
            conversation = human_turns(path) > 1
            # Every scheduled-looking run gets an idle reading, conversations
            # included. This used to skip conversations on purpose - "no reading
            # is better than a reading the rules must remember to ignore" - but
            # withholding it made reap._judge's still-writing reprieve
            # unreachable for a conversation, so a run the owner had opened to thaw
            # died on the age floor while he was working in it. Ten of the first
            # forty kills were that. reap._judge handles the conversation case
            # first and explicitly now, so the reading is safe to supply.
            # Changed 2026-09-03.
            try:
                touched = datetime.fromtimestamp(path.stat().st_mtime,
                                                 tz=now.tzinfo)
                idle = max(0.0, (now - touched).total_seconds() / 60.0)
            except OSError:
                idle = None
            # Read regardless of the idle value: _judge only consults it on
            # the idle branch, but a spared run's verdict is worth being
            # honest about too.
            blocked = pending_tool(path)
        out.append(reap.Proc(pid=proc.pid, started=proc.started,
                             command_line=proc.command_line,
                             parent_pid=proc.parent_pid,
                             idle_minutes=idle,
                             is_conversation=conversation,
                             blocked_on=blocked))
    return out


__all__ = ["find", "idle_minutes", "human_turns", "is_conversation",
           "pending_tool", "annotate", "timedelta"]
