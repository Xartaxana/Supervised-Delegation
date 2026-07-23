"""Tests for WITNESS ECHO -- the witness/dod_track cross-check
implemented in tools/journal_echo.py (this port's second extension,
alongside TIER ECHO).

Style mirrors tools/test_journal_echo.py (pure-logic unit tests + a
subprocess smoke of the whole hook over stdin, real tmp_path git repos
for the git-mode path). This file is SELF-CONTAINED (does not import
test_journal_echo) -- helpers (git repo, hook invocation, journal
lines) are duplicated locally, the same self-containment principle
journal_echo.py itself documents.

Covers:
 1. green match -> silence.
 2. red contradiction -> loud WARN naming the command.
 3. several runs of the same command: red->green (latest green) ->
    silence; green->red (latest red) -> WARN.
 4. non-empty track with no match -> soft WARN.
 5. "retroactive" in notes -> note, not WARN.
 6. track missing / empty / malformed JSON -> note, not an exception.
 7. a run under a subagent's agent_id still counts (agent_id is not
    filtered).
 8. normalization: a command with extra whitespace/tabs in the witness
    still matches.
 9. a witness with no command at all (prose), non-empty track -> soft
    WARN.
 10. non-builder accepted / accepted with no witness / other events ->
    the cross-check never runs.
 11. a very long witness (10K+) against a track of hundreds of runs --
    no quadratic blowup.
 12. an event whose witness lives in a NOT-new (already-committed) line
    -> not re-triggered.
Plus the rule-6a boundary -- MAX_WITNESS_LINES (exactly 5 / the 6th
"+1 more").

Also a sync test comparing journal_echo._witness_track_path (this
port) and the two OTHER live copies of the same track-path formula
(tools/dod_gate.py, tools/main_gate.py -- both carry their own
module-level _track_path(cwd, session_id), neither imports
dod_track.py -- the same hook self-containment this toolkit's hooks
already hold each other to) against the CANON tools/dod_track.py's own
_track_path, on identical samples -- drift in any of the three trips
the corresponding parametrized case. Plus a ts-format test:
dod_track._now_iso is fixed-width (lexicographic order == chronological
order, the assumption journal_echo._last_by_ts relies on).

Run from toolkit/: python -m pytest tools/test_witness_echo.py -q
"""

import datetime as dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dod_gate  # noqa: E402 -- canon cross-check of the track-path formula (read-only)
import dod_track  # noqa: E402 -- CANON of the track-path formula + _now_iso (read-only)
import main_gate  # noqa: E402 -- canon cross-check of the track-path formula (read-only)

import journal_echo as we  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "journal_echo.py"


# =======================================================================
# helpers -- journal lines
# =======================================================================


def _delegated_head_line(task_id="t-001", ts="2026-07-10T08:00:00"):
    obj = {"ts": ts, "event": "delegated", "agent": "builder", "category": "implementation",
           "notes": "seed task", "task_id": task_id, "model": "sonnet",
           "worker_ref": "cli:2026-07-10T08:00:00"}
    return json.dumps(obj, ensure_ascii=False)


HEAD_LINE = _delegated_head_line()
HEAD_TEXT = HEAD_LINE + "\n"


def _fresh_ts(offset_seconds=0):
    """A fresh ts for e2e silence-assertion tests -- an old ts on a NEW
    line reads as a stale event under this repo's own conventions and
    could confuse an unrelated check; head lines stay historical (they
    are not "new")."""
    return (dt.datetime.now() + dt.timedelta(seconds=offset_seconds)).isoformat(timespec="seconds")


def _accepted_line(ts="2026-07-10T08:10:00", witness="tests pass", notes="accepted",
                    task_id="t-001", by="fable", agent="builder", **kw):
    obj = {"ts": ts, "event": "accepted", "agent": agent, "category": "implementation",
           "notes": notes, "task_id": task_id, "by": by, "model": "sonnet", "witness": witness}
    obj.update(kw)
    return json.dumps(obj, ensure_ascii=False)


def _delegated_line(ts="2026-07-10T08:10:00", task_id="t-002", notes="delegated"):
    obj = {"ts": ts, "event": "delegated", "agent": "builder", "category": "implementation",
           "notes": notes, "task_id": task_id, "model": "sonnet",
           "worker_ref": "cli:" + ts}
    return json.dumps(obj, ensure_ascii=False)


