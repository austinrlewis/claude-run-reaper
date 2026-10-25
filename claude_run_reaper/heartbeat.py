"""The dead-man's switch for task-monitor-refresh.

The monitor stamps a file when a run COMPLETES. This module decides, from that
stamp alone, whether the owner needs to be told the monitor has stopped - and it is
deliberately the only place in this program that can send mail.

WHY THIS PROGRAM MAILS AT ALL, WHEN ITS OWN DOCSTRING SAYS IT NEVER DOES.
`run.py` says "it never emails; task-monitor-refresh reads the run records and
reports", and that rule is right for everything else here: two emailers on one
problem is how a daily alert gets ignored. This is the one case the rule cannot
cover, because the problem IS the reporter. Nothing else on the machine runs
unattended, hourly, with a real exit code. So the exception is drawn as narrowly
as it can be: this program mails about the HEARTBEAT and about nothing else.
Kills, spared processes and reap failures still go only into the run record and
still reach the owner through the monitor. If you ever find yourself adding a second
subject line here, that is the rule reasserting itself - put it in the record.

FOUR STATES, AND ONLY TWO OF THEM ARE QUIET.
    ok          the stamp is younger than the threshold
    stale       the stamp is older - the monitor missed at least one whole slot
    missing     no stamp at all - never installed, or deleted
    unreadable  the file is there but is not a stamp we can parse

ONE THREAD PER OUTAGE.
Every message about a single outage - the first alert, each re-alert, and the
RESOLVED that closes it - is a reply on the thread the first alert started, so
the inbox carries one row whose newest message IS the current state. Two
constraints fall out of that and are easy to break. Gmail refuses to thread a
reply whose Subject differs from the thread's, so a follow-up reuses the FIRST
subject with `Re:` and says what changed in the BODY, never in the subject line.
And the alert is the only record the outage happened, so nothing here deletes
it; the resolve clears the thread's unread state and leaves the history.

`missing` and `unreadable` are reported as loudly as `stale` ON PURPOSE. A
dead-man's switch that goes quiet when it cannot see its own input is not a
switch, it is decoration - which is the same failure the monitor's own STEP 2.6
was built to fix ("a check that silently passes when it cannot see").
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

OK = "ok"
STALE = "stale"
MISSING = "missing"
UNREADABLE = "unreadable"


@dataclass
class Reading:
    """What the heartbeat file says right now."""
    kind: str                       # OK / STALE / MISSING / UNREADABLE
    stamped_at: datetime | None     # when the monitor last COMPLETED a run
    age_hours: float | None
    detail: str                     # one plain line, safe to put in an email

    def as_dict(self) -> dict:
        return {"kind": self.kind,
                "stampedAt": self.stamped_at.isoformat() if self.stamped_at else None,
                "ageHours": round(self.age_hours, 2) if self.age_hours is not None else None,
                "detail": self.detail}


@dataclass
class Decision:
    """What to do about that reading."""
    reading: Reading
    send: bool
    subject: str | None
    body: str | None
    state: dict                     # write this back after a successful send
    thread_id: str | None = None    # reply here; None starts a new thread
    resolved: bool = False          # this message closes the outage out


def _parse_stamp(raw: str) -> datetime:
    """An ISO timestamp, naive or offset-aware, as Mountain time.

    A naive stamp is read as Mountain rather than rejected: the monitor is told
    to write an offset, but a stamp we can read approximately is worth more than
    an `unreadable` that pages the owner over a formatting slip.
    """
    stamp = datetime.fromisoformat(raw)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=ZoneInfo(config.TIMEZONE))
    return stamp


def read(path: Path, now: datetime,
         max_age_hours: float = config.HEARTBEAT_MAX_AGE_HOURS) -> Reading:
    """Classify the heartbeat file. Never raises."""
    if not path.exists():
        return Reading(MISSING, None, None,
                       "there is no heartbeat file at all (%s)" % path)
    try:
        # utf-8-sig, not utf-8: the monitor's STEP 4.6 stamp has been written
        # with PowerShell `Set-Content -Encoding utf8`, which on Windows
        # PowerShell 5.1 prepends a UTF-8 BOM. Plain utf-8 chokes on it and the
        # switch flips to UNREADABLE and pages the owner over a formatting slip.
        # utf-8-sig reads a file with OR without the BOM. (2026-08-29)
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        stamped = _parse_stamp(data["completedAt"])
    except Exception as exc:
        return Reading(UNREADABLE, None, None,
                       "the heartbeat file could not be read (%s: %s)"
                       % (type(exc).__name__, exc))

    # A stamp in the future means a clock changed under us. Treat it as fresh
    # rather than as an alert: this switch exists to catch silence, and a wrong
    # clock is the monitor's problem to report, not a reason to page at 3 AM.
    age = (now - stamped).total_seconds() / 3600.0
    if age <= max_age_hours:
        return Reading(OK, stamped, age,
                       "last completed %s (%.1fh ago)"
                       % (stamped.strftime("%a %Y-%m-%d %H:%M"), age))
    return Reading(STALE, stamped, age,
                   "last completed %s, which was %.1fh ago"
                   % (stamped.strftime("%a %Y-%m-%d %H:%M"), age))


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}                   # no state, or unreadable state, means "never alerted"


def _alert_subject(reading: Reading, task: str, max_age_hours: float) -> str:
    return "%s %s has not run in %s" % (
        config.HEARTBEAT_SUBJECT_TAG, task,
        "over %.0fh" % max_age_hours
        if reading.kind == STALE else reading.kind)


def _reply_subject(state: dict, fallback: str) -> str:
    """The first alert's subject, prefixed `Re:`, so Gmail keeps the thread.

    Losing the stored subject is not a failure - the message goes out under
    `fallback` and starts its own thread, which is exactly the old behaviour.
    Going quiet would be the failure.
    """
    stored = state.get("subject")
    return "Re: %s" % stored if stored else fallback


def _resolved_body(reading: Reading, task: str) -> str:
    """The subject line is the old alert's, so the body has to carry the news."""
    if task != config.MONITORED_TASK:
        return "\n".join([
            "RESOLVED - %s is running again." % task,
            "",
            "  %s" % reading.detail,
            "",
            "Nothing else was checked - this only says it completed a pass, not",
            "that what it reported or fixed is healthy. Its next pass covers that.",
        ])
    return "\n".join([
        "RESOLVED - %s is running again." % task,
        "",
        "  %s" % reading.detail,
        "",
        "Nothing else was checked - this only says the reporter is back, not",
        "that what it reports is healthy. Its next run covers that.",
    ])


