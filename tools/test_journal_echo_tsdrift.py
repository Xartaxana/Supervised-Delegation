"""Tests for the TS DRIFT layer and the WITNESS ECHO staleness axis in
tools/journal_echo.py (ported from HQ, this batch).

Self-contained, by the same convention tools/test_witness_echo.py
already uses: this file does NOT import test_journal_echo.py (helpers
-- git repos, running the hook, journal lines, subagent transcripts,
the dod_track fixture -- are duplicated locally) so it can be read and
verified in isolation.

Covers the ts-drift battery literally:
 1. a fresh ts (0 drift) -- silent.
 2. future EXACTLY at the threshold (TS_FUTURE_TOLERANCE_SECONDS) --
    silent.
 3. future threshold+1s -- warns.
 4. stale EXACTLY at the threshold (TS_STALE_TOLERANCE_SECONDS) --
    silent.
 5. stale threshold+1s -- warns.
 6. a heavily old ts (hours) -- warns (STALE).
 7. an unparseable ts -- silent in the drift layer (fail-open), the
    existing form diagnostic ("not ISO format") is untouched/not
    duplicated.
 8. several batch lines sharing ONE ts -- per-event (each its own
    record, not deduplicated).
 9. non-journal edits -- the layer is not active (main()'s ordinary
    pass-through path).
Plus a minimal anchor smoke of pre-existing functionality (JOURNAL
ECHO/TIER ECHO/WITNESS ECHO/combine_context backward compatibility)
-- green after this additive port.

PAYLOAD-SCOPED ECHO BASE section: covers _extract_original_file/
_resolve_echo_base and the root regression this base exists to fix
(an OLDER uncommitted line outside this call's own payload must never
be re-evaluated for ts drift on a LATER, unrelated call) -- pure logic
plus e2e coverage of the primary/fallback paths, the Write path, the
non-tail-edit/no-op path, the fallback marker's visibility rule, and
the TS-DRIFT sibling of the tier/witness re-echo regression test (see
tools/test_journal_echo.py / tools/test_witness_echo.py for the
TIER/WITNESS versions of the same regression).

WITNESS ECHO STALENESS section: covers _load_witness_edits/
_last_edit_ts/_last_green_ts/_detect_staleness -- pure logic plus e2e
coverage of the doc-only exemption (the very Edit/Write call writing
an accepted line into routing-log.jsonl must not stale itself) and the
independence of the staleness axis from the command-match axis (both
can fire on the same line).

Run from the repo root: python -m pytest tools/test_journal_echo_tsdrift.py -q
"""

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import journal_echo as je  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "journal_echo.py"


# =======================================================================
# helpers -- a fixed clock for pure _detect_ts_drift tests
# =======================================================================

NOW = dt.datetime(2026, 7, 22, 12, 0, 0)


def _iso(delta_seconds: float) -> str:
    return (NOW + dt.timedelta(seconds=delta_seconds)).isoformat()


# =======================================================================
# helpers -- journal lines (by the example of test_journal_echo._line)
# =======================================================================


def _line(ts, event="delegated", agent="builder", category="implementation",
          notes="note", worker_ref="cli:2026-07-10T08:00:00", **kw) -> str:
    obj = {"ts": ts, "event": event, "agent": agent, "category": category,
           "notes": notes, "worker_ref": worker_ref}
    obj.update(kw)
    return json.dumps(obj, ensure_ascii=False)


HEAD_LINE = _line(ts="2026-07-10T08:00:00", task_id="t-001", model="sonnet")
HEAD_TEXT = HEAD_LINE + "\n"


# =======================================================================
# helpers -- real git repos (by the example of test_journal_validator/test_journal_echo)
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
# helpers -- running the hook
# =======================================================================


_NO_ORIGINAL_FILE = object()  # sentinel -- omit tool_response.originalFile
# entirely (exercises the FALLBACK path of je._resolve_echo_base --
# identical to the pre-payload-scoping HEAD-diff computation). Default
# preserves every pre-existing call site's payload shape byte-for-byte.


def _post_tool_use_payload(file_path, cwd=".", session_id="sess-1", tool_name="Edit",
                            original_file=_NO_ORIGINAL_FILE) -> dict:
    tool_response = {"filePath": str(file_path), "success": True}
    if original_file is not _NO_ORIGINAL_FILE:
        # tool_response.originalFile (Edit/Write Zod schemas -- see
        # journal_echo.py's "PAYLOAD-SCOPED ECHO BASE" section).
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


def _run_hook(payload, timeout=10, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env=env,
    )