# =======================================================================
# helpers -- real git repos (mirrors test_journal_validator/test_journal_echo)
# =======================================================================


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")


def _init_repo(root: Path):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")


def _write_journal(root: Path, text: str) -> None:
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "routing-log.jsonl").write_text(text, encoding="utf-8")


def _seed_committed_journal(root: Path, text: str = HEAD_TEXT) -> Path:
    _init_repo(root)
    _write_journal(root, text)
    _git(root, "add", "logs/routing-log.jsonl")
    _git(root, "commit", "-q", "-m", "seed journal")
    return root / "logs" / "routing-log.jsonl"


# =======================================================================
# helpers -- dod_track fixture
# =======================================================================


def _run_entry(ts, command, outcome, agent_id=None, tool_name="Bash"):
    return {"ts": ts, "tool_name": tool_name, "command": command, "outcome": outcome,
            "agent_id": agent_id}


def _write_track(root: Path, session_id: str, runs: list) -> Path:
    track_dir = root / ".claude" / "dod_track"
    track_dir.mkdir(parents=True, exist_ok=True)
    path = track_dir / f"{session_id}.json"
    path.write_text(json.dumps({"edits": [], "runs": runs}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    return path


# =======================================================================
# helpers -- running the hook
# =======================================================================


_NO_ORIGINAL_FILE = object()  # sentinel -- omit tool_response.originalFile
# entirely (t-277/t-279: exercises the FALLBACK path of
# journal_echo._resolve_echo_base -- identical to the pre-t-279
# HEAD-diff computation). The default preserves every existing call
# site's payload shape byte-for-byte.


def _post_tool_use_payload(file_path, cwd, session_id="sess-1", tool_name="Edit",
                            original_file=_NO_ORIGINAL_FILE) -> dict:
    tool_response = {"filePath": str(file_path), "success": True}
    if original_file is not _NO_ORIGINAL_FILE:
        # t-277/t-279: tool_response.originalFile (Edit/Write Zod
        # schemas -- see journal_echo.py's "PAYLOAD-SCOPED ECHO BASE").
        tool_response["originalFile"] = original_file
    return {
        "session_id": session_id,
        "transcript_path": "/x/transcript.jsonl",
        "cwd": str(cwd),
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": str(file_path)},
        "tool_response": tool_response,
        "tool_use_id": "tu-1",
    }


def _run_hook(payload, timeout=10) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def _parse_stdout_json(stdout: str):
    if not stdout:
        return None
    payload = json.loads(stdout)
    hook_output = payload["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PostToolUse"
    return hook_output


# =======================================================================
# _normalize_ws -- pure logic
# =======================================================================


def test_normalize_ws_collapses_multiple_spaces():
    assert we._normalize_ws("pytest   tools/x.py") == "pytest tools/x.py"


def test_normalize_ws_collapses_tabs_and_newlines():
    assert we._normalize_ws("pytest\ttools/x.py\n-q") == "pytest tools/x.py -q"


def test_normalize_ws_strips_edges():
    assert we._normalize_ws("  pytest -q  ") == "pytest -q"


def test_normalize_ws_not_a_string_returns_empty():
    assert we._normalize_ws(None) == ""
    assert we._normalize_ws(42) == ""


# =======================================================================
# _load_witness_runs -- pure logic (missing/empty/malformed)
# =======================================================================


def test_load_witness_runs_missing_session_id_none():
    assert we._load_witness_runs(".", None) is None
    assert we._load_witness_runs(".", "") is None


def test_load_witness_runs_missing_file_returns_none(tmp_path):
    assert we._load_witness_runs(str(tmp_path), "no-such-session") is None


def test_load_witness_runs_empty_file_returns_none(tmp_path):
    track_dir = tmp_path / ".claude" / "dod_track"
    track_dir.mkdir(parents=True)
    (track_dir / "sess-1.json").write_text("   \n", encoding="utf-8")
    assert we._load_witness_runs(str(tmp_path), "sess-1") is None


def test_load_witness_runs_malformed_json_returns_none(tmp_path):
    track_dir = tmp_path / ".claude" / "dod_track"
    track_dir.mkdir(parents=True)
    (track_dir / "sess-1.json").write_text("{not valid json", encoding="utf-8")
    assert we._load_witness_runs(str(tmp_path), "sess-1") is None


def test_load_witness_runs_not_a_dict_returns_none(tmp_path):
    track_dir = tmp_path / ".claude" / "dod_track"
    track_dir.mkdir(parents=True)
    (track_dir / "sess-1.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert we._load_witness_runs(str(tmp_path), "sess-1") is None


def test_load_witness_runs_no_runs_key_returns_none(tmp_path):
    track_dir = tmp_path / ".claude" / "dod_track"
    track_dir.mkdir(parents=True)
    (track_dir / "sess-1.json").write_text(json.dumps({"edits": []}), encoding="utf-8")
    assert we._load_witness_runs(str(tmp_path), "sess-1") is None


def test_load_witness_runs_empty_runs_list_returns_empty_list(tmp_path):
    _write_track(tmp_path, "sess-1", [])
    result = we._load_witness_runs(str(tmp_path), "sess-1")
    assert result == []


def test_load_witness_runs_valid_returns_runs(tmp_path):
    runs = [_run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "green")]
    _write_track(tmp_path, "sess-1", runs)
    assert we._load_witness_runs(str(tmp_path), "sess-1") == runs


# =======================================================================
# track-path formula sync test -- CANON is tools/dod_track.py._track_path;
# THREE live copies are checked against it (journal_echo._witness_track_path
# -- this diff; dod_gate._track_path and main_gate._track_path -- pre-
# existing, read-only, not owned by this task). Any of the three
# drifting from the canon fails the corresponding parametrized case.
# =======================================================================


_TRACK_PATH_SAMPLES = [
    ("plain", "sess-1"),
    ("dashed-session-id", "a8ed966d-1ca6-d4de-7000"),
    ("agent-style-id", "agent:a8ed966d1ca6d4de7"),
]


def _track_path_cases():
    for label, session_id in _TRACK_PATH_SAMPLES:
        yield ("journal_echo", we._witness_track_path, label, session_id)
        yield ("dod_gate", dod_gate._track_path, label, session_id)
        yield ("main_gate", main_gate._track_path, label, session_id)


@pytest.mark.parametrize(
    "module_name,func,label,session_id",
    list(_track_path_cases()),
    ids=[f"{m}-{lbl}" for m, _, lbl, _ in _track_path_cases()],
)
def test_track_path_formula_matches_canon_across_all_copies(tmp_path, module_name, func,
                                                              label, session_id):
    cwd = str(tmp_path)
    canon = dod_track._track_path(cwd, session_id)
    candidate = func(cwd, session_id)
    assert candidate == canon, (
        f"{module_name}'s _track_path formula drifted from dod_track._track_path (canon) "
        f"for sample cwd={cwd!r} session_id={session_id!r}"
    )


def test_track_path_canon_itself_is_self_consistent(tmp_path):
    cwd = str(tmp_path)
    session_id = "sess-1"
    assert dod_track._track_path(cwd, session_id) == dod_track._track_path(cwd, session_id)


def test_track_path_empty_cwd_falls_back_to_dot_consistently():
    session_id = "sess-1"
    canon = dod_track._track_path("", session_id)
    assert we._witness_track_path("", session_id) == canon
    assert dod_gate._track_path("", session_id) == canon
    assert main_gate._track_path("", session_id) == canon


# =======================================================================
# ts format -- dod_track._now_iso fixed width (lexicographic order ==
# chronological order, the assumption journal_echo._last_by_ts relies on)
# =======================================================================


_TS_FIXED_WIDTH_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}$")


def test_dod_track_now_iso_matches_fixed_width_format():
    sample = dod_track._now_iso()
    assert _TS_FIXED_WIDTH_RE.match(sample), f"unexpected ts shape: {sample!r}"
    assert len(sample) == 26  # YYYY-MM-DDTHH:MM:SS.ffffff -- fixed width


def test_ts_format_string_ordering_matches_chronological_ordering():
    fmt = "%Y-%m-%dT%H:%M:%S.%f"
    t1 = dt.datetime(2026, 7, 21, 23, 59, 59, 999999)
    t2 = dt.datetime(2026, 7, 22, 0, 0, 0, 1)
    assert t1 < t2
    s1, s2 = t1.strftime(fmt), t2.strftime(fmt)
    assert s1 < s2
    assert len(s1) == len(s2) == 26


# =======================================================================
# _match_witness -- pure logic
# =======================================================================


def test_match_witness_green_last_no_loud():
    runs = [_run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "green")]
    matched, loud = we._match_witness("run: pytest tools/x.py -q -> 5 passed", runs)
    assert matched is True
    assert loud == []


def test_match_witness_red_last_is_loud():
    runs = [_run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "red")]
    matched, loud = we._match_witness("run: pytest tools/x.py -q -> 1 failed", runs)
    assert matched is True
    assert len(loud) == 1
    cmd, ts = loud[0]
    assert cmd == "pytest tools/x.py -q"
    assert ts == "2026-07-21T10:00:00.000000"


