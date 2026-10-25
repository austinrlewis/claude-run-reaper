# claude-run-reaper

A small Windows program that finds and kills Claude Code scheduled runs that launched but never exited, plus a dead-man's switch that emails when the health monitor itself stops running.

It's part of a personal automation project ([portfolio](https://austinrlewis.github.io/#projects)): about twenty scheduled programs and Claude agents that run a short-term rental's tracking unattended. This one is published as a code sample.

## Why it exists

A Claude scheduled task has no exit code, so "last run at" only proves a run started. A run that hangs never dies; it sits resident using CPU. In August 2026, six hung runs piled up over three days, burned about 2.5 CPU-hours, and stopped new scheduled runs from starting. The monitoring task could see them but wasn't allowed to kill them, so this runs as a separate Windows Task Scheduler job.

## What it does

- **Finds leaked runs by command line, not start time.** A scheduled run carries a flag that interactive sessions never do, so a long interactive session is never mistaken for a leak.
- **Leaves alone** remote-control listeners, the sessions started under them, and any conversation someone is actively replying to.
- **Records every kill,** with the evidence, to a run record that the health monitor reads.
- **Acts as a dead-man's switch.** It's the only part of the system that can report the monitor itself has stopped completing runs, and it emails only then.

The detailed design, including the cases it learned from, is in [`claude_run_reaper/README.md`](claude_run_reaper/README.md).

## Layout

```
claude_run_reaper/
  config.py        thresholds and the command-line markers
  reap.py          deciding which processes are leaked runs
  transcripts.py   reading a run's transcript to tell "idle" from "working"
  heartbeat.py     the dead-man's switch for the health monitor
  run.py           entry point: find, kill, record, check the heartbeat
  tests/           134 tests, built from real command lines and incidents
common/            mail and Google-auth helpers (stubs here, see below)
```

## Running the tests

Python 3.10+ (tested on 3.12), standard library only. From the repo root:

```
python -m claude_run_reaper.tests.run_all
```

## Notes

- `common/` is a stub. The real project sends alerts through a shared Gmail helper that isn't part of this sample; the tests replace it with a fake, so they run without any account.
- Paths and the email address are placeholders.
- Windows only: process discovery uses `Get-CimInstance Win32_Process`.