def _fixer_body(reading: Reading, now: datetime, task: str,
                max_age_hours: float) -> str:
    """The failure-fixer's version: same shape, but what is at stake and what to
    check are about THAT program, not the monitor."""
    return "\n".join([
        "%s has not completed a pass." % task,
        "",
        "  %s" % reading.detail,
        "  threshold: %.0f hours" % max_age_hours,
        "  checked:   %s" % now.strftime("%a %Y-%m-%d %H:%M %Z"),
        "",
        "WHAT THIS MEANS. That program reads the failure notes, tries the safe",
        "fixes and mails the one [Fixer] report. While it is down, new failures",
        "are not triaged and nothing is fixed or reported.",
        "",
        "WHAT TO CHECK.",
        "  1. Is the FailureFixer scheduled job enabled, and did it fire?",
        "  2. Is a pass or a fix run hung? This program kills leaked runs hourly",
        "     - its records are in the claude_run_reaper logs folder under the",
        "     local app data folder.",
        "  3. The fixer's own logs are in the failure_fixer folder there, and a",
        "     crashed pass deliberately does not stamp the heartbeat.",
        "",
        "This thread repeats at most once every %.0f hours while the condition"
        % config.HEARTBEAT_RE_ALERT_HOURS,
        "lasts, and gets one more reply when it clears. Everything about this",
        "outage stays here.",
        "",
        "Sent by claude_run_reaper, which is a Windows scheduled job and does not",
        "depend on the program it is watching.",
    ])