def test_match_witness_red_then_green_last_green_silent():
    runs = [
        _run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "red"),
        _run_entry("2026-07-21T10:05:00.000000", "pytest tools/x.py -q", "green"),
    ]
    matched, loud = we._match_witness("pytest tools/x.py -q", runs)
    assert matched is True
    assert loud == []


def test_match_witness_green_then_red_last_red_loud():
    runs = [
        _run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "green"),
        _run_entry("2026-07-21T10:05:00.000000", "pytest tools/x.py -q", "red"),
    ]
    matched, loud = we._match_witness("pytest tools/x.py -q", runs)
    assert matched is True
    assert len(loud) == 1
    assert loud[0][1] == "2026-07-21T10:05:00.000000"


def test_match_witness_no_command_found_not_matched():
    runs = [_run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "green")]
    matched, loud = we._match_witness("a prose paragraph describing manual review", runs)
    assert matched is False
    assert loud == []


def test_match_witness_normalization_double_spaces_and_tabs_in_witness():
    runs = [_run_entry("2026-07-21T10:00:00.000000", "python -m pytest tools/ -q", "green")]
    witness = "control run:\tpython  -m  pytest\ttools/  -q  -> 900 passed"
    matched, loud = we._match_witness(witness, runs)
    assert matched is True
    assert loud == []


