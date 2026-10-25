"""The dead-man's switch.

Two things are being protected here, and they pull in opposite directions:

  * it must not go quiet when it cannot see (missing/unreadable are alerts), and
  * it must not email hourly while a known condition lasts.

Every test below is one of those two, or the boundary between them.

    python claude_run_reaper/tests/test_heartbeat.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_run_reaper import config, heartbeat, run                        # noqa: E402

MT = ZoneInfo("America/Denver")
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=MT)

# HERMETIC AGAINST THE FAILURE-FIXER WATCH (added 2026-10-21). That watch switches
# on when the owner's register script creates failure_fixer\ARMED, and the older tests
# below patch only the monitor's constants. Once the marker exists for real they
# would otherwise also read the live fixer heartbeat and state. Point the marker at
# a path that cannot exist so every older test sees exactly the one watch it was
# written for; the new tests set their own marker.
_NO_MARKER = Path(tempfile.gettempdir()) / "claude_run_reaper_test_no_such_dir" / "ARMED"
_REAL_MARKER = config.FIXER_ARMED_MARKER
config.FIXER_ARMED_MARKER = _NO_MARKER


def _write(tmp: Path, hours_ago: float | None, raw: str | None = None) -> Path:
    path = tmp / "task-monitor-refresh.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    stamp = NOW - timedelta(hours=hours_ago)
    path.write_text(json.dumps({"task": config.MONITORED_TASK,
                                "completedAt": stamp.isoformat()}), encoding="utf-8")
    return path


# --- reading the file ------------------------------------------------------

def test_fresh_stamp_is_ok():
    with tempfile.TemporaryDirectory() as tmp:
        r = heartbeat.read(_write(Path(tmp), 3), NOW)
        assert r.kind == heartbeat.OK, r.kind
        assert 2.9 < r.age_hours < 3.1


def test_a_stamp_older_than_the_threshold_is_stale():
    with tempfile.TemporaryDirectory() as tmp:
        r = heartbeat.read(_write(Path(tmp), 30), NOW)
        assert r.kind == heartbeat.STALE, r.kind
        assert "30.0h ago" in r.detail


def test_the_threshold_boundary_is_inclusive():
    """26h exactly is still OK - the monitor runs daily, so 24h + grace has to
    pass without alerting or every slightly-late run pages the owner."""
    with tempfile.TemporaryDirectory() as tmp:
        assert heartbeat.read(_write(Path(tmp), config.HEARTBEAT_MAX_AGE_HOURS),
                              NOW).kind == heartbeat.OK
        assert heartbeat.read(_write(Path(tmp), config.HEARTBEAT_MAX_AGE_HOURS + 0.1),
                              NOW).kind == heartbeat.STALE


def test_a_missing_file_alerts_rather_than_passing():
    with tempfile.TemporaryDirectory() as tmp:
        r = heartbeat.read(Path(tmp) / "nope.json", NOW)
        assert r.kind == heartbeat.MISSING, r.kind


def test_an_unparseable_file_alerts_rather_than_passing():
    for raw in ("{not json", json.dumps({"task": "x"}),
                json.dumps({"completedAt": "the day before yesterday"})):
        with tempfile.TemporaryDirectory() as tmp:
            r = heartbeat.read(_write(Path(tmp), None, raw), NOW)
            assert r.kind == heartbeat.UNREADABLE, "%s -> %s" % (raw, r.kind)


def test_a_naive_stamp_is_read_as_mountain_not_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        naive = (NOW - timedelta(hours=2)).replace(tzinfo=None).isoformat()
        path = _write(Path(tmp), None, json.dumps({"completedAt": naive}))
        assert heartbeat.read(path, NOW).kind == heartbeat.OK


def test_a_future_stamp_is_not_an_alert():
    """A clock change is the monitor's problem to report, not a reason to page."""
    with tempfile.TemporaryDirectory() as tmp:
        assert heartbeat.read(_write(Path(tmp), -5), NOW).kind == heartbeat.OK


