"""Which processes are leaked scheduled runs, and which are left alone.

Pure decision logic. Nothing here lists a process or kills one - run.py supplies
the candidates and run.py does the killing - so every rule below is testable
without a real claude.exe on the machine, including the rules that must NOT fire.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import config


class TooManyKills(Exception):
    """The match wants to kill more processes than any real backlog contains."""


@dataclass(frozen=True)
class Proc:
    """One claude.exe as Windows reports it."""
    pid: int
    started: datetime          # timezone-aware
    command_line: str
    parent_pid: int = 0        # 0 when unknown; only used for PROTECTED descent
    # Minutes since this run last wrote to its session transcript, or None when
    # that could not be determined. None means NO OPINION, never "idle" - see
    # transcripts.py. Supplied by the caller so this module stays pure.
    idle_minutes: float | None = None
    # True when a person has typed into this run since it launched. A scheduled
    # run the owner replies to keeps the scheduled command line, so this is the only
    # thing separating "leaked" from "he is talking to it right now".
    is_conversation: bool = False
    # The tool this run is still waiting on, or None when it is waiting on none
    # (or could not be read). This is what separates "finished and did not exit"
    # from "frozen part way through" - idle_minutes alone cannot, and asserting
    # the first from the second is the bug this field was added to fix on
    # 2026-08-25. See transcripts.pending_tool.
    blocked_on: str | None = None


@dataclass(frozen=True)
class Verdict:
    pid: int
    age_hours: float
    started: str
    reason: str
    matched: bool = False      # did the command line match the signature?
    idle_minutes: float | None = None
    blocked_on: str | None = None

    def as_dict(self) -> dict:
        # idleMinutes is carried into the run record on purpose: it is what lets
        # task-monitor-refresh tell "killed long after finishing" apart from
        # "killed mid-flight" without re-deriving it from the transcripts. A
        # large value means the work had already landed. null means unknown -
        # the monitor must go and read the transcript itself, never assume.
        #
        # blockedOn is the CORRECTION to that (2026-08-25): a large idleMinutes
        # does NOT mean the work landed if the run was frozen waiting on a tool.
        # When blockedOn names a tool, the run did not finish, whatever the idle
        # reading says, and the monitor must treat the kill as mid-flight.
        return {"pid": self.pid, "ageHours": round(self.age_hours, 2),
                "started": self.started, "reason": self.reason,
                "idleMinutes": (None if self.idle_minutes is None
                                else round(self.idle_minutes, 1)),
                "blockedOn": self.blocked_on}


def _arguments(command_line: str) -> list[str]:
    """The argument tokens of a command line, with the executable path removed.

    The path is dropped on purpose: it is attacker-free but not marker-free, and
    a directory called "rc" would otherwise read as the remote-control alias.
    """
    cl = (command_line or "").strip()
    if cl.startswith('"'):
        end = cl.find('"', 1)
        rest = cl[end + 1:] if end != -1 else ""
    else:
        head, sep, tail = cl.partition(" ")
        rest = tail if sep else ""
    return rest.split()


def is_remote_control(command_line: str) -> bool:
    """True for a Remote Control listener, in either spelling the CLI accepts.

    `--remote-control` (optionally `--remote-control <name>`) and the bare `rc`
    alias. Both are matched as whole argument tokens: `--remote-control-session-
    name-prefix` only configures naming and does not start a listener, and a
    substring rule for "rc" would match most paths on the machine.
    """
    for arg in _arguments(command_line):
        if arg in config.PROTECTED_ALIASES:
            return True
        if arg.split("=", 1)[0] in config.PROTECTED:
            return True
    return False


def is_scheduled_run(command_line: str) -> bool:
    """True only for a process launched as a Claude scheduled run.

    Deliberately conservative in both directions: an unrecognised command line
    is NOT a scheduled run, and anything carrying a FORBIDDEN marker is spared
    even if it otherwise matches.
    """
    cl = command_line or ""
    if is_remote_control(cl):
        return False
    if any(bad in cl for bad in config.FORBIDDEN):
        return False
    return all(req in cl for req in config.REQUIRED)


def age_hours(proc: Proc, now: datetime) -> float:
    return (now - proc.started).total_seconds() / 3600.0


def protected_pids(procs: list[Proc]) -> set[int]:
    """Every PROTECTED process, plus everything descended from one.

    The owner's `claude remote-control` listener is meant to run for weeks while he
    is away, and anything it spawns is him working, not a scheduled run. Descent
    is walked rather than just matching the listener itself, because a session
    driven from his phone is a CHILD of the listener and carries its own command
    line - which could legitimately disable AskUserQuestion, since there is no
    local UI to answer a prompt with.
    """
    roots = {p.pid for p in procs if is_remote_control(p.command_line)}
    if not roots:
        return roots

    by_parent: dict[int, list[Proc]] = {}
    for proc in procs:
        by_parent.setdefault(proc.parent_pid, []).append(proc)

    protected, frontier = set(roots), list(roots)
    while frontier:
        for child in by_parent.get(frontier.pop(), ()):
            if child.pid not in protected:
                protected.add(child.pid)
                frontier.append(child.pid)
    return protected


def _judge(proc: Proc, age: float, limit: float) -> tuple[bool, str]:
    """Kill-or-keep for a process already known to be an unprotected scheduled run.

    Two independent grounds for a kill, and the order matters:

      1. IDLE. Its transcript has been quiet past config.IDLE_MINUTES, so the run
         finished and the process simply is not exiting. Age is irrelevant here -
         this is what reaps a 37-second run that has been hanging for 25 minutes
         instead of leaving it until the five-hour floor.

      2. OLD. Past the age floor, exactly as before this rule existed.

    The one softening: past the floor but demonstrably STILL WRITING earns the
    benefit of the doubt up to HARD_MAX_AGE_HOURS. That is what stops a genuine
    `memory-health-audit` overrun being killed mid-flight. It is a reprieve, not
    an exemption - past the hard ceiling a writer dies too, because a process
    stuck in a loop also writes, and never killing one is worse than the leak.
    Since 2026-09-03 a CONVERSATION earns that same reprieve on the same terms;
    before then it could only ever be spared for being under the floor.

    An UNKNOWN idle time (None) never kills on ground 1 and never grants the
    reprieve. It falls through to exactly the pre-2026-08-18 behaviour.
    """
    idle = proc.idle_minutes

    # A run the owner is talking to is never reaped for going quiet - a pause is him
    # reading, not a leak. It still faces the age floor, exactly as it did before
    # the idle rule existed, so this is a carve-out from the NEW rule only and
    # never keeps a process alive longer than the old behaviour would have.
    if proc.is_conversation:
        if age <= limit:
            return False, ("scheduled run turned conversation, %.1fh old - "
                           "under the %.1fh floor" % (age, limit))
        # Past the floor, but STILL WRITING earns a conversation the same
        # reprieve a plain long run gets.
        #
        # WHY THIS WAS ADDED (2026-09-03). Until now a conversation was given no
        # idle reading at all, deliberately - which made the reprieve at the
        # bottom of this function unreachable for it, because that branch needs a
        # non-None idle. So the ONE category where a person is provably present
        # was the only category that could never be spared for being actively
        # alive, only for being young. Ten of the reaper's first forty kills were
        # exactly that shape: runs the owner had opened to thaw a freeze, killed
        # 5.4-5.9h later on the age floor while he was working in them. That is
        # backwards, and it punished the correct response to a frozen run.
        #
        # Still a reprieve, not an exemption, and on the same two terms as the
        # plain one: quiet past IDLE_MINUTES forfeits it (that is a conversation
        # he has walked away from, or a freeze he never unstuck), and the hard
        # ceiling kills a writer regardless.
        if (idle is not None and idle <= config.IDLE_MINUTES
                and age < config.HARD_MAX_AGE_HOURS):
            return False, ("conversation, %.1fh old but still writing %.0f min "
                           "ago - spared as a long run, hard ceiling %.1fh"
                           % (age, idle, config.HARD_MAX_AGE_HOURS))
        return True, ("scheduled run turned conversation, %.1fh resident - "
                      "over the %.1fh floor" % (age, limit))

    if idle is not None and idle > config.IDLE_MINUTES:
        # Quiet means STOPPED, not FINISHED. Which one it is turns entirely on
        # whether a tool call is still outstanding, and saying "finished" when a
        # run was frozen mid-call is how two real failures got filed as
        # non-events on 2026-08-25. Kill it either way - a frozen run is not
        # coming back on its own - but never label it finished without the
        # evidence. See transcripts.pending_tool.
        if proc.blocked_on:
            return True, ("STALLED mid tool call - waiting on %s, quiet for "
                          "%.0f min, %.1fh resident. Did NOT finish."
                          % (proc.blocked_on, idle, age))
        return True, ("finished scheduled run that did not exit - quiet for "
                      "%.0f min, %.1fh resident" % (idle, age))

    if age <= limit:
        return False, ("scheduled run, %.1fh old - under the %.1fh floor"
                       % (age, limit))

    if idle is not None and age < config.HARD_MAX_AGE_HOURS:
        return False, ("scheduled run, %.1fh old but still writing %.0f min ago "
                       "- spared as a long run, hard ceiling %.1fh"
                       % (age, idle, config.HARD_MAX_AGE_HOURS))

    return True, "leaked scheduled run, %.1fh resident" % age


def classify(procs: list[Proc], now: datetime, self_pid: int | None = None,
             max_age_hours: float | None = None) -> tuple[list[Verdict], list[Verdict]]:
    """Split candidates into (kill, keep).

    EVERY process gets a verdict, spared ones included. A log that records only
    kills cannot answer "why was that one left alive", which is the question
    asked the morning after a leak is missed. `matched` marks the ones whose
    command line fits the signature, so a caller can log the near-misses without
    logging thirty unrelated desktop-app processes every hour.
    """
    limit = config.MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    protected = protected_pids(procs)
    kill: list[Verdict] = []
    keep: list[Verdict] = []

    for proc in sorted(procs, key=lambda p: p.started):
        age = age_hours(proc, now)
        stamp = proc.started.isoformat()
        matched = is_scheduled_run(proc.command_line)
        if self_pid is not None and proc.pid == self_pid:
            keep.append(Verdict(proc.pid, age, stamp, "the reaper itself", matched))
        elif proc.pid in protected:
            # Checked BEFORE the signature: protection must hold even for a
            # process that looks exactly like a scheduled run.
            keep.append(Verdict(proc.pid, age, stamp,
                                "remote control, or descended from it", matched))
        elif not matched:
            keep.append(Verdict(proc.pid, age, stamp,
                                "not a scheduled run (interactive, resumed, or app process)",
                                False))
        else:
            kill_it, reason = _judge(proc, age, limit)
            (kill if kill_it else keep).append(
                Verdict(proc.pid, age, stamp, reason, True, proc.idle_minutes,
                        proc.blocked_on))

    if len(kill) > config.MAX_KILLS_PER_RUN:
        raise TooManyKills(
            "%d processes matched, over the %d cap - refusing to kill anything"
            % (len(kill), config.MAX_KILLS_PER_RUN))
    return kill, keep