def _body(reading: Reading, now: datetime, task: str, max_age_hours: float) -> str:
    if task != config.MONITORED_TASK:
        return _fixer_body(reading, now, task, max_age_hours)
    return "\n".join([
        "%s has not completed a run." % task,
        "",
        "  %s" % reading.detail,
        "  threshold: %.0f hours" % max_age_hours,
        "  checked:   %s" % now.strftime("%a %Y-%m-%d %H:%M %Z"),
        "",
        "WHAT THIS MEANS. That task is the only thing that tells you an overdue",
        "task, a failed program, schedule drift or a bad output check. While it",
        "is down, none of those reach you and an empty inbox proves nothing.",
        "",
        "WHAT TO CHECK.",
        "  1. Is the routine still enabled, and did it fire? (Routines list)",
        "  2. Is a run hung? This program kills leaked runs hourly - its records",
        "     are in %LOCALAPPDATA%\\claude_run_reaper\\logs\\.",
        "  3. Is the Task Monitor artifact stale? Its SNAPSHOT_AT says when it",
        "     was last refreshed, and that is the same run that stamps this file.",
        "",
        "This thread repeats at most once every %.0f hours while the condition"
        % config.HEARTBEAT_RE_ALERT_HOURS,
        "lasts, and gets one more reply when it clears. Everything about this",
        "outage stays here.",
        "",
        "Sent by claude_run_reaper, which is a Windows scheduled job and does not",
        "depend on the task it is watching.",
    ])


def decide(reading: Reading, state: dict, now: datetime,
           task: str | None = None, max_age_hours: float | None = None) -> Decision:
    """Whether this reading is worth an email, given what was already said.

    Recovery is reported too, and only when an alert actually went out. Silence
    after an alert is ambiguous - it reads the same as the alert having been the
    last thing this program ever managed to do.

    `task` and `max_age_hours` say WHICH watch this is (default: the monitor, so
    every older caller is unchanged). They shape the subject and the body and
    nothing else - the decision logic is identical for every watch.
    """
    task = task or config.MONITORED_TASK
    if max_age_hours is None:
        max_age_hours = config.HEARTBEAT_MAX_AGE_HOURS
    alerted_at = state.get("alertedAt")
    thread_id = state.get("threadId")
    if reading.kind == OK:
        if alerted_at:
            return Decision(
                reading, True,
                _reply_subject(state, "%s RESOLVED - %s is running again"
                               % (config.HEARTBEAT_SUBJECT_TAG,
                                  task)),
                _resolved_body(reading, task),
                {},              # cleared, so a later failure alerts immediately
                thread_id, True)
        return Decision(reading, False, None, None, state)

    if alerted_at:
        try:
            since = (now - _parse_stamp(alerted_at)).total_seconds() / 3600.0
        except Exception:
            since = None         # unparseable state: re-alert rather than go quiet
        if since is not None and since < config.HEARTBEAT_RE_ALERT_HOURS:
            return Decision(reading, False, None, None, state)

    # A re-alert stays on the outage's thread. The kind can have changed under
    # us (stale -> missing); that goes in the body, because changing the subject
    # is the thing that would split the thread.
    subject = _alert_subject(reading, task, max_age_hours)
    new_state = {"alertedAt": now.isoformat(), "kind": reading.kind,
                 "subject": state.get("subject") or subject}
    if thread_id:
        new_state["threadId"] = thread_id
    return Decision(
        reading, True,
        _reply_subject(state, subject) if alerted_at else subject,
        _body(reading, now, task, max_age_hours), new_state,
        thread_id if alerted_at else None)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not state:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps(state, indent=1), encoding="utf-8")


def seconds_until_stale(reading: Reading,
                        max_age_hours: float | None = None) -> float | None:
    """For the run record, so a reading that is merely CLOSE is still visible."""
    if reading.age_hours is None:
        return None
    if max_age_hours is None:
        max_age_hours = config.HEARTBEAT_MAX_AGE_HOURS
    return (max_age_hours - reading.age_hours) * 3600.0