def test_a_stamp_written_with_a_utf8_bom_still_reads():
    """PowerShell 5.1 `Set-Content -Encoding utf8` prepends a BOM. Plain utf-8
    chokes on it; the reader uses utf-8-sig so a formatting slip in the monitor's
    stamp never flips the switch to UNREADABLE. (2026-08-29)"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "task-monitor-refresh.json"
        stamp = (NOW - timedelta(hours=3)).isoformat()
        payload = json.dumps({"task": config.MONITORED_TASK, "completedAt": stamp})
        path.write_text(payload, encoding="utf-8-sig")          # BOM-prefixed
        assert path.read_bytes()[:3] == b"\xef\xbb\xbf", "fixture must have a BOM"
        assert heartbeat.read(path, NOW).kind == heartbeat.OK


def test_alert_state_written_with_a_bom_still_loads():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        path.write_text(json.dumps({"alertedAt": NOW.isoformat(), "kind": "stale"}),
                        encoding="utf-8-sig")
        assert heartbeat.load_state(path).get("kind") == "stale"


# --- deciding whether to send ----------------------------------------------

def _stale() -> heartbeat.Reading:
    return heartbeat.Reading(heartbeat.STALE, NOW - timedelta(hours=30), 30.0, "x")


def _ok() -> heartbeat.Reading:
    return heartbeat.Reading(heartbeat.OK, NOW - timedelta(hours=2), 2.0, "x")


def test_a_clean_reading_with_no_prior_alert_sends_nothing():
    assert heartbeat.decide(_ok(), {}, NOW).send is False


def test_first_stale_reading_sends():
    d = heartbeat.decide(_stale(), {}, NOW)
    assert d.send is True
    assert config.HEARTBEAT_SUBJECT_TAG in d.subject
    assert d.state.get("alertedAt")


def test_it_does_not_re_alert_hourly():
    """This program runs hourly. Without the window that is 24 emails a day."""
    state = {"alertedAt": (NOW - timedelta(hours=3)).isoformat(), "kind": "stale"}
    assert heartbeat.decide(_stale(), state, NOW).send is False


def test_it_re_alerts_once_the_window_passes():
    state = {"alertedAt": (NOW - timedelta(hours=config.HEARTBEAT_RE_ALERT_HOURS + 1)).isoformat(),
             "kind": "stale"}
    assert heartbeat.decide(_stale(), state, NOW).send is True


def test_unparseable_state_re_alerts_rather_than_going_quiet():
    assert heartbeat.decide(_stale(), {"alertedAt": "???"}, NOW).send is True


def test_recovery_is_reported_once_then_clears():
    state = {"alertedAt": (NOW - timedelta(hours=3)).isoformat(), "kind": "stale"}
    d = heartbeat.decide(_ok(), state, NOW)
    assert d.send is True and "RESOLVED" in d.subject
    assert d.state == {}, "state must clear so the next failure alerts at once"
    assert heartbeat.decide(_ok(), d.state, NOW).send is False


# --- one thread per outage -------------------------------------------------

def test_the_first_alert_starts_a_thread_and_records_its_subject():
    d = heartbeat.decide(_stale(), {}, NOW)
    assert d.thread_id is None, "there is nothing to reply to yet"
    assert d.state["subject"] == d.subject, "the follow-ups need this verbatim"


def test_a_re_alert_replies_on_the_first_alerts_thread():
    state = {"alertedAt": (NOW - timedelta(hours=config.HEARTBEAT_RE_ALERT_HOURS + 1)).isoformat(),
             "kind": "stale", "threadId": "T1", "subject": "[Monitor Heartbeat] first"}
    d = heartbeat.decide(_stale(), state, NOW)
    assert d.send is True and d.thread_id == "T1"
    assert d.subject == "Re: [Monitor Heartbeat] first"
    assert d.state["threadId"] == "T1", "the thread must survive the re-alert"


def test_a_follow_up_never_changes_the_subject():
    """Gmail will not thread a reply whose Subject differs from the thread's, so
    a changed subject silently produces the second inbox row this whole change
    exists to remove. The news goes in the body."""
    state = {"alertedAt": (NOW - timedelta(hours=3)).isoformat(), "kind": "stale",
             "threadId": "T1", "subject": "[Monitor Heartbeat] first"}
    d = heartbeat.decide(_ok(), state, NOW)          # the resolve
    assert d.subject == "Re: [Monitor Heartbeat] first"
    assert d.resolved is True and d.thread_id == "T1"
    assert "RESOLVED" in d.body, "the body is the only place left to say it"

    # and a re-alert that found a DIFFERENT kind keeps the subject too
    aged = dict(state, alertedAt=(NOW - timedelta(
        hours=config.HEARTBEAT_RE_ALERT_HOURS + 1)).isoformat())
    missing = heartbeat.Reading(heartbeat.MISSING, None, None, "no file")
    assert heartbeat.decide(missing, aged, NOW).subject == "Re: [Monitor Heartbeat] first"


def test_state_written_before_threading_existed_still_sends():
    """Upgrading must not turn one quiet program into a silent one."""
    state = {"alertedAt": (NOW - timedelta(hours=3)).isoformat(), "kind": "stale"}
    d = heartbeat.decide(_ok(), state, NOW)
    assert d.send is True and d.thread_id is None
    assert "RESOLVED" in d.subject, "with no thread to join, it says so up front"


def test_the_subject_tag_cannot_collide_with_the_monitors_own_trash_sweep():
    """task-monitor-refresh STEP 4 trashes every prior mail whose subject carries
    'Routine/Program Monitor'. Sharing that string would have the monitor delete
    the alert that says the monitor is dead."""
    for text in ("Routine/Program Monitor", "Task Monitor"):
        assert text.lower() not in config.HEARTBEAT_SUBJECT_TAG.lower()


def test_the_body_says_what_is_at_stake_and_who_sent_it():
    body = heartbeat.decide(_stale(), {}, NOW).body
    for expected in ("overdue", "claude_run_reaper", config.MONITORED_TASK):
        assert expected in body, expected


# --- state on disk ---------------------------------------------------------

def test_saving_an_empty_state_removes_the_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sub" / "heartbeat_alert.json"
        heartbeat.save_state(path, {"alertedAt": NOW.isoformat()})
        assert heartbeat.load_state(path)["alertedAt"]
        heartbeat.save_state(path, {})
        assert not path.exists() and heartbeat.load_state(path) == {}


# --- wiring into the run ---------------------------------------------------

def test_a_dry_run_never_sends_and_never_writes_state():
    with tempfile.TemporaryDirectory() as tmp:
        hb, st = config.HEARTBEAT_FILE, config.HEARTBEAT_STATE
        config.HEARTBEAT_FILE = _write(Path(tmp), 40)
        config.HEARTBEAT_STATE = Path(tmp) / "state.json"
        try:
            out = run.check_heartbeat(NOW, send_mail=False)
            assert out["kind"] == heartbeat.STALE
            assert out["emailed"] is False and out["wouldEmail"]
            assert not config.HEARTBEAT_STATE.exists()
        finally:
            config.HEARTBEAT_FILE, config.HEARTBEAT_STATE = hb, st


def test_a_failed_send_leaves_no_state_so_it_retries_next_hour():
    """The one failure mode that would silently disarm this: marking an alert
    sent when the mail never left."""
    with tempfile.TemporaryDirectory() as tmp:
        hb, st = config.HEARTBEAT_FILE, config.HEARTBEAT_STATE
        config.HEARTBEAT_FILE = _write(Path(tmp), 40)
        config.HEARTBEAT_STATE = Path(tmp) / "state.json"
        previous = sys.modules.get("common")
        sys.modules["common"] = _stub_common()
        try:
            out = run.check_heartbeat(NOW, send_mail=True)
            assert out["emailed"] is False
            assert "mailError" in out
            assert not config.HEARTBEAT_STATE.exists(), "a failed send must not record one"
        finally:
            config.HEARTBEAT_FILE, config.HEARTBEAT_STATE = hb, st
            sys.modules.pop("common", None)
            if previous is not None:
                sys.modules["common"] = previous


def test_the_heartbeat_lands_in_the_run_record():
    """task-monitor-refresh reads these records; what they omit is invisible."""
    with tempfile.TemporaryDirectory() as tmp:
        original = config.LOG_DIR
        config.LOG_DIR = Path(tmp)
        try:
            run._write_record("exit 0", [], [], {"kind": "stale", "emailed": True})
            written = sorted(config.LOG_DIR.glob("*.json"))
            record = json.loads(written[0].read_text(encoding="utf-8"))
            assert record["heartbeat"]["kind"] == "stale"
        finally:
            config.LOG_DIR = original


def test_the_heartbeat_path_is_outside_the_msix_redirect():
    """The monitor runs inside the Claude MSIX package, so anything it writes
    under AppData\\Local lands in the package VFS and this program never sees it.
    That is the failure that broke three programs for two days in August."""
    parts = [p.lower() for p in config.HEARTBEAT_FILE.parts]
    assert "appdata" not in parts, config.HEARTBEAT_FILE
    assert "documents" not in parts, "Documents is a Drive mirror"
    assert ".automation" in parts, config.HEARTBEAT_FILE


def test_the_resolve_replies_into_the_thread_and_clears_its_unread():
    """One inbox row per outage, its newest message the current state - and the
    old alert marked read BEFORE the reply, so the row is unread for the resolve
    and not for the alert it supersedes."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = _FakeMailer()
        out = _check_with(Path(tmp), 2, {"alertedAt": (NOW - timedelta(hours=3)).isoformat(),
                                         "kind": "stale", "threadId": "T1",
                                         "subject": "[Monitor Heartbeat] first"}, fake)
        assert out["emailed"] is True
        assert fake.calls[0][0] == "latest", "In-Reply-To has to be fetched first"
        assert ("label", "T1", (), ("UNREAD",)) in fake.calls
        assert fake.calls.index(("label", "T1", (), ("UNREAD",))) < \
            [c[0] for c in fake.calls].index("send"), "read AFTER the send hides it"
        sent = [c for c in fake.calls if c[0] == "send"][0]
        assert sent[2] == "T1" and sent[3] == "<rfc822@mail>"
        assert not (Path(tmp) / "state.json").exists(), "the outage is closed out"