def test_match_witness_subagent_agent_id_counts():
    runs = [_run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "green",
                        agent_id="a8ed966d1ca6d4de7")]
    matched, loud = we._match_witness("pytest tools/x.py -q -> 5 passed", runs)
    assert matched is True
    assert loud == []


def test_match_witness_mixed_agent_ids_all_counted():
    runs = [
        _run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "red", agent_id=None),
        _run_entry("2026-07-21T10:05:00.000000", "pytest tools/x.py -q", "green",
                   agent_id="sub-1"),
    ]
    matched, loud = we._match_witness("pytest tools/x.py -q", runs)
    assert matched is True
    assert loud == []  # latest (by ts) among BOTH agent_ids is green


# =======================================================================
# _collect_witness_events -- pure logic (full lattice)
# =======================================================================


def _new_lines(*lines):
    return list(lines)


def test_collect_witness_events_green_match_silent(tmp_path):
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "green"),
    ])
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    line = _accepted_line(witness="pytest tools/x.py -q -> 5 passed")
    events = we._collect_witness_events(_new_lines(line), [], payload)
    assert events == []


def test_collect_witness_events_red_contradiction_loud(tmp_path):
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "red"),
    ])
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    line = _accepted_line(witness="pytest tools/x.py -q -> 1 failed (fixed by hand after)")
    events = we._collect_witness_events(_new_lines(line), [], payload)
    assert len(events) == 1
    kind, line_no, cmd, ts = events[0]
    assert kind == "warn_loud"
    assert line_no == 1
    assert cmd == "pytest tools/x.py -q"


def test_collect_witness_events_no_match_soft_warn(tmp_path):
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "green"),
    ])
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    line = _accepted_line(witness="reviewed manually, looks correct")
    events = we._collect_witness_events(_new_lines(line), [], payload)
    assert len(events) == 1
    assert events[0][0] == "warn_soft"
    assert events[0][1] == 1


def test_collect_witness_events_prose_witness_no_commands_soft_warn(tmp_path):
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-21T10:00:00.000000", "python -m pytest tools/ gateway/ -q", "green"),
    ])
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    line = _accepted_line(witness="All checks look good, reviewed the diff by eye and it is fine.")
    events = we._collect_witness_events(_new_lines(line), [], payload)
    assert len(events) == 1
    assert events[0][0] == "warn_soft"