def _parse_stdout_json(stdout: str) -> dict:
    payload = json.loads(stdout)
    hook_output = payload["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PostToolUse"
    return hook_output


# =======================================================================
# _detect_ts_drift -- pure logic, both threshold boundaries (rule 6a)
# =======================================================================


def test_detect_ts_drift_fresh_zero_delta_silent():
    assert je._detect_ts_drift(_iso(0), NOW) is None


def test_detect_ts_drift_small_jitter_silent():
    assert je._detect_ts_drift(_iso(5), NOW) is None
    assert je._detect_ts_drift(_iso(-5), NOW) is None


def test_detect_ts_drift_future_exactly_threshold_boundary_silent():
    assert je._detect_ts_drift(_iso(je.TS_FUTURE_TOLERANCE_SECONDS), NOW) is None


def test_detect_ts_drift_future_threshold_plus_one_warns():
    result = je._detect_ts_drift(_iso(je.TS_FUTURE_TOLERANCE_SECONDS + 1), NOW)
    assert result is not None
    kind, delta = result
    assert kind == "future"
    assert delta == pytest.approx(je.TS_FUTURE_TOLERANCE_SECONDS + 1)


def test_detect_ts_drift_stale_exactly_threshold_boundary_silent():
    assert je._detect_ts_drift(_iso(-je.TS_STALE_TOLERANCE_SECONDS), NOW) is None


def test_detect_ts_drift_stale_threshold_plus_one_warns():
    result = je._detect_ts_drift(_iso(-(je.TS_STALE_TOLERANCE_SECONDS + 1)), NOW)
    assert result is not None
    kind, delta = result
    assert kind == "stale"
    # delta is the POSITIVE magnitude of the lag, not the "raw"
    # (parsed-now) difference -- see _detect_ts_drift: stale_delta =
    # -delta, the same convention _format_ts_drift_line expects
    # (rounds abs()).
    assert delta == pytest.approx(je.TS_STALE_TOLERANCE_SECONDS + 1)


def test_detect_ts_drift_hours_old_warns_stale():
    # This stays valid under the payload-scoped semantics below: it
    # checks the PURE ts-vs-now comparison function (_detect_ts_drift),
    # which knows NOTHING about new_lines/base selection and does not
    # participate in the root-cause class the payload-scoped base
    # exists to fix (growing staleness of an ALREADY-checked old line on
    # repeated hook calls -- that is a line-SELECTION bug, not a bug in
    # comparing one specific ts against one specific now). The value
    # here models a DIFFERENT, orthogonal and still-legitimate case: a
    # ts declared "5 hours ago" AT THE MOMENT a NEW line is written --
    # itself an F-29 violation (ts must be read from the clock
    # immediately before writing), not growing staleness of an
    # already-checked line -- and must warn regardless of which base
    # version is in play. See test_echo_tsdrift_hours_old_warns_stale
    # below for the same note at the e2e level, and the "PAYLOAD-SCOPED
    # ECHO BASE" section further down for the test that catches the
    # actual root-cause class (growing staleness).
    result = je._detect_ts_drift(_iso(-3600 * 5), NOW)
    assert result is not None
    assert result[0] == "stale"


def test_detect_ts_drift_unparsable_string_returns_none():
    assert je._detect_ts_drift("not-a-timestamp", NOW) is None
    assert je._detect_ts_drift("2026-13-99T99:99:99", NOW) is None


def test_detect_ts_drift_non_string_returns_none():
    assert je._detect_ts_drift(None, NOW) is None
    assert je._detect_ts_drift(42, NOW) is None


def test_detect_ts_drift_empty_string_returns_none():
    assert je._detect_ts_drift("", NOW) is None


# =======================================================================
# _collect_ts_drift_events -- pure logic
# =======================================================================


def test_collect_ts_drift_events_empty_new_lines():
    assert je._collect_ts_drift_events([], [], NOW) == []


def test_collect_ts_drift_events_clean_line_silent():
    line = _line(ts=_iso(0), task_id="t-002", model="sonnet")
    assert je._collect_ts_drift_events([line], [], NOW) == []


def test_collect_ts_drift_events_future_line_reported():
    future_ts = _iso(je.TS_FUTURE_TOLERANCE_SECONDS + 10)
    line = _line(ts=future_ts, task_id="t-002", model="sonnet")
    events = je._collect_ts_drift_events([line], [], NOW)
    assert len(events) == 1
    line_no, kind, delta = events[0]
    assert (line_no, kind) == (1, "future")


def test_collect_ts_drift_events_batch_same_ts_reported_per_event():
    # Several lines sharing the SAME ts -- each its own record, not
    # collapsed into one.
    future_ts = _iso(je.TS_FUTURE_TOLERANCE_SECONDS + 10)
    lines = [
        _line(ts=future_ts, task_id="t-002", model="sonnet"),
        _line(ts=future_ts, task_id="t-003", model="sonnet"),
        _line(ts=future_ts, task_id="t-004", model="sonnet"),
    ]
    events = je._collect_ts_drift_events(lines, [], NOW)
    assert len(events) == 3
    assert [e[0] for e in events] == [1, 2, 3]
    assert all(e[1] == "future" for e in events)


def test_collect_ts_drift_events_malformed_json_line_skipped_not_raised():
    assert je._collect_ts_drift_events(["{not valid json"], [], NOW) == []


def test_collect_ts_drift_events_not_a_dict_line_skipped():
    assert je._collect_ts_drift_events(["[1, 2, 3]"], [], NOW) == []


def test_collect_ts_drift_events_line_numbering_accounts_for_head_lines():
    future_ts = _iso(je.TS_FUTURE_TOLERANCE_SECONDS + 10)
    head_lines = ["dummy head 1", "dummy head 2"]
    line = _line(ts=future_ts, task_id="t-002", model="sonnet")
    events = je._collect_ts_drift_events([line], head_lines, NOW)
    assert events[0][0] == 3  # len(head_lines) + idx(0) + 1


def test_collect_ts_drift_events_missing_ts_field_skipped():
    obj = json.loads(_line(ts=_iso(0), task_id="t-002", model="sonnet"))
    del obj["ts"]
    assert je._collect_ts_drift_events([json.dumps(obj)], [], NOW) == []


# =======================================================================
# _format_ts_drift_line -- pure logic, literal FUTURE format
# =======================================================================


def test_format_ts_drift_line_future_exact_literal():
    line = je._format_ts_drift_line((2, "future", 125.0))
    assert line == (
        "TS DRIFT: line 2 event ts is 125s in the FUTURE "
        "(F-29: ts must be read from the system clock immediately before writing)"
    )


def test_format_ts_drift_line_stale_contains_marker_and_seconds():
    line = je._format_ts_drift_line((3, "stale", 1801.0))
    assert "TS DRIFT: line 3 event ts is 1801s STALE" in line
    assert line.isascii()


def test_format_ts_drift_line_rounds_fractional_seconds():
    line = je._format_ts_drift_line((1, "future", 125.6))
    assert "126s" in line


def test_format_ts_drift_line_is_ascii_always():
    assert je._format_ts_drift_line((1, "future", 999.0)).isascii()
    assert je._format_ts_drift_line((1, "stale", 9999.0)).isascii()


# =======================================================================
# build_ts_drift_segment -- pure logic
# =======================================================================


def test_build_ts_drift_segment_empty_list():
    assert je.build_ts_drift_segment([]) == ""


def test_build_ts_drift_segment_single_event():
    ev = (2, "future", 125.0)
    seg = je.build_ts_drift_segment([ev])
    assert seg == je._format_ts_drift_line(ev)


def test_build_ts_drift_segment_joins_multiple_with_semicolon():
    events = [(2, "future", 125.0), (3, "stale", 1801.0)]
    seg = je.build_ts_drift_segment(events)
    assert seg.count("TS DRIFT") == 2
    assert "; " in seg


# ---------------------------------------------------------------------
# build_ts_drift_segment -- MAX_TS_DRIFT_LINES boundary (rule 6a)
# ---------------------------------------------------------------------


def test_build_ts_drift_segment_exactly_five_boundary_no_more_suffix():
    events = [(i, "future", 200.0) for i in range(1, je.MAX_TS_DRIFT_LINES + 1)]
    seg = je.build_ts_drift_segment(events)
    assert seg.count("TS DRIFT") == je.MAX_TS_DRIFT_LINES
    assert "more" not in seg


def test_build_ts_drift_segment_beyond_boundary_six_adds_one_more():
    events = [(i, "future", 200.0) for i in range(1, je.MAX_TS_DRIFT_LINES + 2)]
    seg = je.build_ts_drift_segment(events)
    assert seg.count("TS DRIFT") == je.MAX_TS_DRIFT_LINES
    assert seg.endswith("; +1 more")


def test_build_ts_drift_segment_far_beyond_boundary_counts_correctly():
    events = [(i, "future", 200.0) for i in range(1, je.MAX_TS_DRIFT_LINES + 6)]
    seg = je.build_ts_drift_segment(events)
    assert seg.count("TS DRIFT") == je.MAX_TS_DRIFT_LINES
    assert seg.endswith("; +5 more")


# =======================================================================
# combine_context -- 2-/3-arg backward compatibility + the new segment
# =======================================================================


def test_combine_context_two_arg_form_unaffected():
    violations = ["line 2: msg one"]
    assert je.combine_context(violations, []) == je.build_context(violations)


def test_combine_context_three_arg_witness_form_unaffected():
    violations = ["v"]
    ctx = je.combine_context(violations, [], [])
    assert ctx == je.build_context(violations)


def test_combine_context_ts_drift_only_segment():
    ev = (2, "future", 125.0)
    ctx = je.combine_context([], [], None, [ev])
    assert ctx == je.build_ts_drift_segment([ev])
    assert "JOURNAL ECHO" not in ctx


def test_combine_context_all_four_segments_joined_in_order():
    violations = ["v"]
    tier_ev = (2, "mismatch", "fable", {"claude-opus-4-8": 1})
    ts_ev = (3, "future", 125.0)
    ctx = je.combine_context(violations, [tier_ev], [], [ts_ev])
    assert ctx == (
        je.build_context(violations) + "; " + je.build_tier_segment([tier_ev])
        + "; " + je.build_ts_drift_segment([ts_ev])
    )


def test_combine_context_all_empty_yields_empty_string():
    assert je.combine_context([], [], None, None) == ""


# =======================================================================
# main() end-to-end -- subprocess smoke, DoD 1-9
# =======================================================================


def test_echo_tsdrift_fresh_ts_silent(tmp_path):
    # DoD 1: a fresh ts (0 drift) -- silent.
    journal_path = _seed_committed_journal(tmp_path)
    fresh_ts = dt.datetime.now().isoformat()
    new_line = _line(ts=fresh_ts, task_id="t-002", model="sonnet", notes="fresh")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_tsdrift_future_beyond_threshold_warns(tmp_path):
    # DoD 3 (e2e path, generous margin -- the exact boundary is already
    # proven by the pure test test_detect_ts_drift_future_threshold_plus_one_warns;
    # here we use the real process clock, with margin against subprocess
    # jitter).
    journal_path = _seed_committed_journal(tmp_path)
    future_ts = (dt.datetime.now() + dt.timedelta(seconds=je.TS_FUTURE_TOLERANCE_SECONDS + 60)).isoformat()
    new_line = _line(ts=future_ts, task_id="t-002", model="sonnet", notes="future drift")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "TS DRIFT" in ctx
    assert "FUTURE" in ctx
    assert "F-29" in ctx


def test_echo_tsdrift_stale_beyond_threshold_warns(tmp_path):
    # DoD 5 (e2e path, generous margin).
    journal_path = _seed_committed_journal(tmp_path)
    stale_ts = (dt.datetime.now() - dt.timedelta(seconds=je.TS_STALE_TOLERANCE_SECONDS + 60)).isoformat()
    new_line = _line(ts=stale_ts, task_id="t-002", model="sonnet", notes="stale drift")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "TS DRIFT" in ctx
    assert "STALE" in ctx
    assert "D-0079" in ctx


def test_echo_tsdrift_hours_old_warns_stale(tmp_path):
    # DoD 6. This scenario also stays valid under the payload-scoped
    # semantics: ONE line is added IN THIS SAME call (single-call
    # append, no originalFile -> falls back to HEAD-diff, which here
    # COINCIDES with the payload-scoped base: both agree "new" is
    # exactly this one line). Its ts is "5 hours ago" AT THE MOMENT OF
    # WRITING -- a legitimate F-29 warning regardless of which base
    # version is active (see the comment on
    # test_detect_ts_drift_hours_old_warns_stale above for how this
    # differs from the t-277-class bug: GROWING staleness of an
    # ALREADY-checked line on a LATER, different call -- see the
    # regression test in the "PAYLOAD-SCOPED ECHO BASE" section below).
    journal_path = _seed_committed_journal(tmp_path)
    stale_ts = (dt.datetime.now() - dt.timedelta(hours=5)).isoformat()
    new_line = _line(ts=stale_ts, task_id="t-002", model="sonnet", notes="hours old")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    assert "STALE" in hook_output["additionalContext"]


def test_echo_tsdrift_unparsable_ts_silent_in_drift_layer_existing_diagnostic_intact(tmp_path):
    # DoD 7: an unparseable ts -- silent in the drift layer (fail-open),
    # does NOT duplicate the diagnostic -- the existing JOURNAL ECHO
    # complaint ("not ISO format") stays the ONLY source of signal for
    # this field.
    journal_path = _seed_committed_journal(tmp_path)
    new_line = _line(ts="not-a-timestamp", task_id="t-002", model="sonnet", notes="bad ts")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "JOURNAL ECHO" in ctx
    assert "not ISO format" in ctx
    assert "TS DRIFT" not in ctx


def test_echo_tsdrift_batch_two_lines_same_future_ts_per_event(tmp_path):
    # DoD 8: several batch lines sharing ONE ts -- per-event, two
    # separate TS DRIFT records, not one collapsed record.
    journal_path = _seed_committed_journal(tmp_path)
    future_ts = (dt.datetime.now() + dt.timedelta(seconds=je.TS_FUTURE_TOLERANCE_SECONDS + 60)).isoformat()
    lines = [
        _line(ts=future_ts, task_id="t-002", model="sonnet", notes="batch one"),
        _line(ts=future_ts, task_id="t-003", model="sonnet", notes="batch two"),
    ]
    journal_path.write_text(HEAD_TEXT + "".join(l + "\n" for l in lines), encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert ctx.count("TS DRIFT") == 2


def _standalone_batch(tmp_path, n, future_ts):
    # head_text=None (git NEVER init'ed) -> ALL n lines on disk are
    # "new" (see MAX_TS_DRIFT_LINES's own docstring: this exact scenario
    # is why the ceiling exists). Each is a valid delegated line with a
    # sequential task_id (the validator requires max+1) and its own
    # worker_ref.
    lines = [
        _line(ts=future_ts, task_id=f"t-{i + 1:03d}", model="sonnet",
              worker_ref=f"cli:seed-{i}", notes=f"standalone drift #{i}")
        for i in range(n)
    ]
    journal_path = tmp_path / "logs" / "routing-log.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    return journal_path


def test_echo_tsdrift_standalone_exactly_max_lines_no_more_suffix(tmp_path):
    # Boundary (rule 6a): EXACTLY MAX_TS_DRIFT_LINES lines -- no
    # "+more", e2e path (standalone mode, head_text=None -- see the
    # ceiling's motivation in MAX_TS_DRIFT_LINES's own docstring).
    future_ts = (dt.datetime.now() + dt.timedelta(seconds=je.TS_FUTURE_TOLERANCE_SECONDS + 60)).isoformat()
    journal_path = _standalone_batch(tmp_path, je.MAX_TS_DRIFT_LINES, future_ts)
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert ctx.count("TS DRIFT") == je.MAX_TS_DRIFT_LINES
    assert "more" not in ctx


def test_echo_tsdrift_standalone_beyond_max_lines_adds_more_suffix(tmp_path):
    # Boundary+1: MAX_TS_DRIFT_LINES+1 lines -- "+1 more".
    future_ts = (dt.datetime.now() + dt.timedelta(seconds=je.TS_FUTURE_TOLERANCE_SECONDS + 60)).isoformat()
    journal_path = _standalone_batch(tmp_path, je.MAX_TS_DRIFT_LINES + 1, future_ts)
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert ctx.count("TS DRIFT") == je.MAX_TS_DRIFT_LINES
    assert "+1 more" in ctx


def test_echo_tsdrift_non_journal_path_silent(tmp_path):
    # DoD 9: a non-journal edit -- the layer is not active (main()'s
    # pass-through path exits at _is_journal_path before ts-drift is
    # even computed).
    other_file = tmp_path / "not-a-journal.txt"
    other_file.write_text('{"ts": "not-a-timestamp"}', encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(other_file))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_tsdrift_defect_and_drift_together_one_context(tmp_path):
    # A form defect (empty category) + TS DRIFT together -- both
    # segments in one additionalContext, joined by "; ".
    journal_path = _seed_committed_journal(tmp_path)
    future_ts = (dt.datetime.now() + dt.timedelta(seconds=je.TS_FUTURE_TOLERANCE_SECONDS + 60)).isoformat()
    bad_line = _line(ts=future_ts, task_id="t-002", model="sonnet", category="", notes="defect+drift")
    journal_path.write_text(HEAD_TEXT + bad_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "JOURNAL ECHO: 1 defect(s)" in ctx
    assert "TS DRIFT" in ctx
    assert "; TS DRIFT" in ctx


def test_echo_tsdrift_ascii_output_stdout(tmp_path):
    # ASCII output by this file's own convention: the stdout wire bytes
    # stay pure ASCII (json.dumps ensure_ascii=True); TS DRIFT itself
    # never carries non-ASCII dynamic content (only integers).
    journal_path = _seed_committed_journal(tmp_path)
    future_ts = (dt.datetime.now() + dt.timedelta(seconds=je.TS_FUTURE_TOLERANCE_SECONDS + 60)).isoformat()
    new_line = _line(ts=future_ts, task_id="t-002", model="sonnet", notes="ascii check")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.stdout.isascii()


# =======================================================================
# smoke: minimal anchor coverage of pre-existing functionality (JOURNAL
# ECHO / TIER ECHO / WITNESS ECHO / combine_context) -- green after this
# additive ts-drift port. Helpers for TIER/WITNESS -- see the module
# docstring for the self-containment rationale.
# =======================================================================


def test_smoke_non_journal_path_silent(tmp_path):
    other_file = tmp_path / "not-a-journal.txt"
    other_file.write_text("irrelevant content", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(other_file))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_smoke_clean_new_line_silent(tmp_path):
    # IMPORTANT (a real finding, not a guess): the fixed historical ts
    # fixture ("2026-07-10T08:10:00", as elsewhere in this file) is NOW
    # (after this port) legitimately caught as TS DRIFT STALE -- the
    # machine's real clock has long since moved past this date. This is
    # NOT a defect of the new layer (it is SUPPOSED to catch an old ts),
    # it is the expected consequence: this smoke must use a FRESH ts
    # (the current clock) to test EXACTLY "pre-existing functionality is
    # not broken", without crossing into the new layer.
    journal_path = _seed_committed_journal(tmp_path)
    fresh_ts = dt.datetime.now().isoformat()
    new_line = _line(ts=fresh_ts, task_id="t-002", model="sonnet", notes="clean")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_smoke_missing_category_defect_reported(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    fresh_ts = dt.datetime.now().isoformat()
    bad_line = _line(ts=fresh_ts, task_id="t-002", model="sonnet", category="")
    journal_path.write_text(HEAD_TEXT + bad_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "JOURNAL ECHO: 1 defect(s) in new lines:" in ctx
    assert "'category'" in ctx


def _assistant_line(model):
    return {"type": "assistant", "message": {"model": model}}


def _write_agent_transcript(home: Path, agent_id: str, lines,
                            proj="proj-slug", sess="sess-id") -> Path:
    path = home / ".claude" / "projects" / proj / sess / "subagents" / f"agent-{agent_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) if not isinstance(line, str) else line for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


def _env_with_home(home: Path) -> dict:
    env = dict(os.environ)
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    return env


def test_smoke_tier_echo_mismatch_still_works(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    home = tmp_path / "home"
    _write_agent_transcript(home, "fbl001", [_assistant_line("claude-opus-4-8")])
    fresh_ts = dt.datetime.now().isoformat()
    new_line = _line(ts=fresh_ts, task_id="t-002", model="fable",
                      worker_ref="agent:fbl001", notes="mismatch case")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path), env=_env_with_home(home))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "TIER ECHO" in ctx
    assert "MISMATCH" in ctx


def _write_track(root: Path, session_id: str, runs: list, edits: list = None) -> Path:
    track_dir = root / ".claude" / "dod_track"
    track_dir.mkdir(parents=True, exist_ok=True)
    path = track_dir / f"{session_id}.json"
    path.write_text(json.dumps({"edits": edits or [], "runs": runs}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    return path


def test_smoke_witness_echo_red_warn_still_works(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    _write_track(tmp_path, "sess-1", [
        {"ts": "2026-07-10T08:05:00.000000", "tool_name": "Bash",
         "command": "python -m pytest tools/ -q", "outcome": "red", "agent_id": None},
    ])
    fresh_ts = dt.datetime.now().isoformat()
    new_line = _line(ts=fresh_ts, event="accepted", agent="builder",
                      task_id="t-001", by="opus", model="sonnet",
                      witness="ran: python -m pytest tools/ -q", notes="accepted with red witness")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, cwd=str(tmp_path), session_id="sess-1"))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "WITNESS ECHO" in ctx
    assert "contradiction" in ctx


def test_smoke_combine_context_backward_compat_two_arg():
    assert je.combine_context(["v"], []) == je.build_context(["v"])


# =======================================================================
# PAYLOAD-SCOPED ECHO BASE -- _extract_original_file, pure
# =======================================================================


def test_extract_original_file_non_edit_write_tool_unavailable():
    payload = {"tool_response": {"originalFile": "x"}}
    assert je._extract_original_file(payload, "Bash") is je._ORIGINAL_FILE_UNAVAILABLE
    assert je._extract_original_file(payload, "MultiEdit") is je._ORIGINAL_FILE_UNAVAILABLE
    assert je._extract_original_file(payload, None) is je._ORIGINAL_FILE_UNAVAILABLE


def test_extract_original_file_tool_response_not_dict_unavailable():
    payload = {"tool_response": "not-a-dict"}
    assert je._extract_original_file(payload, "Edit") is je._ORIGINAL_FILE_UNAVAILABLE


def test_extract_original_file_tool_response_missing_unavailable():
    assert je._extract_original_file({}, "Edit") is je._ORIGINAL_FILE_UNAVAILABLE


def test_extract_original_file_key_absent_unavailable():
    payload = {"tool_response": {"filePath": "x"}}
    assert je._extract_original_file(payload, "Write") is je._ORIGINAL_FILE_UNAVAILABLE


def test_extract_original_file_none_means_new_file_empty_string():
    payload = {"tool_response": {"originalFile": None}}
    assert je._extract_original_file(payload, "Write") == ""
    assert je._extract_original_file(payload, "Edit") == ""


def test_extract_original_file_wrong_type_unavailable():
    payload = {"tool_response": {"originalFile": 42}}
    assert je._extract_original_file(payload, "Edit") is je._ORIGINAL_FILE_UNAVAILABLE


def test_extract_original_file_valid_string_returned():
    payload = {"tool_response": {"originalFile": "line1\nline2\n"}}
    assert je._extract_original_file(payload, "Edit") == "line1\nline2\n"
    assert je._extract_original_file(payload, "Write") == "line1\nline2\n"


# =======================================================================
# PAYLOAD-SCOPED ECHO BASE -- _resolve_echo_base, pure
# =======================================================================


def test_resolve_echo_base_primary_path_tail_append():
    head_lines = ["h1"]
    staged_lines = ["h1", "a1", "b1"]
    payload = {"tool_response": {"originalFile": "h1\na1\n"}}
    base, new, fallback = je._resolve_echo_base(payload, "Edit", staged_lines, head_lines)
    assert fallback is False
    assert base == ["h1", "a1"]
    assert new == ["b1"]


def test_resolve_echo_base_falls_back_when_unavailable():
    head_lines = ["h1"]
    staged_lines = ["h1", "a1"]
    payload = {"tool_response": {}}  # no originalFile key at all
    base, new, fallback = je._resolve_echo_base(payload, "Edit", staged_lines, head_lines)
    assert fallback is True
    assert base == head_lines
    assert new == ["a1"]


def test_resolve_echo_base_falls_back_on_non_tail_edit():
    head_lines = ["h1"]
    staged_lines = ["h1", "a1", "b1"]
    # originalFile claims a DIFFERENT prior state -- disk doesn't extend
    # it as a prefix (a non-tail edit).
    payload = {"tool_response": {"originalFile": "different\n"}}
    base, new, fallback = je._resolve_echo_base(payload, "Edit", staged_lines, head_lines)
    assert fallback is True
    assert base == head_lines
    assert new == staged_lines[len(head_lines):]


def test_resolve_echo_base_no_op_edit_yields_empty_new_lines():
    head_lines = ["h1"]
    staged_lines = ["h1", "a1"]
    payload = {"tool_response": {"originalFile": "h1\na1\n"}}  # identical to disk -- nothing added
    base, new, fallback = je._resolve_echo_base(payload, "Edit", staged_lines, head_lines)
    assert fallback is False
    assert new == []


def test_resolve_echo_base_write_new_file_none_original():
    staged_lines = ["a1", "a2"]
    payload = {"tool_response": {"originalFile": None}}
    base, new, fallback = je._resolve_echo_base(payload, "Write", staged_lines, [])
    assert fallback is False
    assert base == []
    assert new == ["a1", "a2"]


def test_resolve_echo_base_fallback_when_head_diff_also_non_append_only():
    # Both bases fail -> the fallback branch itself yields [] (append_ok
    # False against head_lines too) -- matches this file's pre-existing
    # behavior for the old HEAD-diff append-only-violation case.
    head_lines = ["h1", "h2"]
    staged_lines = ["DIFFERENT"]
    payload = {"tool_response": {}}
    base, new, fallback = je._resolve_echo_base(payload, "Edit", staged_lines, head_lines)
    assert fallback is True
    assert new == []


# =======================================================================
# PAYLOAD-SCOPED ECHO BASE -- e2e, root regression
# =======================================================================


def test_echo_payload_scoped_earlier_uncommitted_line_outside_scope_silent(tmp_path):
    # Root-cause regression: line A was added EARLIER (not this call),
    # its ts is old enough to genuinely be STALE by the wall clock --
    # but it is NOT part of THIS call's payload (this call's
    # originalFile already includes it) -> zero ts-drift events, even
    # though the old HEAD-diff logic would have re-evaluated it too
    # (that IS the bug this base fixes).
    journal_path = _seed_committed_journal(tmp_path)
    stale_ts = (dt.datetime.now() - dt.timedelta(seconds=je.TS_STALE_TOLERANCE_SECONDS + 600)).isoformat()
    line_a = _line(ts=stale_ts, task_id="t-002", model="sonnet",
                   notes="A: written earlier, now stale by wall clock")
    after_call_a = HEAD_TEXT + line_a + "\n"
    fresh_ts = dt.datetime.now().isoformat()
    line_b = _line(ts=fresh_ts, task_id="t-003", model="sonnet", worker_ref="cli:call-b",
                   notes="B: this call's own new line, fresh")
    journal_path.write_text(after_call_a + line_b + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, original_file=after_call_a))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_payload_scoped_only_current_call_line_flagged_not_earlier_stale(tmp_path):
    # Surgical variant: BOTH A and B are stale by wall clock -- only B
    # (this call's own line) may be reported; A must not reappear.
    journal_path = _seed_committed_journal(tmp_path)
    stale_ts_a = (dt.datetime.now() - dt.timedelta(seconds=je.TS_STALE_TOLERANCE_SECONDS + 600)).isoformat()
    line_a = _line(ts=stale_ts_a, task_id="t-002", model="sonnet", notes="A: earlier call, stale")
    after_call_a = HEAD_TEXT + line_a + "\n"
    stale_ts_b = (dt.datetime.now() - dt.timedelta(seconds=je.TS_STALE_TOLERANCE_SECONDS + 60)).isoformat()
    line_b = _line(ts=stale_ts_b, task_id="t-003", model="sonnet", worker_ref="cli:call-b",
                   notes="B: this call's own new line, also stale")
    journal_path.write_text(after_call_a + line_b + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, original_file=after_call_a))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert ctx.count("TS DRIFT") == 1
    assert "line 3" in ctx  # HEAD=1 line, A=line 2 (out of scope), B=line 3 (in scope)


# =======================================================================
# PAYLOAD-SCOPED ECHO BASE -- e2e, TS_STALE_TOLERANCE boundary via the
# PRIMARY (not fallback) path. Exact-boundary precision is already
# proven by the pure _detect_ts_drift tests above (fixed NOW, no
# subprocess jitter) -- these e2e tests only prove the primary path
# reaches the same detector, with a generous margin against subprocess
# start-up jitter, the same style the rest of this file already uses.
# =======================================================================


def test_echo_payload_scoped_fresh_ts_silent(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    fresh_ts = dt.datetime.now().isoformat()
    new_line = _line(ts=fresh_ts, task_id="t-002", model="sonnet", notes="fresh, payload-scoped path")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, original_file=HEAD_TEXT))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_payload_scoped_stale_beyond_threshold_warns(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    stale_ts = (dt.datetime.now() - dt.timedelta(seconds=je.TS_STALE_TOLERANCE_SECONDS + 60)).isoformat()
    new_line = _line(ts=stale_ts, task_id="t-002", model="sonnet", notes="stale, payload-scoped path")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, original_file=HEAD_TEXT))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "TS DRIFT" in ctx
    assert "STALE" in ctx


# =======================================================================
# PAYLOAD-SCOPED ECHO BASE -- e2e, a batch of N lines in ONE call,
# PRIMARY path, per-event
# =======================================================================


def test_echo_payload_scoped_batch_lines_one_call_per_event(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    future_ts = (dt.datetime.now() + dt.timedelta(seconds=je.TS_FUTURE_TOLERANCE_SECONDS + 60)).isoformat()
    lines = [
        _line(ts=future_ts, task_id=f"t-{i:03d}", model="sonnet", worker_ref=f"cli:batch-{i}",
              notes=f"batch line #{i}")
        for i in (2, 3, 4)
    ]
    journal_path.write_text(HEAD_TEXT + "".join(l + "\n" for l in lines), encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, original_file=HEAD_TEXT))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert ctx.count("TS DRIFT") == 3


# =======================================================================
# PAYLOAD-SCOPED ECHO BASE -- e2e, Write path
# =======================================================================


def test_echo_write_new_file_originalfile_none_correct_scoping(tmp_path):
    # Write creates a NEW file -- originalFile=None per Write's own Zod
    # schema (see journal_echo.py) -- the whole content string becomes
    # "this call's own"; it is clean -> silent.
    (tmp_path / "logs").mkdir(parents=True)
    journal_path = tmp_path / "logs" / "routing-log.jsonl"
    fresh_ts = dt.datetime.now().isoformat()
    line = _line(ts=fresh_ts, task_id="t-001", model="sonnet", notes="brand new journal via Write")
    journal_path.write_text(line + "\n", encoding="utf-8")
    payload = _post_tool_use_payload(journal_path, tool_name="Write", original_file=None)
    result = _run_hook(payload)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_write_new_file_stale_line_flagged(tmp_path):
    (tmp_path / "logs").mkdir(parents=True)
    journal_path = tmp_path / "logs" / "routing-log.jsonl"
    stale_ts = (dt.datetime.now() - dt.timedelta(seconds=je.TS_STALE_TOLERANCE_SECONDS + 60)).isoformat()
    line = _line(ts=stale_ts, task_id="t-001", model="sonnet", notes="brand new journal, stale first line")
    journal_path.write_text(line + "\n", encoding="utf-8")
    payload = _post_tool_use_payload(journal_path, tool_name="Write", original_file=None)
    result = _run_hook(payload)
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "TS DRIFT" in ctx
    assert "STALE" in ctx


def test_echo_write_update_existing_file_correct_scoping(tmp_path):
    # Write OVERWRITES an existing file entirely -- originalFile = the
    # previous full content; only the appended tail falls in scope, old
    # lines (already on disk BEFORE this specific call) do not, even if
    # they are themselves old by the clock.
    journal_path = _seed_committed_journal(tmp_path)
    stale_ts_prior = (dt.datetime.now() - dt.timedelta(seconds=je.TS_STALE_TOLERANCE_SECONDS + 600)).isoformat()
    prior_extra = _line(ts=stale_ts_prior, task_id="t-002", model="sonnet", notes="prior extra line, stale")
    prior_full = HEAD_TEXT + prior_extra + "\n"
    fresh_ts = dt.datetime.now().isoformat()
    new_line = _line(ts=fresh_ts, task_id="t-003", model="sonnet", worker_ref="cli:write-update",
                      notes="freshly written via Write, appended to prior_full")
    journal_path.write_text(prior_full + new_line + "\n", encoding="utf-8")
    payload = _post_tool_use_payload(journal_path, tool_name="Write", original_file=prior_full)
    result = _run_hook(payload)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_write_missing_originalfile_key_falls_back_with_marker(tmp_path):
    # Write's tool_response WITHOUT originalFile at all -- falls back to
    # HEAD-diff + a visible marker (checked together with a real defect
    # so the marker is actually visible -- see the fallback-marker
    # section below for the dedicated "clean call stays silent" test).
    journal_path = _seed_committed_journal(tmp_path)
    stale_ts = (dt.datetime.now() - dt.timedelta(seconds=je.TS_STALE_TOLERANCE_SECONDS + 60)).isoformat()
    new_line = _line(ts=stale_ts, task_id="t-002", model="sonnet", notes="stale, Write without originalFile")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    payload = _post_tool_use_payload(journal_path, tool_name="Write")  # no original_file kwarg -> key absent
    result = _run_hook(payload)
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "TS DRIFT" in ctx
    assert je.FALLBACK_MARKER_TEXT in ctx


# =======================================================================
# PAYLOAD-SCOPED ECHO BASE -- e2e, non-tail edit / no-op
# =======================================================================


def test_echo_non_tail_edit_falls_back_silently_when_no_actual_drift(tmp_path):
    # originalFile is present but is NOT a prefix of the current disk
    # (a non-tail edit) -- falls back to HEAD-diff; in this scenario
    # both bases agree the one new line is clean -- zero events.
    journal_path = _seed_committed_journal(tmp_path)
    fresh_ts = dt.datetime.now().isoformat()
    new_line = _line(ts=fresh_ts, task_id="t-002", model="sonnet", notes="clean new line")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    payload = _post_tool_use_payload(journal_path, original_file="{totally unrelated content}\n")
    result = _run_hook(payload)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_no_op_edit_identical_original_file_zero_events(tmp_path):
    # "no-op": originalFile == the current disk literally -- nothing
    # added by this call -> zero new lines, zero events.
    journal_path = _seed_committed_journal(tmp_path)
    fresh_ts = dt.datetime.now().isoformat()
    new_line = _line(ts=fresh_ts, task_id="t-002", model="sonnet", notes="already-committed-equivalent state")
    full_text = HEAD_TEXT + new_line + "\n"
    journal_path.write_text(full_text, encoding="utf-8")
    payload = _post_tool_use_payload(journal_path, original_file=full_text)
    result = _run_hook(payload)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


# =======================================================================
# PAYLOAD-SCOPED ECHO BASE -- e2e, fallback-marker visibility (own
# engineering completion of "so degradation is visible, not silent")
# =======================================================================


def test_echo_fallback_marker_appears_alongside_other_output(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    stale_ts = (dt.datetime.now() - dt.timedelta(seconds=je.TS_STALE_TOLERANCE_SECONDS + 60)).isoformat()
    bad_line = _line(ts=stale_ts, task_id="t-002", model="sonnet", category="",
                      notes="defect + stale, fallback path")
    journal_path.write_text(HEAD_TEXT + bad_line + "\n", encoding="utf-8")
    payload = _post_tool_use_payload(journal_path)  # no original_file -> fallback engaged
    result = _run_hook(payload)
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "JOURNAL ECHO" in ctx
    assert "TS DRIFT" in ctx
    assert je.FALLBACK_MARKER_TEXT in ctx
    assert ("; " + je.FALLBACK_MARKER_TEXT) in ctx  # joined as the trailing segment


def test_echo_fallback_marker_not_shown_on_otherwise_clean_call(tmp_path):
    # Own engineering decision (see journal_echo.py, "PAYLOAD-SCOPED
    # ECHO BASE"): the fallback by ITSELF does not make a clean call
    # noisy -- the same "no noise on a clean write" guarantee this hook
    # has carried from the start.
    journal_path = _seed_committed_journal(tmp_path)
    fresh_ts = dt.datetime.now().isoformat()
    clean_line = _line(ts=fresh_ts, task_id="t-002", model="sonnet", notes="clean, fallback path")
    journal_path.write_text(HEAD_TEXT + clean_line + "\n", encoding="utf-8")
    payload = _post_tool_use_payload(journal_path)  # no original_file -> fallback engaged
    result = _run_hook(payload)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


# =======================================================================
# PAYLOAD-SCOPED ECHO BASE -- e2e, the TS-DRIFT sibling of the tier/
# witness re-echo regression (see tools/test_journal_echo.py /
# tools/test_witness_echo.py for the TIER/WITNESS versions)
# =======================================================================


def test_echo_tsdrift_payload_scoped_not_reechoed_on_later_unrelated_call(tmp_path):
    # Root-cause test: line A is flagged STALE on call #1; call #2 adds
    # a DIFFERENT (fresh) line B -- A must NOT reappear in call #2's
    # output (it is already outside this call's own payload).
    journal_path = _seed_committed_journal(tmp_path)
    stale_ts = (dt.datetime.now() - dt.timedelta(seconds=je.TS_STALE_TOLERANCE_SECONDS + 60)).isoformat()
    line_a = _line(ts=stale_ts, task_id="t-002", model="sonnet", notes="call #1: stale")
    after_call_1 = HEAD_TEXT + line_a + "\n"
    journal_path.write_text(after_call_1, encoding="utf-8")
    result1 = _run_hook(_post_tool_use_payload(journal_path, original_file=HEAD_TEXT))
    assert result1.returncode == 0
    ctx1 = _parse_stdout_json(result1.stdout)["additionalContext"]
    assert "TS DRIFT" in ctx1
    assert "STALE" in ctx1

    fresh_ts = dt.datetime.now().isoformat()
    line_b = _line(ts=fresh_ts, task_id="t-003", model="sonnet", worker_ref="cli:call-b",
                   notes="call #2: unrelated fresh line")
    journal_path.write_text(after_call_1 + line_b + "\n", encoding="utf-8")
    result2 = _run_hook(_post_tool_use_payload(journal_path, original_file=after_call_1))
    assert result2.returncode == 0
    assert result2.stdout == ""
    assert result2.stderr == ""


# =======================================================================
# WITNESS ECHO STALENESS -- _load_witness_edits/_last_edit_ts/
# _last_green_ts/_detect_staleness, pure logic
# =======================================================================


def test_last_edit_ts_max_of_non_doc_only_edits():
    edits = [
        {"ts": "2026-07-10T08:00:00.000000", "file_path": "tools/foo.py"},
        {"ts": "2026-07-10T09:00:00.000000", "file_path": "tools/bar.py"},
    ]
    assert je._last_edit_ts(edits) == "2026-07-10T09:00:00.000000"


def test_last_edit_ts_excludes_doc_only_jsonl_the_journal_itself():
    edits = [
        {"ts": "2026-07-10T08:00:00.000000", "file_path": "tools/foo.py"},
        {"ts": "2026-07-10T09:00:00.000000", "file_path": "logs/routing-log.jsonl"},
    ]
    # The journal write itself (this same accepted line, .jsonl) does not
    # count as "last edit" -- otherwise every batched accepted line
    # would stale itself.
    assert je._last_edit_ts(edits) == "2026-07-10T08:00:00.000000"


def test_last_edit_ts_excludes_doc_only_md_and_json():
    edits = [
        {"ts": "2026-07-10T08:00:00.000000", "file_path": "tools/foo.py"},
        {"ts": "2026-07-10T09:00:00.000000", "file_path": "docs/NOTES.md"},
        {"ts": "2026-07-10T10:00:00.000000", "file_path": "config.json"},
    ]
    assert je._last_edit_ts(edits) == "2026-07-10T08:00:00.000000"


def test_last_edit_ts_excludes_dotfiles():
    edits = [
        {"ts": "2026-07-10T08:00:00.000000", "file_path": "tools/foo.py"},
        {"ts": "2026-07-10T09:00:00.000000", "file_path": ".gitignore"},
    ]
    assert je._last_edit_ts(edits) == "2026-07-10T08:00:00.000000"


def test_last_edit_ts_missing_file_path_conservatively_counted():
    # No file_path at all -- conservatively treated as NOT doc-only
    # (counted as a code edit): missing information does not earn an
    # exemption.
    edits = [{"ts": "2026-07-10T08:00:00.000000"}]
    assert je._last_edit_ts(edits) == "2026-07-10T08:00:00.000000"


def test_last_edit_ts_all_doc_only_returns_none():
    edits = [
        {"ts": "2026-07-10T08:00:00.000000", "file_path": "logs/routing-log.jsonl"},
        {"ts": "2026-07-10T09:00:00.000000", "file_path": "docs/NOTES.md"},
    ]
    assert je._last_edit_ts(edits) is None


def test_last_edit_ts_empty_list_returns_none():
    assert je._last_edit_ts([]) is None


def test_last_edit_ts_skips_non_dict_and_non_string_ts():
    edits = ["not-a-dict", {"ts": 12345, "file_path": "tools/foo.py"},
             {"ts": "2026-07-10T08:00:00.000000", "file_path": "tools/bar.py"}]
    assert je._last_edit_ts(edits) == "2026-07-10T08:00:00.000000"


def test_last_green_ts_max_of_green_runs():
    runs = [
        {"ts": "2026-07-10T08:00:00.000000", "outcome": "green"},
        {"ts": "2026-07-10T09:00:00.000000", "outcome": "red"},
        {"ts": "2026-07-10T10:00:00.000000", "outcome": "green"},
    ]
    assert je._last_green_ts(runs) == "2026-07-10T10:00:00.000000"


def test_last_green_ts_no_green_run_returns_none():
    runs = [{"ts": "2026-07-10T08:00:00.000000", "outcome": "red"}]
    assert je._last_green_ts(runs) is None


def test_last_green_ts_empty_list_returns_none():
    assert je._last_green_ts([]) is None


def test_detect_staleness_no_edits_returns_none():
    runs = [{"ts": "2026-07-10T08:00:00.000000", "outcome": "green"}]
    assert je._detect_staleness(runs, []) is None


def test_detect_staleness_edit_after_last_green_is_stale():
    runs = [{"ts": "2026-07-10T08:00:00.000000", "outcome": "green"}]
    edits = [{"ts": "2026-07-10T09:00:00.000000", "file_path": "tools/foo.py"}]
    result = je._detect_staleness(runs, edits)
    assert result == ("2026-07-10T09:00:00.000000", "2026-07-10T08:00:00.000000")


def test_detect_staleness_edit_before_last_green_is_clean():
    runs = [{"ts": "2026-07-10T09:00:00.000000", "outcome": "green"}]
    edits = [{"ts": "2026-07-10T08:00:00.000000", "file_path": "tools/foo.py"}]
    assert je._detect_staleness(runs, edits) is None


def test_detect_staleness_edit_exactly_at_last_green_is_clean():
    # Boundary: strict `>`, an edit AT the exact same ts as the green run
    # is not a violation.
    runs = [{"ts": "2026-07-10T09:00:00.000000", "outcome": "green"}]
    edits = [{"ts": "2026-07-10T09:00:00.000000", "file_path": "tools/foo.py"}]
    assert je._detect_staleness(runs, edits) is None


def test_detect_staleness_no_green_run_at_all_is_stale():
    runs = [{"ts": "2026-07-10T08:00:00.000000", "outcome": "red"}]
    edits = [{"ts": "2026-07-10T09:00:00.000000", "file_path": "tools/foo.py"}]
    result = je._detect_staleness(runs, edits)
    assert result == ("2026-07-10T09:00:00.000000", None)


def test_detect_staleness_empty_runs_no_green_is_stale():
    edits = [{"ts": "2026-07-10T09:00:00.000000", "file_path": "tools/foo.py"}]
    result = je._detect_staleness([], edits)
    assert result == ("2026-07-10T09:00:00.000000", None)


# =======================================================================
# WITNESS ECHO STALENESS -- e2e, doc-only exemption and independence
# from the command-match axis
# =======================================================================


def test_echo_witness_staleness_flagged_when_edit_after_green_run(tmp_path):
    journal_path = _seed_committed_journal(tmp_path)
    _write_track(
        tmp_path, "sess-1",
        runs=[{"ts": "2026-07-10T08:00:00.000000", "tool_name": "Bash",
               "command": "python -m pytest tools/ -q", "outcome": "green", "agent_id": None}],
        edits=[{"ts": "2026-07-10T09:00:00.000000", "file_path": "tools/foo.py"}],
    )
    fresh_ts = dt.datetime.now().isoformat()
    new_line = _line(ts=fresh_ts, event="accepted", agent="builder",
                      task_id="t-001", by="opus", model="sonnet",
                      witness="ran: python -m pytest tools/ -q",
                      notes="witness matches its own green run, but track has a later edit")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, cwd=str(tmp_path), session_id="sess-1"))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "WITNESS ECHO" in ctx
    assert "track staleness" in ctx
    # The command-match axis is silent (witness matches its own green
    # run) -- no "contradiction" text alongside the staleness warning.
    assert "contradiction" not in ctx