def test_a_deleted_thread_falls_back_to_a_fresh_send():
    """The owner deleting the alert must not silence the switch."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = _FakeMailer(fail_latest=True)
        out = _check_with(Path(tmp), 2, {"alertedAt": (NOW - timedelta(hours=3)).isoformat(),
                                         "kind": "stale", "threadId": "GONE",
                                         "subject": "[Monitor Heartbeat] first"}, fake)
        assert out["emailed"] is True
        sent = [c for c in fake.calls if c[0] == "send"][0]
        assert sent[2] is None and sent[3] == "", "unthreaded rather than not at all"
        assert not any(c[0] == "label" for c in fake.calls), "no thread to mark read"


def test_the_first_alert_records_the_thread_it_created():
    """Without this the next message has nothing to reply to."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = _FakeMailer()
        out = _check_with(Path(tmp), 40, {}, fake)
        assert out["emailed"] is True and out["threadId"] == "NEW"
        saved = heartbeat.load_state(Path(tmp) / "state.json")
        assert saved["threadId"] == "NEW" and saved["subject"]
        assert not any(c[0] == "latest" for c in fake.calls), "nothing to reply to yet"


def test_a_fresh_outage_trashes_the_older_ones_and_keeps_its_own():
    """Resolved outages must not pile up in the Claude label forever."""
    with tempfile.TemporaryDirectory() as tmp:
        gmail = _FakeGmail(["OLD1", "NEW", "OLD2"])
        out = _check_with(Path(tmp), 40, {}, _FakeMailer(), gmail)
        assert out["emailed"] is True and out["trashedPrior"] == 2
        assert gmail.trashed == ["OLD1", "OLD2"], "the new alert's own thread survives"
        assert config.HEARTBEAT_SWEEP_PHRASE in gmail.queries[0]