def test_collect_witness_events_retro_note_no_warn(tmp_path):
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "red"),
    ])
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    line = _accepted_line(witness="pytest tools/x.py -q -> 1 failed",
                           notes="retroactive acceptance, bounds fixed")
    events = we._collect_witness_events(_new_lines(line), [], payload)
    assert len(events) == 1
    kind, line_no, text = events[0]
    assert kind == "note"
    assert text == we.NOTE_RETRO


def test_collect_witness_events_missing_track_note(tmp_path):
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}  # no track file written
    line = _accepted_line(witness="pytest tools/x.py -q -> 5 passed")
    events = we._collect_witness_events(_new_lines(line), [], payload)
    assert len(events) == 1
    kind, line_no, text = events[0]
    assert kind == "note"
    assert text == we.NOTE_TRACK_EMPTY


def test_collect_witness_events_empty_track_file_note(tmp_path):
    track_dir = tmp_path / ".claude" / "dod_track"
    track_dir.mkdir(parents=True)
    (track_dir / "sess-1.json").write_text("", encoding="utf-8")
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    line = _accepted_line(witness="pytest tools/x.py -q -> 5 passed")
    events = we._collect_witness_events(_new_lines(line), [], payload)
    assert events[0][0] == "note"
    assert events[0][2] == we.NOTE_TRACK_EMPTY


def test_collect_witness_events_malformed_json_track_note(tmp_path):
    track_dir = tmp_path / ".claude" / "dod_track"
    track_dir.mkdir(parents=True)
    (track_dir / "sess-1.json").write_text("{not valid", encoding="utf-8")
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    line = _accepted_line(witness="pytest tools/x.py -q -> 5 passed")
    events = we._collect_witness_events(_new_lines(line), [], payload)
    assert events[0][0] == "note"
    assert events[0][2] == we.NOTE_TRACK_EMPTY


def test_collect_witness_events_empty_runs_list_note(tmp_path):
    _write_track(tmp_path, "sess-1", [])
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    line = _accepted_line(witness="pytest tools/x.py -q -> 5 passed")
    events = we._collect_witness_events(_new_lines(line), [], payload)
    assert events[0][0] == "note"
    assert events[0][2] == we.NOTE_TRACK_EMPTY


def test_collect_witness_events_no_exception_on_bad_track(tmp_path):
    track_dir = tmp_path / ".claude" / "dod_track"
    track_dir.mkdir(parents=True)
    (track_dir / "sess-1.json").write_text("not json at all {{{", encoding="utf-8")
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    line = _accepted_line(witness="pytest tools/x.py -q -> 5 passed")
    events = we._collect_witness_events(_new_lines(line), [], payload)  # must not raise
    assert events[0][0] == "note"


def test_collect_witness_events_non_builder_agent_skipped(tmp_path):
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "red"),
    ])
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    line = _accepted_line(witness="pytest tools/x.py -q -> 1 failed", agent="critic")
    events = we._collect_witness_events(_new_lines(line), [], payload)
    assert events == []


def test_collect_witness_events_missing_witness_skipped(tmp_path):
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "red"),
    ])
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    obj = json.loads(_accepted_line())
    del obj["witness"]
    events = we._collect_witness_events(_new_lines(json.dumps(obj)), [], payload)
    assert events == []


def test_collect_witness_events_empty_witness_skipped(tmp_path):
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    line = _accepted_line(witness="   ")
    events = we._collect_witness_events(_new_lines(line), [], payload)
    assert events == []


def test_collect_witness_events_other_event_skipped(tmp_path):
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    obj = json.loads(_delegated_line())
    obj["witness"] = "pytest tools/x.py -q -> 1 failed"
    events = we._collect_witness_events(_new_lines(json.dumps(obj)), [], payload)
    assert events == []


def test_collect_witness_events_malformed_json_line_skipped_not_raised(tmp_path):
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    events = we._collect_witness_events(_new_lines("{not valid json"), [], payload)
    assert events == []