def test_echo_witness_staleness_not_flagged_when_journal_write_itself_is_the_only_edit(tmp_path):
    # The doc-only exemption's whole point: the Edit/Write call that
    # writes THIS accepted line into routing-log.jsonl (itself .jsonl)
    # must not stale itself.
    journal_path = _seed_committed_journal(tmp_path)
    _write_track(
        tmp_path, "sess-1",
        runs=[{"ts": "2026-07-10T08:00:00.000000", "tool_name": "Bash",
               "command": "python -m pytest tools/ -q", "outcome": "green", "agent_id": None}],
        edits=[{"ts": "2026-07-23T23:59:59.000000", "file_path": "logs/routing-log.jsonl"}],
    )
    fresh_ts = dt.datetime.now().isoformat()
    new_line = _line(ts=fresh_ts, event="accepted", agent="builder",
                      task_id="t-001", by="opus", model="sonnet",
                      witness="ran: python -m pytest tools/ -q",
                      notes="only edit in track is the journal write itself")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, cwd=str(tmp_path), session_id="sess-1"))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_witness_staleness_independent_of_command_match_both_fire(tmp_path):
    # Both axes fire on the SAME line: the witness command's OWN latest
    # run is red (command-match axis: warn_loud) AND the track carries a
    # later edit with no green run at all (staleness axis: warn_stale).
    journal_path = _seed_committed_journal(tmp_path)
    _write_track(
        tmp_path, "sess-1",
        runs=[{"ts": "2026-07-10T08:00:00.000000", "tool_name": "Bash",
               "command": "python -m pytest tools/ -q", "outcome": "red", "agent_id": None}],
        edits=[{"ts": "2026-07-10T09:00:00.000000", "file_path": "tools/foo.py"}],
    )
    fresh_ts = dt.datetime.now().isoformat()
    new_line = _line(ts=fresh_ts, event="accepted", agent="builder",
                      task_id="t-001", by="opus", model="sonnet",
                      witness="ran: python -m pytest tools/ -q",
                      notes="both axes fire")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path, cwd=str(tmp_path), session_id="sess-1"))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "contradiction" in ctx
    assert "track staleness" in ctx
