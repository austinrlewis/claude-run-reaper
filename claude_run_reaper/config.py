"""Static facts about the leaked-run reaper.

If one of these is wrong the program should fail loudly, not adapt.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

TIMEZONE = "America/Denver"

PROCESS_NAME = "claude.exe"

# WHAT A LEAKED SCHEDULED RUN LOOKS LIKE.
#
# A Claude scheduled run is launched from the claude-code bundle with
# AskUserQuestion disabled. That flag is not decoration - it literally means
# "no user is present to answer", which is the definition of a scheduled run.
# An interactive chat session never carries it (it carries --effort/--model
# instead), and a resumed session carries --resume.
#
# Matching on the command line rather than on timestamps is the whole point.
# The Task Monitor used to correlate a process StartTime against each task's
# CURRENT lastRunAt, so when a task ran twice the earlier leak became invisible
# - that is how PID 13272 (task-monitor-refresh's own 2:32 AM run) sat resident
# unnoticed on Aug 17, 2026 while the monitor reported three leaks and not four.
REQUIRED = ("\\claude-code\\", "--disallowedTools AskUserQuestion")
FORBIDDEN = ("--resume=", "--remote-control")

# REMOTE CONTROL IS PROTECTED, AND SO IS EVERYTHING UNDER IT.
#
# The owner drives the PC from a phone with `claude remote-control` while away.
# That listener is SUPPOSED to run for days or weeks - a long uptime is the
# feature, not a leak, and a kill lands precisely when nobody is at the machine
# to notice.
#
# Two layers, because one was too thin. The listener runs from the npm install
# at ...\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe, which DOES
# contain the "\claude-code\" required marker - it was escaping the match only
# by not carrying --disallowedTools AskUserQuestion. A phone-driven session has
# no local UI to answer a prompt, so it could plausibly carry that flag and then
# look exactly like a scheduled run. So: the listener's own flag is forbidden
# outright above, AND every process descended from it is spared below.
PROTECTED = ("--remote-control",)

# ...AND THE ALIAS, which is what the owner actually types (added 2026-09-08).
#
# `claude rc` is the CLI's short form for `claude --remote-control`. It does NOT
# expand on the command line: his live listener read back as
#   ...\@anthropic-ai\claude-codein\claude.exe" rc
# so every substring check for the long flag found nothing, protected_pids()
# collected no roots, and the descendant sparing above - the whole vacation case
# - silently protected nothing. The listener itself still survived, by lacking
# --disallowedTools AskUserQuestion; its phone-driven CHILDREN, which carry that
# flag precisely because a phone has no UI to answer a permission prompt with,
# did not.
#
# Matched as a bare ARGUMENT TOKEN, never a substring - see reap.is_remote_control.
# "rc" occurs inside ordinary paths and flag values, and a substring rule here
# would spare half the machine instead of one listener.
PROTECTED_ALIASES = ("rc",)

# A real scheduled run finishes in minutes. This is the only thing standing
# between this program and a legitimately long run killed mid-flight.
#
# Raised 3 -> 5 on 2026-08-17. Three hours was borrowed from the Task Monitor's
# old reporting threshold, where a wrong guess only cost a spurious line in an
# email; here it costs a terminated run. `memory-health-audit` is the longest
# task the owner has and a genuine overrun would have been killed silently.
# Five hours costs nothing in coverage: every real leak found that day had been
# resident between 8 and 51 hours, so none of them would have escaped. Do not
# lower it casually - the cheap direction is up.
MAX_AGE_HOURS = 5.0

# ---------------------------------------------------------------------------
# THE IDLE-TRANSCRIPT TEST (added 2026-08-18)
#
# Age alone is a poor proxy for "leaked", in BOTH directions, and the evidence
# for that was collected on Aug 18, 2026 by reading the session transcripts of
# every run reaped in the preceding day:
#
#   Airbnb Pricing Review   worked 37 seconds, then sat idle 5.9h  -> reaped
#   Gmail Spam Deletion     worked 5.8 minutes, then sat idle 5.8h -> reaped
#   Airbnb Tracker Sync     worked 5.4 minutes, then sat idle 3.7h -> still up
#   Work search log update  worked 6.0 minutes, then EXITED normally
#
# Not one of them was slow. Every one finished its work in under six minutes and
# wrote its final summary; the PROCESS then failed to exit. The cause is not in
# these programs and cannot be fixed from a task prompt: scheduled runs are
# launched by the Claude DESKTOP APP as its own children with
# `--input-format stream-json`, a mode that by design keeps the process alive
# waiting for more input on stdin after a turn ends. Exiting is the launcher's
# job, and it does it only sometimes.
#
# So the age floor was making both mistakes at once: leaving a finished run
# burning a core for five hours, AND standing ready to kill `memory-health-audit`
# mid-flight if it ever genuinely overran. A transcript settles what age cannot -
# a working run writes to its session .jsonl continuously, a finished-and-hung
# one stops dead.
#
# The rule is ADDITIVE and deliberately so. It never spares anything the age
# floor already killed; it only kills the finished ones sooner. See reap.classify.
IDLE_MINUTES = 20.0

# Above MAX_AGE_HOURS a run that is STILL WRITING gets the benefit of the doubt
# up to this ceiling. That is the half of the change that protects a legitimately
# long run - `memory-health-audit` is the one that could plausibly need it.
# Past this, a writer is killed anyway: a process stuck in a loop still writes,
# and letting one run forever is the failure this whole program exists to stop.
HARD_MAX_AGE_HOURS = 12.0

# Session transcripts. A run's transcript is the file whose CreationTime matches
# the process start - see transcripts.py for why that join is safe.
#
# NOT under %LOCALAPPDATA%: this program runs OUTSIDE the desktop app's MSIX
# package, and these files are written by a process INSIDE it, so the real path
# is the plain user profile one. Do not "correct" it to an AppData\Local path.
TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

# How close a transcript's CreationTime must sit to the process start to be
# treated as that run's. Both are stamped by the same launch; two seconds is
# generous. Widening this is how you mis-attribute one run's transcript to
# another and spare a leak because a DIFFERENT run is still writing.
TRANSCRIPT_MATCH_SECONDS = 5.0

# A runaway guard, not a policy. If one run ever wants to kill more than this,
# the MATCH is wrong, not the machine - so it kills nothing and exits 2 to be
# looked at. Sweeping twenty-plus processes on a bad pattern is the one failure
# here that is worse than the leak it fixes.
MAX_KILLS_PER_RUN = 20

# Outside Documents\, which is a Drive mirror - run logs must not sync.
_LOCAL_STATE = Path.home() / "AppData" / "Local" / "claude_run_reaper"
LOG_DIR = _LOCAL_STATE / "logs"

# Keep a bounded history. One record per hourly run is ~9k files a year, and
# nothing reads a record older than a couple of weeks.
KEEP_RECORDS = 400


# ---------------------------------------------------------------------------
# THE DEAD-MAN'S SWITCH (added 2026-08-17)
#
# Every health signal in this system - overdue tasks, failed programs, schedule
# drift, output-check findings, and this program's own kill records - reaches
# The owner through ONE daily Claude task, task-monitor-refresh. That task has no
# exit code, and a run that hangs still stamps lastRunAt and still classifies
# itself green. So when the monitor is the thing that breaks, a healthy week and
# a dead pipeline look identical from the inbox.
#
# This program is the only other thing that runs unattended on a schedule, it
# runs hourly, and it is a Windows job with a real exit code. So it carries the
# switch: the monitor stamps a file when a run COMPLETES, and this program mails
# The owner if that stamp goes stale.
#
# WHY THE FILE LIVES UNDER ~\.automation\ AND NOWHERE ELSE.
# The monitor runs inside the Claude desktop app, which is an MSIX package, so
# anything it writes under %LOCALAPPDATA% is redirected into the package's
# private VFS - visible from inside Claude, invisible to this program. That is
# the exact failure that broke three programs for two days in August. ~\.automation\
# sits outside the package VFS and outside the Drive mirror. Do not "consolidate"
# this next to the run records in %LOCALAPPDATA%\claude_run_reaper\ - those are
# written by THIS program, from outside the app, which is why they are fine there.
MONITORED_TASK = "task-monitor-refresh"
HEARTBEAT_FILE = Path.home() / ".automation" / "heartbeat" / "task-monitor-refresh.json"

# The monitor runs daily at 8:30 AM MT. 26 hours is one missed slot plus 90
# minutes of grace, so a run that merely starts late never trips this.
HEARTBEAT_MAX_AGE_HOURS = 26.0

# This program runs HOURLY. Without this, one stale heartbeat is 24 emails a day
# and the owner stops reading them by lunchtime - the exact failure Task Standards
# names ("a task that can email daily will end up emailing daily").
HEARTBEAT_RE_ALERT_HOURS = 24.0

# Deliberately NOT "[Routine/Program Monitor]". The monitor's STEP 4 trashes every
# prior email whose subject carries that string, so sharing the tag would have the
# monitor delete the alert that says the monitor is dead.
HEARTBEAT_SUBJECT_TAG = "[Monitor Heartbeat]"

# Older outages are trashed when a NEW one opens (added 2026-09-25 - a resolved
# 9/6 outage was still sitting in the Claude label three weeks later). Only on a
# fresh outage, never on a re-alert or a RESOLVED, so the live outage and its
# all-clear always survive until the next outage replaces them. Trash, never a
# permanent delete. Gmail matches the phrase as the adjacent pair "monitor
# heartbeat", which no other subject in the estate carries -
# tools/subject_collision.py holds that true.
HEARTBEAT_SWEEP_PHRASE = "Monitor Heartbeat"

# Written by THIS program, so %LOCALAPPDATA% is correct - see the note above.
HEARTBEAT_STATE = _LOCAL_STATE / "heartbeat_alert.json"


# ---------------------------------------------------------------------------
# THE SECOND WATCH: failure-fixer (added 2026-10-21)
#
# The dead-man's switch above was built for ONE reporter. The failure_fixer
# program is a second thing that must keep running unattended, and it is the only
# thing that tells the owner about a failed program, so it is watched the same way:
# it stamps ~\.automation\heartbeat\failure-fixer.json when a pass COMPLETES, and
# this program mails if that stamp goes stale. Same MSIX reasoning as above for
# why the file sits under ~\.automation\ and not under the local app data folder.
FIXER_TASK = "failure-fixer"
FIXER_HEARTBEAT_FILE = Path.home() / ".automation" / "heartbeat" / "failure-fixer.json"

# The fixer runs hourly-ish; 3 hours is two missed passes plus grace.
FIXER_HEARTBEAT_MAX_AGE_HOURS = 3.0

# Written by THIS program, so the local app data folder is correct (see above).
FIXER_HEARTBEAT_STATE = _LOCAL_STATE / "heartbeat_alert_fixer.json"

# RULING R4. The fixer's heartbeat does not exist until the owner arms the program,
# and a missing heartbeat is an ALERT everywhere else in this module. So the fixer
# watch is active only while this marker exists - failure_fixer\register_task.ps1
# creates it when it registers the job and deletes it on -Remove. No marker means
# the watch is skipped entirely: not read, not mailed, not logged as a problem.
FIXER_ARMED_MARKER = (Path.home() / "Documents" / "Automation"
                      / "failure_fixer" / "ARMED")


class Watch(NamedTuple):
    """One heartbeat this program is responsible for.

    A NamedTuple, not a dataclass, on purpose: other programs load this file by
    path (task_monitor's wiring test, for one) without registering the module in
    sys.modules, and @dataclass looks the module up there and raises.
    """
    task: str                       # the name in the subject line and the alert body
    file: Path                      # the stamp the watched program writes
    max_age_hours: float            # older than this is STALE
    state: Path                     # this watch's own alert state (never shared)
    armed_marker: Path | None = None    # when set, watch only while this file exists


def watched() -> tuple:
    """Every watch, built from the constants above at CALL time.

    A function rather than a tuple frozen at import so that the older tests - and
    anything else that rebinds HEARTBEAT_FILE / HEARTBEAT_STATE - keep steering the
    monitor watch exactly as they did when it was the only one. `WATCHED` below is
    this, evaluated on access.
    """
    return (
        Watch(MONITORED_TASK, HEARTBEAT_FILE, HEARTBEAT_MAX_AGE_HOURS, HEARTBEAT_STATE),
        Watch(FIXER_TASK, FIXER_HEARTBEAT_FILE, FIXER_HEARTBEAT_MAX_AGE_HOURS,
              FIXER_HEARTBEAT_STATE, FIXER_ARMED_MARKER),
    )


def __getattr__(name: str):
    # Module-level attribute hook: `config.WATCHED` is always the live `watched()`.
    # A test or caller that assigns config.WATCHED directly shadows this normally.
    if name == "WATCHED":
        return watched()
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