def test_collect_witness_events_line_numbering_accounts_for_head_lines(tmp_path):
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "green"),
    ])
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    head_lines = ["dummy 1", "dummy 2"]
    line = _accepted_line(witness="reviewed by eye, no matching command")
    events = we._collect_witness_events(_new_lines(line), head_lines, payload)
    assert events[0][1] == 3  # len(head_lines) + idx(0) + 1


def test_collect_witness_events_track_read_once_per_call(tmp_path, monkeypatch):
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-21T10:00:00.000000", "pytest tools/x.py -q", "green"),
    ])
    calls = {"n": 0}
    real = we._load_witness_runs

    def counting(cwd, session_id):
        calls["n"] += 1
        return real(cwd, session_id)
    monkeypatch.setattr(we, "_load_witness_runs", counting)
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    lines = [
        _accepted_line(ts="2026-07-10T08:10:00", task_id="t-001", witness="pytest tools/x.py -q"),
        _accepted_line(ts="2026-07-10T08:11:00", task_id="t-001", witness="pytest tools/x.py -q"),
    ]
    we._collect_witness_events(lines, [], payload)
    assert calls["n"] == 1


# =======================================================================
# performance -- 10K+ witness, hundreds of runs, no quadratic blowup
# =======================================================================


def test_collect_witness_events_large_witness_and_track_performs(tmp_path):
    runs = [
        _run_entry(f"2026-07-21T10:{i % 60:02d}:00.000000", f"pytest tools/test_module_{i}.py -q",
                   "green" if i % 3 else "red", agent_id=(f"sub-{i}" if i % 5 == 0 else None))
        for i in range(300)
    ]
    _write_track(tmp_path, "sess-1", runs)
    filler = "x" * 10_000
    witness = f"{filler} manual review only, no command referenced {filler}"
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    line = _accepted_line(witness=witness)
    start = time.perf_counter()
    events = we._collect_witness_events(_new_lines(line), [], payload)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
    assert len(events) == 1
    assert events[0][0] == "warn_soft"  # none of the 300 commands occur in the witness


def test_collect_witness_events_large_witness_finds_embedded_command(tmp_path):
    runs = [
        _run_entry(f"2026-07-21T10:{i % 60:02d}:00.000000", f"pytest tools/test_module_{i}.py -q",
                   "green", agent_id=None)
        for i in range(300)
    ]
    _write_track(tmp_path, "sess-1", runs)
    filler = "x" * 10_000
    witness = f"{filler} ran pytest tools/test_module_150.py -q -> 3 passed {filler}"
    payload = {"session_id": "sess-1", "cwd": str(tmp_path)}
    line = _accepted_line(witness=witness)
    start = time.perf_counter()
    events = we._collect_witness_events(_new_lines(line), [], payload)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
    assert events == []  # matched, green -> silence


# =======================================================================
# build_witness_segment / _format_witness_line -- pure logic, boundaries (6a)
# =======================================================================


def test_build_witness_segment_empty_list():
    assert we.build_witness_segment([]) == ""


def test_build_witness_segment_notes_excluded_from_output():
    assert we.build_witness_segment([("note", 1, "whatever")]) == ""


def test_build_witness_segment_loud_exact_format():
    ev = ("warn_loud", 2, "pytest tools/x.py -q", "2026-07-21T10:00:00.000000")
    seg = we.build_witness_segment([ev])
    assert seg == ("WITNESS ECHO: line 2 contradiction - command 'pytest tools/x.py -q' "
                    "recorded RED in session track (last red at 2026-07-21T10:00:00.000000)")


def test_build_witness_segment_loud_sanitizes_ts_control_chars():
    ev = ("warn_loud", 2, "pytest tools/x.py -q", "2026-07-21T10:00:00\x00\x1f.000000")
    seg = we.build_witness_segment([ev])
    assert "\x00" not in seg
    assert "\x1f" not in seg
    assert "2026-07-21T10:00:00.000000" in seg


def test_build_witness_segment_loud_ts_ascii_only_replaces_non_ascii():
    ev = ("warn_loud", 2, "pytest tools/x.py -q", "клод-2026-07-21T10:00:00")
    seg = we.build_witness_segment([ev], ascii_only=True)
    assert "клод" not in seg
    assert "?" in seg