def test_a_re_alert_or_a_resolve_never_sweeps():
    """Only a NEW outage replaces the old ones - never the live one's follow-ups."""
    for hours_ago in (40, 2):       # re-alert on a stale stamp, then the all-clear
        with tempfile.TemporaryDirectory() as tmp:
            gmail = _FakeGmail(["T1", "OLD1"])
            out = _check_with(Path(tmp), hours_ago,
                              {"alertedAt": (NOW - timedelta(hours=30)).isoformat(),
                               "kind": "stale", "threadId": "T1",
                               "subject": "[Monitor Heartbeat] first"},
                              _FakeMailer(), gmail)
            assert out["emailed"] is True
            assert gmail.queries == [] and gmail.trashed == [], hours_ago


class _FakeGmail:
    """Just enough of the Gmail API for the prior-outage sweep."""

    def __init__(self, thread_ids):
        self.thread_ids, self.queries, self.trashed = thread_ids, [], []

    def users(self):
        return self

    def threads(self):
        return self

    def list(self, userId, q, maxResults):
        self.queries.append(q)
        return _Call({"threads": [{"id": t} for t in self.thread_ids]})

    def modify(self, userId, id, body):
        assert body == {"addLabelIds": ["TRASH"]}, "trash only, never a delete"
        self.trashed.append(id)
        return _Call({})


class _Call:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class _FakeMailer:
    """A mailer that records instead of sending."""

    def __init__(self, fail_latest: bool = False):
        self.calls: list[tuple] = []
        self.fail_latest = fail_latest

    def latest_message(self, _service, thread_id):
        if self.fail_latest:
            raise RuntimeError("thread %s not found" % thread_id)
        self.calls.append(("latest", thread_id))
        return "m1", "<rfc822@mail>"

    def label_thread(self, _service, thread_id, add, remove=None):
        self.calls.append(("label", thread_id, tuple(add), tuple(remove or ())))

    def send(self, subject, _html, _body, thread_id=None, in_reply_to="", **_kw):
        self.calls.append(("send", subject, thread_id, in_reply_to))
        return "msg1", thread_id or "NEW"


