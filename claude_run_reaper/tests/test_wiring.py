"""Tests that the pieces are connected the way the design assumes.

Unit tests pass happily while the snapshot query names the wrong process and the
program cheerfully reaps nothing forever.

    python claude_run_reaper/tests/test_wiring.py
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_run_reaper import config, reap, run                             # noqa: E402


def test_every_module_imports():
    for module in (config, reap, run):
        assert module is not None


def test_state_lives_outside_the_drive_mirror():
    # Documents\ is a Drive mirror. Run logs must not sync to the cloud.
    assert "Documents" not in str(config.LOG_DIR)
    assert "AppData\\Local" in str(config.LOG_DIR)


def test_the_snapshot_query_asks_for_every_field_the_program_needs():
    query = run._PS_SNAPSHOT % config.PROCESS_NAME
    assert "claude.exe" in query
    # ParentProcessId is load-bearing: without it every process looks like an
    # orphan and the remote-control descent protection silently protects nothing.
    for field in ("ProcessId", "ParentProcessId", "CommandLine", "Start"):
        assert field in query


def test_the_snapshot_timestamp_format_is_one_python_can_parse():
    # The PowerShell 'o' round-trip format carries 7 fractional digits, one more
    # than datetime.fromisoformat accepts. This asserts we did not drift back.
    assert "yyyy-MM-ddTHH:mm:sszzz" in run._PS_SNAPSHOT
    assert "ToString('o')" not in run._PS_SNAPSHOT


def test_the_snapshot_parses_a_single_result_as_well_as_a_list():
    # ConvertTo-Json emits a bare object, not a one-item array, when one process
    # matches. Parsing that as a dict would crash on the only interesting day.
    one = json.loads('{"ProcessId":1,"Start":"2026-08-17T03:09:26-06:00","CommandLine":"x"}')
    assert isinstance(one, dict)
    many = json.loads('[{"ProcessId":1,"Start":"2026-08-17T03:09:26-06:00","CommandLine":"x"}]')
    assert isinstance(many, list)


def test_this_program_can_only_send_mail_about_the_heartbeat():
    # The monitor reports; this program acts. Two emailers on one problem is how
    # a daily alert stops being read.
    #
    # NARROWED 2026-08-17, not dropped. The dead-man's switch is the one thing
    # the monitor cannot report, because the monitor is what has failed - so this
    # program mails about THAT and about nothing else. This test is what keeps the
    # exception from widening: the only mailer import in the package has to sit
    # inside run.check_heartbeat, and no reap path may reach it.
    #
    # Matched on the import and the call, not the bare word - the docstrings here
    # talk ABOUT email, and a substring test flagged its own prose.
    # The tests themselves are excluded: this file has to NAME the things it bans.
    package = Path(__file__).resolve().parents[2] / "claude_run_reaper"
    ALLOWED = {"run.py"}
    for path in package.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "smtplib" not in text, "%s hand-rolls mail" % path.name
        if path.name in ALLOWED:
            continue
        for forbidden in ("from common import", "import mailer", "mailer.send"):
            assert forbidden not in text, "%s reaches for the mailer" % path.name

    # run.py may reach for it exactly once, and only from check_heartbeat.
    tree = ast.parse((package / "run.py").read_text(encoding="utf-8"))
    reaching = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and inner.module == "common":
                reaching.append(node.name)
    assert reaching == ["check_heartbeat"], (
        "the mailer must be imported only inside check_heartbeat, found %s" % reaching)

    # And nothing at module scope, which would put it on every reap path.
    for node in tree.body:
        assert not (isinstance(node, ast.ImportFrom) and node.module == "common"),             "importing the mailer at module scope puts it on the reap path"


def test_the_reap_never_decides_anything_from_the_heartbeat():
    # The switch is a passenger. A stale heartbeat, a broken mailer or an expired
    # token must not change which processes get killed, or a mail problem quietly
    # switches off the killer.
    package = Path(__file__).resolve().parents[2] / "claude_run_reaper"
    tree = ast.parse((package / "run.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("_run", "kill", "snapshot"):
            source = ast.unparse(node)
            assert "heartbeat" not in source, "%s reads the heartbeat" % node.name


def test_the_kill_is_a_tree_kill():
    # A hung run owns MCP server subprocesses. Killing the parent alone leaves
    # those resident, which is most of the memory the leak was holding.
    source = (Path(__file__).resolve().parents[2] / "claude_run_reaper" / "run.py"
              ).read_text(encoding="utf-8")
    assert '"/T", "/F"' in source


def test_the_floor_and_the_cap_are_sane():
    assert config.MAX_AGE_HOURS >= 1.0        # below an hour and real runs die
    assert 1 < config.MAX_KILLS_PER_RUN <= 50


def test_the_required_markers_are_the_ones_that_define_a_scheduled_run():
    assert "--disallowedTools AskUserQuestion" in config.REQUIRED
    assert "\\claude-code\\" in config.REQUIRED
    assert "--resume=" in config.FORBIDDEN


def test_remote_control_is_protected_by_both_layers():
    # Belt and braces, on purpose - see the config comment. Either alone would
    # have been one build change away from killing the owner's vacation session.
    assert "--remote-control" in config.FORBIDDEN
    assert "--remote-control" in config.PROTECTED
    # And the alias the owner actually types, which was missing until 2026-09-08.
    assert "rc" in config.PROTECTED_ALIASES


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