def test_build_witness_segment_loud_ts_truncated_at_max_message_len():
    giant_ts = "9" * (we.MAX_MESSAGE_LEN + 50)
    ev = ("warn_loud", 2, "pytest tools/x.py -q", giant_ts)
    seg = we.build_witness_segment([ev])
    assert ("9" * we.MAX_MESSAGE_LEN) in seg
    assert ("9" * (we.MAX_MESSAGE_LEN + 1)) not in seg


def test_build_witness_segment_soft_exact_format():
    ev = ("warn_soft", 3)
    seg = we.build_witness_segment([ev])
    assert seg == ("WITNESS ECHO: line 3 witness command(s) not observed in session track "
                    "(batch/cross-session/retro acceptance legitimate - verify manually)")


def test_build_witness_segment_exactly_five_boundary_no_more_suffix():
    events = [("warn_soft", i) for i in range(1, 6)]
    seg = we.build_witness_segment(events)
    assert seg.count("WITNESS ECHO") == 5
    assert "more" not in seg


def test_build_witness_segment_beyond_boundary_six_adds_one_more():
    events = [("warn_soft", i) for i in range(1, 7)]
    seg = we.build_witness_segment(events)
    assert seg.count("WITNESS ECHO") == 5
    assert seg.endswith("; +1 more")


def test_build_witness_segment_ascii_only_true_sanitizes_command():
    ev = ("warn_loud", 2, "команда с кириллицей", "2026-07-21T10:00:00.000000")
    seg = we.build_witness_segment([ev], ascii_only=True)
    assert "команда" not in seg
    assert "?" in seg


def test_build_witness_segment_ascii_only_false_keeps_command_readable():
    ev = ("warn_loud", 2, "команда с кириллицей", "2026-07-21T10:00:00.000000")
    seg = we.build_witness_segment([ev], ascii_only=False)
    assert "команда" in seg


# =======================================================================
# combine_context -- extended with witness_events (backward compatibility)
# =======================================================================


def test_combine_context_two_arg_call_unchanged():
    violations = ["line 2: msg one"]
    assert we.combine_context(violations, []) == we.build_context(violations)


def test_combine_context_witness_only_no_violations_no_tier():
    ev = ("warn_soft", 2)
    ctx = we.combine_context([], [], [ev])
    assert ctx == we.build_witness_segment([ev])
    assert "JOURNAL ECHO" not in ctx
    assert "TIER ECHO" not in ctx


def test_combine_context_all_three_segments_joined():
    violations = ["line 2: msg"]
    tier_ev = (3, "mismatch", "fable", {"claude-opus-4-8": 1})
    witness_ev = ("warn_soft", 4)
    ctx = we.combine_context(violations, [tier_ev], [witness_ev])
    expected = (we.build_context(violations) + "; " + we.build_tier_segment([tier_ev]) +
                "; " + we.build_witness_segment([witness_ev]))
    assert ctx == expected


def test_combine_context_witness_none_defaults_to_empty():
    assert we.combine_context([], [], None) == ""


# =======================================================================
# main() end-to-end -- subprocess smoke
# =======================================================================


def test_e2e_green_witness_silent(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-10T08:05:00.000000", "python -m pytest tools/ gateway/ -q", "green"),
    ])
    new_line = _accepted_line(ts=_fresh_ts(),
                               witness="python -m pytest tools/ gateway/ -q -> 930 passed")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, cwd=tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_e2e_red_witness_contradiction_loud_warn(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-10T08:05:00.000000", "python -m pytest tools/ gateway/ -q", "red"),
    ])
    new_line = _accepted_line(witness="python -m pytest tools/ gateway/ -q -> 3 failed")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, cwd=tmp_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "WITNESS ECHO" in ctx
    assert "recorded RED" in ctx
    assert "python -m pytest tools/ gateway/ -q" in ctx
    assert ctx in result.stderr  # ASCII-only, identical on both channels