def _check_with(tmp: Path, hours_ago: float | None, state: dict, fake,
                gmail=None) -> dict:
    """check_heartbeat against a temp heartbeat file, temp state and a fake mailer."""
    hb, st = config.HEARTBEAT_FILE, config.HEARTBEAT_STATE
    config.HEARTBEAT_FILE = _write(tmp, hours_ago)
    config.HEARTBEAT_STATE = tmp / "state.json"
    if state:
        heartbeat.save_state(config.HEARTBEAT_STATE, state)
    previous = sys.modules.get("common")
    sys.modules["common"] = _stub_common(fake, gmail)
    try:
        return run.check_heartbeat(NOW, send_mail=True)
    finally:
        config.HEARTBEAT_FILE, config.HEARTBEAT_STATE = hb, st
        sys.modules.pop("common", None)
        if previous is not None:
            sys.modules["common"] = previous


def _stub_common(fake=None, gmail=None):
    """Stands in for the `common` package so no test can send real mail.

    Stubbing the PACKAGE, not `common.mailer`: run.py does `from common import
    mailer`, which reads the attribute off an already-imported package and would
    walk straight past a sys.modules["common.mailer"] entry.
    """
    stub = types.ModuleType("common")
    mailer = types.ModuleType("common.mailer")

    def send(*_args, **_kwargs):
        raise RuntimeError("token expired")

    mailer.send = fake.send if fake else send
    if fake:
        mailer.latest_message = fake.latest_message
        mailer.label_thread = fake.label_thread
    auth = types.ModuleType("common.google_auth")
    auth.gmail_service = lambda: gmail if gmail is not None else object()
    stub.mailer = mailer
    stub.google_auth = auth
    return stub


# --- the failure-fixer watch (added 2026-10-21) -----------------------------
#
# Two watches, read independently. The monitor's own behaviour is pinned by every
# test above and must not move; these pin what is NEW: the fixer's file, its 3 h
# threshold, its own state, the ARMED marker, isolation between the two, and a
# trash sweep that cannot reach across to the other watch's live thread.

FIXER_SUBJECT_STALE = "[Monitor Heartbeat] failure-fixer has not run in over 3h"
MONITOR_SUBJECT_STALE = "[Monitor Heartbeat] task-monitor-refresh has not run in over 26h"


class _SubjectGmail(_FakeGmail):
    """A mailbox that knows each thread's subject, so a sweep is judged by what it
    would REALLY match. Gmail drops punctuation and matches subject:"..." as a
    phrase; this folds both sides the same way."""

    def __init__(self, subjects: dict):
        super().__init__(list(subjects))
        self.subjects = subjects

    @staticmethod
    def _fold(text: str) -> str:
        return " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())

    def list(self, userId, q, maxResults):
        import re
        self.queries.append(q)
        terms = [self._fold(t) for t in re.findall(r'subject:"([^"]+)"', q)]
        hits = [t for t, subj in self.subjects.items()
                if all(term in self._fold(subj) for term in terms)]
        return _Call({"threads": [{"id": t} for t in hits]})


class _TwoThreadMailer(_FakeMailer):
    """Gives every fresh send its own thread id, and can refuse one task's mail."""

    def __init__(self, refuse: str | None = None):
        super().__init__()
        self.refuse, self.sent = refuse, []

    def send(self, subject, _html, _body, thread_id=None, in_reply_to="", **_kw):
        if self.refuse and self.refuse in subject:
            raise RuntimeError("token expired for %s" % self.refuse)
        self.sent.append(subject)
        self.calls.append(("send", subject, thread_id, in_reply_to))
        return "msg%d" % len(self.sent), thread_id or "NEW-%d" % len(self.sent)


def _write_named(tmp: Path, name: str, task: str, hours_ago: float) -> Path:
    path = tmp / name
    path.write_text(json.dumps({"task": task,
                                "completedAt": (NOW - timedelta(hours=hours_ago)).isoformat()}),
                    encoding="utf-8")
    return path


