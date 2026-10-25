# claude_run_reaper

Kills Claude scheduled runs that launched and never exited.

## The problem

A Claude scheduled task exposes no exit code, so `lastRunAt` proves it LAUNCHED,
not that it finished. A run that hangs does not die - it sits resident burning a
few percent of a core forever. Six leaked between Aug 15 and Aug 17, 2026, and
once enough had piled up, NEW runs stopped starting: `program-credential-fix-check`
(7:45 AM) and `door-code-reminder` (8:03 AM) both silently failed to fire on
Aug 16 and only ran at 11:43 AM once the backlog cleared. The six had burned
about 2.5 CPU-hours between them by the time they were found.

The Task Monitor could SEE them but could not kill them: terminating a process is
blocked by Claude's auto-mode permission classifier, and that gate is correct.
A Windows Task Scheduler job runs outside it. That is the entire reason this is a
program and not a task.

## How a leaked run is recognised

By its command line, not by its start time.

| Marker | Meaning |
|---|---|
| `\claude-code\` (required) | launched from the claude-code bundle, not the desktop app's own processes |
| `--disallowedTools AskUserQuestion` (required) | "no user is present to answer" - the definition of a scheduled run |
| `--resume=` (forbidden) | a resumed session, always spared |
| `--remote-control` (forbidden, and protected) | the phone-driven listener - see below |

An interactive chat session carries `--effort`/`--model claude-opus-5` and never
carries the `disallowedTools` marker. The desktop app's own processes run from
`WindowsApps\...\app\claude.exe` with a `--type=` flag. The remote-control
listener runs from the npm install and is expected to live for days.

Matching is on the command line only. The age floor is a separate, later test -
see Safety below.

The earlier approach, in the Task Monitor's STEP 1c, correlated a process
StartTime against each task's CURRENT `lastRunAt`. That has a blind spot: when a
task runs twice, the earlier leak becomes invisible. It is how PID 13272
(`task-monitor-refresh`'s own 2:32 AM run) sat resident unnoticed on Aug 17, 2026
while the monitor reported three leaks and there were four. Command-line matching
has no such blind spot and needs no correlation at all.

## Safety

Killing the wrong process is worse than the leak, so three rules stand between
the match and a kill:

- **A 5-hour floor.** A real scheduled run finishes in minutes. Nothing younger
  is touched on age alone. Raised from 3 on Aug 17, 2026 at no cost to coverage:
  every real leak found that day had been resident between 8 and 51 hours. Do
  not lower it casually - the cheap direction is up.
- **An idle-transcript test** (added Aug 18, 2026). Age is a poor proxy for
  "leaked" in both directions. Reading the transcripts of every run reaped in
  the preceding day showed none of them was slow: the pricing review worked
  **37 seconds** then hung 5.9h, the spam sweep worked 5.8 min then hung 5.8h,
  the tracker sync worked 5.4 min then hung. Each had finished its work and
  written its final summary; the *process* failed to exit. So the floor was
  making both mistakes at once - leaving finished runs burning a core for five
  hours, while standing ready to kill a genuine `memory-health-audit` overrun.
  A transcript settles what age cannot: a working run appends to its session
  `.jsonl` continuously, a finished-and-hung one stops dead. Quiet past
  `IDLE_MINUTES` (20) is now a kill at any age, and past the 5-hour floor a run
  that is **still writing** is spared up to `HARD_MAX_AGE_HOURS` (12). The rule
  is additive: it never spares anything the floor already killed.

  The cause is not fixable from here, which is why the reaper is the fix rather
  than a workaround. Scheduled runs are launched by the Claude **desktop app**
  as its own children (they are not Task Scheduler jobs) with
  `--input-format stream-json`, a mode that by design keeps the process alive
  waiting for more input on stdin after a turn ends. Exiting is the launcher's
  job and it does it only sometimes.
- **A conversation is never reaped for going quiet.** When the owner replies to a
  scheduled run instead of opening a new session, it is the *same process* and
  still carries `--disallowedTools AskUserQuestion` - the signature cannot tell
  them apart. Without this, a 20-minute pause while he reads would kill the
  session he is talking to. `transcripts.human_turns()` counts genuinely typed
  input (tool results are `type: "user"` too and are excluded); more than one
  means a person is in the loop, and that run faces the age floor only, exactly
  as it did before the idle rule existed. Caught on Aug 18, 2026 against a live
  example before the rule was ever armed.
- **A conversation that is still writing earns the long-run reprieve too**
  (Sep 3, 2026). Until then a conversation was deliberately given *no* idle
  reading, which made the still-writing reprieve unreachable for it: the one
  category where a person is provably present was the only one that could never
  be spared for being actively alive, only for being young. Ten of the reaper's
  first forty kills were that shape - runs the owner had opened to thaw a freeze,
  killed 5.4-5.9h later while he was working in them. It is still a reprieve on
  the same two terms as the plain one: quiet past `IDLE_MINUTES` forfeits it, and
  `HARD_MAX_AGE_HOURS` kills a writer regardless. **Downstream consequence:** a
  killed conversation now carries `blockedOn` and `idleMinutes`, so
  `task_monitor.reaper.Kill.kind` classifies it as `FROZEN_MIDCALL` (emails) or
  `FINISHED_HUNG` (silent) rather than always `USER_INTERRUPTED` (silent). That
  is the intended reading - a run killed while still waiting on a tool call did
  not finish - but it makes `USER_INTERRUPTED` all but unreachable.
- **Unknown means spare.** An empty or unreadable command line never matches.
- **A runaway cap.** If one run wants to kill more than `MAX_KILLS_PER_RUN` (20),
  the MATCH is wrong, not the machine. It kills nothing and exits 2.
- **Remote control is untouchable, and so is everything under it.** the owner drives
  this PC from his phone with `claude remote-control` while away; that listener
  is *supposed* to run for weeks, and a wrongful kill lands exactly when nobody
  is at the machine. Two layers, because one was too thin: the npm path it runs
  from (`...\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe`) **does**
  contain the `\claude-code\` required marker, so until Aug 17, 2026 it was
  spared only by not carrying `--disallowedTools AskUserQuestion` - and a
  phone-driven session, having no local UI to answer a prompt with, could
  plausibly carry it. Now `--remote-control` is forbidden outright, **and** every
  process descended from a `--remote-control` process is spared before the
  signature is even consulted.

It never emails. `task-monitor-refresh` reads these records and reports; this
program acts. Two emailers on one problem is how a daily alert stops being read.

## Running it

```
.\run_reaper.bat --list                     every claude.exe and its verdict
.\run_reaper.bat --dry-run                  what it would kill, kills nothing
.\run_reaper.bat                            live
```

Exit codes match `Automation\tools\output_check.py`: `0` fine, `1` a kill failed,
`2` the check itself is broken (could not enumerate, or the guard tripped).

## Arming it

```
powershell -ExecutionPolicy Bypass -File .\register_task.ps1
```

Registers `ClaudeRunReaper`, hourly. `-Remove` unregisters it, `-DryRun`
registers a version that never kills.

## Logs

One timestamped JSON per run under `%LOCALAPPDATA%\claude_run_reaper\logs`,
holding every verdict - spared processes included, with the reason. Outside
`Documents\` because that is a Drive mirror. Pruned to the newest 400.


## The dead-man's switch (added 2026-08-17)

This program does one thing that is not reaping, and it is the only mail it
ever sends.

Every health signal in the owner's system - an overdue task, a program that exited
non-zero, schedule drift, a failed output check, and this program's own kill
records - reaches him through one daily Claude task, `task-monitor-refresh`.
That task has no exit code. A run that hangs still stamps `lastRunAt` and still
classifies its own row green. So when the monitor is what breaks, a healthy week
and a dead pipeline are the same empty inbox.

This program is the only other thing that runs unattended on a schedule, it runs
hourly, and it is a Windows job with a real exit code. So it carries the switch:

- The monitor's STEP 4.6 writes `~\.automation\heartbeat\task-monitor-refresh.json`
  as the last thing a run that actually finished does.
- Every hour, `heartbeat.py` reads it (with `utf-8-sig`, since the monitor's
  PowerShell stamp can carry a UTF-8 BOM that plain `utf-8` rejects - 2026-08-29).
  Older than **26 hours** (one missed daily slot plus 90 minutes of grace),
  missing, or genuinely unparseable, and it emails the owner.
- It repeats at most once per **24 hours** while the condition lasts, and sends
  one message when it clears. The program runs hourly; without that window one
  stale stamp is 24 emails a day.
- **One thread per outage.** The re-alerts and the resolve are replies on the
  thread the first alert started, so the inbox carries one row whose newest
  message is the current state. Nothing is deleted - the alert is the only
  record the outage happened - and the resolve clears the thread's unread flag
  *before* replying, so what is left unread is the good news, not the alarm.
  Two things this leans on: Gmail refuses to thread a reply whose subject
  differs from the thread's, so follow-ups reuse the first subject with `Re:`
  and say what changed in the body; and if the thread has gone, the message is
  sent unthreaded rather than not at all.

**Why the file is at `~\.automation\` and nowhere else.** The monitor runs inside
the Claude desktop app, which is an MSIX package, so anything it writes under
`%LOCALAPPDATA%` is redirected into the package's private VFS - correct-looking
from inside Claude, non-existent to any Windows job. That is the failure that
broke three programs for two days in August 2026. This program's own run records
are fine under `%LOCALAPPDATA%` because *it* creates them, from outside the app.
The distinction is who writes, not where.

**`missing` and `unreadable` alert as loudly as `stale`.** A switch that goes
quiet when it cannot see its own input is decoration.

**The heartbeat cannot affect the reap.** It is checked after the kill, in its
own `try`, and it never changes the exit code - a broken mailer or an expired
token must not switch off the killer. The cost is that a mail failure is visible
only in the run record. `test_wiring.py` enforces both halves: the mailer may be
imported only inside `run.check_heartbeat`, and `_run`/`kill`/`snapshot` may not
mention the heartbeat at all.

**This is a deliberate exception to "this program never emails", not a repeal of
it.** Kills still go only into the run record and still reach the owner through the
monitor. If a second subject line ever wants to live here, that is the rule
reasserting itself.

### It watches the failure-fixer too (added 2026-10-21)

`config.WATCHED` lists every heartbeat this program is responsible for, as
`Watch(task, file, max_age_hours, state, armed_marker)`. There are two:

| Watch | File under `~\.automation\heartbeat\` | Stale after | Alert state | Active |
|---|---|---|---|---|
| `task-monitor-refresh` | `task-monitor-refresh.json` | 26 h | `heartbeat_alert.json` | always |
| `failure-fixer` | `failure-fixer.json` | 3 h | `heartbeat_alert_fixer.json` | only while `Documents\Automation\failure_fixer\ARMED` exists |

- **The marker is the switch (ruling R4).** `failure_fixer\register_task.ps1`
  creates `ARMED` when the owner registers the job and deletes it on `-Remove`. No
  marker, no watch: the fixer's heartbeat is not read, nothing is mailed, nothing is
  logged as a problem. With the marker, a missing heartbeat alerts like any other.
- Subject is `[Monitor Heartbeat] failure-fixer has not run in over 3h`, threaded,
  re-alerted and RESOLVED exactly like the monitor's, from its own state file.
- **Each watch is isolated.** A raising read, a mail failure or a bad state file on
  one never stops the other or the reap, and never changes the exit code. A
  watch that is evaluated but not the monitor lands in the run record under
  `heartbeat.watches["failure-fixer"]`; the monitor's record keeps its old flat shape.
- **The prior-outage sweep is per task.** After a NEW outage, older threads are
  trashed with `subject:"Monitor Heartbeat" subject:"<task>"`, so a fresh monitor
  outage cannot trash the fixer's live thread, or the reverse.
- The mailer is still imported only inside `run.check_heartbeat`, once, on the
  first send needed; the actual send is `_deliver`, which is handed the mailer.
  `test_wiring.py` is unchanged.
- `config.py` must stay loadable by path without a `sys.modules` entry
  (`task_monitor`'s wiring test does that), which is why `Watch` is a `NamedTuple`
  and not a `@dataclass`.

## Tests

```
python claude_run_reaper/tests/run_all.py
```

84 assertions across five suites. `test_reap.py` pins the real command lines captured on Aug 17,
2026 - the signature is the whole safety story, so the tests that matter most
are the ones asserting what must NOT be killed. `test_idle.py` does the same for
the idle rule: it pins the four runs measured on Aug 18, 2026 (three that hung
after finishing, one that exited cleanly), and the bulk of it asserts the
non-kills - a working run, a run under the threshold, a long run still writing,
an unknown idle reading, and a conversation the owner is in the middle of.