def test_e2e_retro_no_warn_even_with_red_track(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-10T08:05:00.000000", "python -m pytest tools/ gateway/ -q", "red"),
    ])
    new_line = _accepted_line(ts=_fresh_ts(),
                               witness="python -m pytest tools/ gateway/ -q -> 3 failed",
                               notes="retroactive fix of missed accepted event")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, cwd=tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_e2e_missing_track_silent_note_not_exception(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    new_line = _accepted_line(ts=_fresh_ts(),
                               witness="python -m pytest tools/ gateway/ -q -> 930 passed")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, cwd=tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_e2e_malformed_track_silent_note_not_exception(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    track_dir = tmp_path / ".claude" / "dod_track"
    track_dir.mkdir(parents=True)
    (track_dir / "sess-1.json").write_text("{not valid json at all", encoding="utf-8")
    new_line = _accepted_line(ts=_fresh_ts(),
                               witness="python -m pytest tools/ gateway/ -q -> 930 passed")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, cwd=tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_e2e_no_match_soft_warn(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-10T08:05:00.000000", "python -m pytest tools/ gateway/ -q", "green"),
    ])
    new_line = _accepted_line(witness="reviewed by eye, everything checks out")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, cwd=tmp_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "WITNESS ECHO" in ctx
    assert "not observed in session track" in ctx


def test_e2e_old_line_witness_not_retriggered(tmp_path):
    old_accepted = _accepted_line(ts="2026-07-10T08:05:00", task_id="t-001",
                                   witness="python -m pytest tools/ gateway/ -q -> 3 failed")
    head_text = HEAD_TEXT + old_accepted + "\n"
    journal_path = _seed_committed_journal(tmp_path, text=head_text)
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-10T08:04:00.000000", "python -m pytest tools/ gateway/ -q", "red"),
    ])
    new_clean_line = _delegated_line(ts=_fresh_ts(), task_id="t-002",
                                      notes="unrelated new clean line")
    journal_path.write_text(head_text + new_clean_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, cwd=tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_e2e_non_builder_accepted_no_witness_check(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-10T08:05:00.000000", "python -m pytest tools/ gateway/ -q", "red"),
    ])
    new_line = _accepted_line(ts=_fresh_ts(),
                               witness="python -m pytest tools/ gateway/ -q -> 3 failed",
                               agent="critic")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, cwd=tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_e2e_witness_payload_scoped_not_reechoed_on_later_unrelated_call(tmp_path):
    # t-277/t-279 (ported from HQ): a WITNESS ECHO contradiction
    # reported on call #1 must NOT be re-echoed on a LATER, unrelated
    # call #2 that appends a different clean line -- call #2's
    # original_file already includes call #1's accepted+witness line,
    # so it's out of scope for call #2 (see
    # journal_echo._resolve_echo_base).
    journal_path = _seed_committed_journal(tmp_path)
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-10T08:05:00.000000", "python -m pytest tools/ gateway/ -q", "red"),
    ])
    contradicting_line = _accepted_line(ts=_fresh_ts(),
                                         witness="python -m pytest tools/ gateway/ -q -> 3 failed",
                                         notes="call #1: contradicting witness")
    after_call_1 = HEAD_TEXT + contradicting_line + "\n"
    journal_path.write_text(after_call_1, encoding="utf-8")
    result1 = _run_hook(_post_tool_use_payload(journal_path, cwd=tmp_path, original_file=HEAD_TEXT))
    assert result1.returncode == 0
    ctx1 = _parse_stdout_json(result1.stdout)["additionalContext"]
    assert "WITNESS ECHO" in ctx1
    assert "recorded RED" in ctx1

    clean_line = _delegated_line(ts=_fresh_ts(), task_id="t-002", notes="call #2: unrelated clean line")
    journal_path.write_text(after_call_1 + clean_line + "\n", encoding="utf-8")
    result2 = _run_hook(_post_tool_use_payload(journal_path, cwd=tmp_path, original_file=after_call_1))
    assert result2.returncode == 0
    assert result2.stdout == ""
    assert result2.stderr == ""


def test_e2e_existing_journal_echo_defect_and_witness_warn_together(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    _write_track(tmp_path, "sess-1", [
        _run_entry("2026-07-10T08:05:00.000000", "python -m pytest tools/ gateway/ -q", "red"),
    ])
    bad_line = _accepted_line(witness="python -m pytest tools/ gateway/ -q -> 3 failed",
                               category="")  # form defect: empty category
    journal_path.write_text(HEAD_TEXT + bad_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, cwd=tmp_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "JOURNAL ECHO: 1 defect(s)" in ctx
    assert "'category'" in ctx
    assert "WITNESS ECHO" in ctx
    assert "; WITNESS ECHO" in ctx