def _two_watches(tmp: Path, monitor_hours, fixer_hours, armed: bool = True, fake=None,
                 gmail=None, send_mail: bool = True, monitor_state=None,
                 fixer_state=None, read_raises=()):
    """check_heartbeat with BOTH watches pointed at temp files. `*_hours` None means
    no heartbeat file. `read_raises` names tasks whose read() blows up."""
    names = ("HEARTBEAT_FILE", "HEARTBEAT_STATE", "FIXER_HEARTBEAT_FILE",
             "FIXER_HEARTBEAT_STATE", "FIXER_ARMED_MARKER")
    saved = {n: getattr(config, n) for n in names}
    real_read = heartbeat.read
    config.HEARTBEAT_FILE = tmp / "monitor.json"
    config.HEARTBEAT_STATE = tmp / "monitor_state.json"
    config.FIXER_HEARTBEAT_FILE = tmp / "fixer.json"
    config.FIXER_HEARTBEAT_STATE = tmp / "fixer_state.json"
    config.FIXER_ARMED_MARKER = tmp / "ARMED"
    if monitor_hours is not None:
        _write_named(tmp, "monitor.json", config.MONITORED_TASK, monitor_hours)
    if fixer_hours is not None:
        _write_named(tmp, "fixer.json", "failure-fixer", fixer_hours)
    if armed:
        config.FIXER_ARMED_MARKER.write_text("", encoding="utf-8")
    if monitor_state:
        heartbeat.save_state(config.HEARTBEAT_STATE, monitor_state)
    if fixer_state:
        heartbeat.save_state(config.FIXER_HEARTBEAT_STATE, fixer_state)

    def read(path, now, *args, **kwargs):
        for task, file in (("task-monitor-refresh", config.HEARTBEAT_FILE),
                           ("failure-fixer", config.FIXER_HEARTBEAT_FILE)):
            if task in read_raises and path == file:
                raise RuntimeError("disk fell over reading %s" % task)
        return real_read(path, now, *args, **kwargs)

    heartbeat.read = read
    previous = sys.modules.get("common")
    sys.modules["common"] = _stub_common(fake, gmail)
    try:
        return run.check_heartbeat(NOW, send_mail=send_mail)
    finally:
        heartbeat.read = real_read
        for n, v in saved.items():
            setattr(config, n, v)
        sys.modules.pop("common", None)
        if previous is not None:
            sys.modules["common"] = previous


def test_two_watches_are_declared_with_the_agreed_values():
    watches = {w.task: w for w in config.WATCHED}
    assert list(watches) == ["task-monitor-refresh", "failure-fixer"], list(watches)
    m, f = watches["task-monitor-refresh"], watches["failure-fixer"]
    assert (m.file, m.max_age_hours, m.state) == (
        config.HEARTBEAT_FILE, 26.0, config.HEARTBEAT_STATE)
    assert m.armed_marker is None, "the monitor watch is always on"
    assert f.file == Path.home() / ".automation" / "heartbeat" / "failure-fixer.json"
    assert f.max_age_hours == 3.0
    assert f.state == Path.home() / "AppData" / "Local" / "claude_run_reaper" / \
        "heartbeat_alert_fixer.json"
    assert f.armed_marker == config.FIXER_ARMED_MARKER


def test_the_fixer_marker_is_the_register_scripts_file():
    assert _REAL_MARKER == Path.home() / "Documents" / "Automation" / "failure_fixer" / "ARMED", \
        _REAL_MARKER


def test_the_fixer_heartbeat_path_is_outside_the_msix_redirect():
    parts = [p.lower() for p in config.FIXER_HEARTBEAT_FILE.parts]
    assert "appdata" not in parts and "documents" not in parts
    assert ".automation" in parts


def test_no_marker_skips_the_fixer_watch_entirely():
    """A missing fixer heartbeat before the owner arms the program must not page,
    and must not even be read. The monitor watch carries on untouched."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = _TwoThreadMailer()
        out = _two_watches(Path(tmp), 40, 99, armed=False, fake=fake)
        assert out["kind"] == heartbeat.STALE and out["emailed"] is True
        assert fake.sent == [MONITOR_SUBJECT_STALE], fake.sent
        assert "watches" not in out, "a skipped watch leaves no trace in the record"
        assert not (Path(tmp) / "fixer_state.json").exists()


def test_no_marker_and_no_fixer_file_is_silent():
    with tempfile.TemporaryDirectory() as tmp:
        fake = _TwoThreadMailer()
        out = _two_watches(Path(tmp), 2, None, armed=False, fake=fake)
        assert out["kind"] == heartbeat.OK and fake.sent == []
        assert "watches" not in out


def test_no_marker_never_reads_the_fixer_file():
    with tempfile.TemporaryDirectory() as tmp:
        out = _two_watches(Path(tmp), 2, 99, armed=False, read_raises=("failure-fixer",))
        assert out["kind"] == heartbeat.OK and "watches" not in out


def test_with_the_marker_a_missing_fixer_file_alerts():
    """Armed means the heartbeat is expected. Silence when it cannot be seen is the
    one thing this switch must never do."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = _TwoThreadMailer()
        out = _two_watches(Path(tmp), 2, None, armed=True, fake=fake)
        assert out["kind"] == heartbeat.OK and out["emailed"] is False
        assert out["watches"]["failure-fixer"]["kind"] == heartbeat.MISSING
        assert fake.sent == ["[Monitor Heartbeat] failure-fixer has not run in missing"]


def test_a_stale_fixer_alerts_while_the_monitor_stays_ok():
    with tempfile.TemporaryDirectory() as tmp:
        out = _two_watches(Path(tmp), 2, 5, send_mail=False)
        assert out["kind"] == heartbeat.OK and out["emailed"] is False
        assert "wouldEmail" not in out, "the monitor is fine and must stay quiet"
        fixer = out["watches"]["failure-fixer"]
        assert fixer["kind"] == heartbeat.STALE
        assert fixer["wouldEmail"] == FIXER_SUBJECT_STALE, fixer["wouldEmail"]
        assert not (Path(tmp) / "fixer_state.json").exists(), "a dry run writes no state"


def test_the_two_thresholds_are_independent():
    """5 h is stale for the fixer (3 h) and fresh for the monitor (26 h)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = _two_watches(Path(tmp), 5, 5, send_mail=False)
        assert out["kind"] == heartbeat.OK
        assert out["watches"]["failure-fixer"]["kind"] == heartbeat.STALE
    with tempfile.TemporaryDirectory() as tmp:
        out = _two_watches(Path(tmp), 2.9, 2.9, send_mail=False)
        assert out["watches"]["failure-fixer"]["kind"] == heartbeat.OK
    with tempfile.TemporaryDirectory() as tmp:
        out = _two_watches(Path(tmp), 3.1, 3.1, send_mail=False)
        assert out["watches"]["failure-fixer"]["kind"] == heartbeat.STALE
        assert out["kind"] == heartbeat.OK


def test_both_stale_sends_two_threads_with_their_own_state():
    with tempfile.TemporaryDirectory() as tmp:
        fake = _TwoThreadMailer()
        out = _two_watches(Path(tmp), 40, 5, fake=fake)
        assert sorted(fake.sent) == sorted([MONITOR_SUBJECT_STALE, FIXER_SUBJECT_STALE])
        assert out["emailed"] is True and out["watches"]["failure-fixer"]["emailed"] is True
        m = heartbeat.load_state(Path(tmp) / "monitor_state.json")
        f = heartbeat.load_state(Path(tmp) / "fixer_state.json")
        assert m["subject"] == MONITOR_SUBJECT_STALE and f["subject"] == FIXER_SUBJECT_STALE
        assert m["threadId"] != f["threadId"], "one thread per outage, per watch"


def test_the_fixer_re_alert_window_and_resolve_use_the_fixer_state():
    aged = {"alertedAt": (NOW - timedelta(hours=3)).isoformat(), "kind": "stale",
            "threadId": "F1", "subject": FIXER_SUBJECT_STALE}
    with tempfile.TemporaryDirectory() as tmp:           # still stale, inside the window
        fake = _TwoThreadMailer()
        out = _two_watches(Path(tmp), 2, 5, fake=fake, fixer_state=aged)
        assert fake.sent == [] and out["watches"]["failure-fixer"]["emailed"] is False
    with tempfile.TemporaryDirectory() as tmp:           # recovered: RESOLVED on F1
        fake = _TwoThreadMailer()
        out = _two_watches(Path(tmp), 2, 1, fake=fake, fixer_state=aged)
        assert fake.sent == ["Re: " + FIXER_SUBJECT_STALE]
        assert ("label", "F1", (), ("UNREAD",)) in fake.calls
        assert not (Path(tmp) / "fixer_state.json").exists(), "outage closed out"


def test_the_fixer_alert_body_names_the_fixer_and_its_threshold():
    stale = heartbeat.Reading(heartbeat.STALE, NOW - timedelta(hours=5), 5.0, "x")
    d = heartbeat.decide(stale, {}, NOW, task="failure-fixer", max_age_hours=3.0)
    assert d.subject == FIXER_SUBJECT_STALE
    for expected in ("failure-fixer", "3 hours", "claude_run_reaper"):
        assert expected in d.body, expected
    assert "task-monitor-refresh" not in d.body, "a fixer alert must not talk about the monitor"
    ok = heartbeat.Reading(heartbeat.OK, NOW - timedelta(hours=1), 1.0, "x")
    resolved = heartbeat.decide(ok, {"alertedAt": NOW.isoformat()}, NOW,
                                task="failure-fixer", max_age_hours=3.0)
    assert resolved.subject == "[Monitor Heartbeat] RESOLVED - failure-fixer is running again"
    assert "failure-fixer" in resolved.body and "task-monitor-refresh" not in resolved.body


def test_a_raising_read_on_one_watch_still_lets_the_other_run():
    with tempfile.TemporaryDirectory() as tmp:           # fixer read breaks
        fake = _TwoThreadMailer()
        out = _two_watches(Path(tmp), 40, 5, fake=fake, read_raises=("failure-fixer",))
        assert out["kind"] == heartbeat.STALE and out["emailed"] is True
        assert fake.sent == [MONITOR_SUBJECT_STALE]
        assert out["watches"]["failure-fixer"]["kind"] == "check-failed"
    with tempfile.TemporaryDirectory() as tmp:           # monitor read breaks
        fake = _TwoThreadMailer()
        out = _two_watches(Path(tmp), 40, 5, fake=fake, read_raises=("task-monitor-refresh",))
        assert out["kind"] == "check-failed"
        assert fake.sent == [FIXER_SUBJECT_STALE]
        assert out["watches"]["failure-fixer"]["emailed"] is True


def test_a_mail_failure_on_one_watch_never_stops_the_other():
    with tempfile.TemporaryDirectory() as tmp:
        fake = _TwoThreadMailer(refuse="task-monitor-refresh")
        out = _two_watches(Path(tmp), 40, 5, fake=fake)
        assert "mailError" in out and out["emailed"] is False
        assert not (Path(tmp) / "monitor_state.json").exists(), "retries next hour"
        assert fake.sent == [FIXER_SUBJECT_STALE]
        assert out["watches"]["failure-fixer"]["emailed"] is True
    with tempfile.TemporaryDirectory() as tmp:
        fake = _TwoThreadMailer(refuse="failure-fixer")
        out = _two_watches(Path(tmp), 40, 5, fake=fake)
        assert out["emailed"] is True and fake.sent == [MONITOR_SUBJECT_STALE]
        assert "mailError" in out["watches"]["failure-fixer"]
        assert not (Path(tmp) / "fixer_state.json").exists()


def test_check_heartbeat_never_raises_even_if_the_watch_list_is_broken():
    real = config.watched

    def boom():
        raise RuntimeError("config blew up")
    try:
        config.watched = boom
        out = run.check_heartbeat(NOW, send_mail=False)
        assert out["kind"] == "check-failed"
    finally:
        config.watched = real


def _sweep_box():
    return _SubjectGmail({
        "M_OLD": MONITOR_SUBJECT_STALE,
        "M_RE": "Re: " + MONITOR_SUBJECT_STALE,
        "F_OLD": FIXER_SUBJECT_STALE,
        "F_RESOLVED": "[Monitor Heartbeat] RESOLVED - failure-fixer is running again",
        "NOISE": "[Fixer] 2 fixed, 0 waiting - Oct 21, 2026",
    })


def test_a_new_fixer_outage_never_trashes_the_monitors_live_thread():
    with tempfile.TemporaryDirectory() as tmp:
        gmail = _sweep_box()
        out = _two_watches(Path(tmp), 2, 5, fake=_TwoThreadMailer(), gmail=gmail)
        assert out["watches"]["failure-fixer"]["emailed"] is True
        assert gmail.trashed == ["F_OLD", "F_RESOLVED"], gmail.trashed
        assert "failure-fixer" in gmail.queries[0]
        assert config.HEARTBEAT_SWEEP_PHRASE in gmail.queries[0]


def test_a_new_monitor_outage_never_trashes_the_fixers_live_thread():
    with tempfile.TemporaryDirectory() as tmp:
        gmail = _sweep_box()
        out = _two_watches(Path(tmp), 40, 1, fake=_TwoThreadMailer(), gmail=gmail)
        assert out["emailed"] is True
        assert gmail.trashed == ["M_OLD", "M_RE"], gmail.trashed
        assert "task-monitor-refresh" in gmail.queries[0]
        assert config.HEARTBEAT_SWEEP_PHRASE in gmail.queries[0]


def test_the_sweep_query_is_scoped_to_one_task():
    gmail = _FakeGmail([])
    assert run._trash_prior_outages(gmail, "KEEP", "failure-fixer") == 0
    assert gmail.queries == ['from:owner@example.com '
                             'subject:"Monitor Heartbeat" subject:"failure-fixer"']


if __name__ == "__main__":
    tests =[(n, o) for n, o in sorted(globals().items())
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
