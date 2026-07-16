"""Unit/smoke tests for tools/dod_gate.py -- covers the base
invariant (edit vs. green run ordering), the per-agent filter (a
worker's own records only, main-thread and other workers' edits are
invisible), the doc-only exemption (.md/.json/.jsonl), and the
2-consecutive-blocks safety valve.

Run from the repo root: python -m pytest tools/test_dod_gate.py
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dod_gate  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "dod_gate.py"


def _run_hook(payload: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _stop_payload(cwd: str, session_id: str = "sess-x", agent_id: str = "agent-1") -> dict:
    return {
        "session_id": session_id,
        "cwd": cwd,
        "hook_event_name": "SubagentStop",
        "agent_type": "builder",
        "agent_id": agent_id,
        "stop_hook_active": False,
    }


# ---------------------------------------------------------------------
# evaluate() -- pure logic. Records carry agent_id="agent-1"; calls
# pass agent_id="agent-1" explicitly to match the per-agent filter.
# ---------------------------------------------------------------------


def test_evaluate_no_edits_no_violation():
    violation, reason = dod_gate.evaluate({"edits": [], "runs": []}, agent_id="agent-1")
    assert violation is False
    assert reason == "no-edits"


def test_evaluate_edit_without_any_run_is_violation():
    track = {
        "edits": [{"ts": "2026-07-16T10:00:00.000000", "agent_id": "agent-1"}],
        "runs": [],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is True
    assert reason == "no-green-run"


def test_evaluate_edit_with_only_red_run_is_violation():
    track = {
        "edits": [{"ts": "2026-07-16T10:00:00.000000", "agent_id": "agent-1"}],
        "runs": [
            {"ts": "2026-07-16T10:00:01.000000", "outcome": "red", "agent_id": "agent-1"}
        ],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is True
    assert reason == "no-green-run"


def test_evaluate_green_run_before_edit_is_violation():
    track = {
        "edits": [{"ts": "2026-07-16T10:00:05.000000", "agent_id": "agent-1"}],
        "runs": [
            {"ts": "2026-07-16T10:00:00.000000", "outcome": "green", "agent_id": "agent-1"}
        ],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is True
    assert reason == "green-before-last-edit"


def test_evaluate_green_run_after_edit_is_not_violation():
    track = {
        "edits": [{"ts": "2026-07-16T10:00:00.000000", "agent_id": "agent-1"}],
        "runs": [
            {"ts": "2026-07-16T10:00:05.000000", "outcome": "green", "agent_id": "agent-1"}
        ],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is False
    assert reason == "green-after-last-edit"


# ---------------------------------------------------------------------
# Per-agent filter -- pure logic.
# ---------------------------------------------------------------------


def test_evaluate_agent_id_filters_out_other_agents_edits():
    track = {
        "edits": [{"ts": "t1", "agent_id": "agent-y"}],
        "runs": [],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-x")
    assert violation is False
    assert reason == "no-edits"


def test_evaluate_agent_id_filters_out_main_edits():
    # The core case: main-thread edits (agent_id=None) are invisible
    # to a SubagentStop evaluation for any agent_id.
    track = {
        "edits": [{"ts": "t1", "agent_id": None}],
        "runs": [],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is False
    assert reason == "no-edits"


def test_evaluate_agent_id_own_edit_visible_among_others():
    track = {
        "edits": [
            {"ts": "t0", "agent_id": None},
            {"ts": "t1", "agent_id": "agent-y"},
            {"ts": "t2", "agent_id": "agent-x"},
        ],
        "runs": [],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-x")
    assert violation is True
    assert reason == "no-green-run"


def test_evaluate_agent_id_cross_agent_green_run_not_counted():
    track = {
        "edits": [{"ts": "t1", "agent_id": "agent-x"}],
        "runs": [{"ts": "t2", "outcome": "green", "agent_id": "agent-y"}],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-x")
    assert violation is True
    assert reason == "no-green-run"


def test_evaluate_agent_id_own_green_run_after_own_edit_passes():
    track = {
        "edits": [{"ts": "t1", "agent_id": "agent-x"}],
        "runs": [{"ts": "t2", "outcome": "green", "agent_id": "agent-x"}],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-x")
    assert violation is False
    assert reason == "green-after-last-edit"


def test_evaluate_fallback_no_agent_id_excludes_main_includes_subagent():
    track = {
        "edits": [
            {"ts": "t1", "agent_id": None},
            {"ts": "t2", "agent_id": "agent-1"},
        ],
        "runs": [],
    }
    violation, reason = dod_gate.evaluate(track)
    assert violation is True
    assert reason == "no-green-run"


def test_evaluate_fallback_no_agent_id_only_main_edits_is_no_edits():
    track = {"edits": [{"ts": "t1", "agent_id": None}], "runs": []}
    violation, reason = dod_gate.evaluate(track)
    assert violation is False
    assert reason == "no-edits"


def test_evaluate_fallback_no_agent_id_missing_key_treated_as_main():
    track = {"edits": [{"ts": "t1"}], "runs": []}
    violation, reason = dod_gate.evaluate(track)
    assert violation is False
    assert reason == "no-edits"


# ---------------------------------------------------------------------
# Doc-only rule (.md/.json/.jsonl), on the agent_id-filtered subset.
# ---------------------------------------------------------------------


def test_evaluate_doc_only_md_edits_no_violation():
    track = {
        "edits": [{"ts": "t1", "file_path": "docs/NOTES.md", "agent_id": "agent-1"}],
        "runs": [],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is False
    assert reason == "doc-only-edits-exempt"


def test_evaluate_doc_only_json_edits_no_violation():
    track = {
        "edits": [
            {"ts": "t1", "file_path": ".claude/settings.json", "agent_id": "agent-1"}
        ],
        "runs": [],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is False
    assert reason == "doc-only-edits-exempt"


def test_evaluate_doc_only_jsonl_routing_log_edit_no_violation():
    track = {
        "edits": [
            {"ts": "t1", "file_path": "logs/routing-log.jsonl", "agent_id": "agent-1"}
        ],
        "runs": [],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is False
    assert reason == "doc-only-edits-exempt"


def test_evaluate_doc_only_extension_case_insensitive():
    track = {
        "edits": [{"ts": "t1", "file_path": "docs/NOTES.MD", "agent_id": "agent-1"}],
        "runs": [],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is False
    assert reason == "doc-only-edits-exempt"


def test_evaluate_doc_only_multiple_edits_all_qualifying():
    track = {
        "edits": [
            {"ts": "t1", "file_path": "docs/NOTES.md", "agent_id": "agent-1"},
            {"ts": "t2", "file_path": "logs/routing-log.jsonl", "agent_id": "agent-1"},
            {"ts": "t3", "file_path": ".claude/settings.json", "agent_id": "agent-1"},
        ],
        "runs": [],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is False
    assert reason == "doc-only-edits-exempt"


def test_evaluate_doc_only_subset_for_agent_ignores_other_agents_non_doc_edit():
    track = {
        "edits": [
            {"ts": "t1", "file_path": "docs/NOTES.md", "agent_id": "agent-1"},
            {"ts": "t2", "file_path": "tools/x.py", "agent_id": "agent-2"},
        ],
        "runs": [],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is False
    assert reason == "doc-only-edits-exempt"


def test_evaluate_unknown_file_path_none_is_fail_closed():
    track = {
        "edits": [{"ts": "t1", "file_path": None, "agent_id": "agent-1"}],
        "runs": [],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is True
    assert reason == "no-green-run"


def test_evaluate_missing_file_path_key_is_fail_closed():
    track = {"edits": [{"ts": "t1", "agent_id": "agent-1"}], "runs": []}
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is True
    assert reason == "no-green-run"


def test_evaluate_mixed_md_and_py_edits_invariant_applies():
    track = {
        "edits": [
            {"ts": "t1", "file_path": "README.md", "agent_id": "agent-1"},
            {"ts": "t2", "file_path": "tools/x.py", "agent_id": "agent-1"},
        ],
        "runs": [],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is True
    assert reason == "no-green-run"


def test_evaluate_mixed_extensions_with_green_run_after_last_edit_not_violation():
    track = {
        "edits": [
            {"ts": "2026-07-16T10:00:00.000000", "file_path": "README.md", "agent_id": "agent-1"},
            {"ts": "2026-07-16T10:00:01.000000", "file_path": "tools/x.py", "agent_id": "agent-1"},
        ],
        "runs": [
            {"ts": "2026-07-16T10:00:05.000000", "outcome": "green", "agent_id": "agent-1"}
        ],
    }
    violation, reason = dod_gate.evaluate(track, agent_id="agent-1")
    assert violation is False
    assert reason == "green-after-last-edit"


# ---------------------------------------------------------------------
# decide() -- gate_state / 2-consecutive-blocks safety valve.
# ---------------------------------------------------------------------


def test_decide_blocks_on_first_violation():
    track = {"edits": [{"ts": "t1", "agent_id": "agent-1"}], "runs": []}
    exit_code, message, updated = dod_gate.decide(track, agent_id="agent-1")
    assert exit_code == 2
    assert "blocked" in message
    assert updated["gate_state"]["consecutive_blocks"] == 1
    assert updated["gate_log"][-1]["action"] == "blocked"


def test_decide_blocks_again_on_second_consecutive_violation():
    track = {
        "edits": [{"ts": "t1", "agent_id": "agent-1"}],
        "runs": [],
        "gate_state": {"consecutive_blocks": 1},
    }
    exit_code, message, updated = dod_gate.decide(track, agent_id="agent-1")
    assert exit_code == 2
    assert updated["gate_state"]["consecutive_blocks"] == 2


def test_decide_skips_on_third_consecutive_violation_safety_valve():
    track = {
        "edits": [{"ts": "t1", "agent_id": "agent-1"}],
        "runs": [],
        "gate_state": {"consecutive_blocks": 2},
    }
    exit_code, message, updated = dod_gate.decide(track, agent_id="agent-1")
    assert exit_code == 0
    assert "safety valve" in message
    assert updated["gate_state"]["consecutive_blocks"] == 0
    assert updated["gate_log"][-1]["action"] == "skipped_after_2_blocks"


def test_decide_resets_counter_on_success():
    track = {
        "edits": [{"ts": "t1", "agent_id": "agent-1"}],
        "runs": [{"ts": "t2", "outcome": "green", "agent_id": "agent-1"}],
        "gate_state": {"consecutive_blocks": 1},
    }
    exit_code, message, updated = dod_gate.decide(track, agent_id="agent-1")
    assert exit_code == 0
    assert message == ""
    assert updated["gate_state"]["consecutive_blocks"] == 0


def test_decide_no_edits_passes_without_touching_counter():
    track = {"edits": [], "runs": [], "gate_state": {"consecutive_blocks": 0}}
    exit_code, message, updated = dod_gate.decide(track, agent_id="agent-1")
    assert exit_code == 0
    assert message == ""
    assert updated["gate_state"]["consecutive_blocks"] == 0
    assert "gate_log" not in updated


def test_decide_doc_only_edits_pass_without_touching_counter():
    track = {
        "edits": [
            {"ts": "t1", "file_path": "logs/routing-log.jsonl", "agent_id": "agent-1"}
        ],
        "runs": [],
        "gate_state": {"consecutive_blocks": 1},
    }
    exit_code, message, updated = dod_gate.decide(track, agent_id="agent-1")
    assert exit_code == 0
    assert message == ""
    assert updated["gate_state"]["consecutive_blocks"] == 0


def test_decide_main_only_edits_pass_ignoring_other_agent_id():
    track = {"edits": [{"ts": "t1", "agent_id": None}], "runs": []}
    exit_code, message, updated = dod_gate.decide(track, agent_id="agent-1")
    assert exit_code == 0
    assert message == ""


# ---------------------------------------------------------------------
# echo-JSON subprocess smoke tests -- full block -> run -> skip scenario
# plus doc-only and per-agent scenarios.
# ---------------------------------------------------------------------


def _write_track(tmp_path: Path, session_id: str, data: dict) -> Path:
    path = tmp_path / ".claude" / "dod_track" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_echo_json_no_track_file_passes(tmp_path):
    result = _run_hook(_stop_payload(str(tmp_path), "sess-none"), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".claude" / "dod_track" / "sess-none.json").exists()


def test_echo_json_main_edits_do_not_block_clean_subagent(tmp_path):
    session_id = "sess-main-only"
    _write_track(
        tmp_path,
        session_id,
        {"edits": [{"ts": "t1", "tool_name": "Edit", "agent_id": None}], "runs": []},
    )

    result = _run_hook(
        _stop_payload(str(tmp_path), session_id, agent_id="agent-1"), cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_echo_json_blocks_when_own_edit_without_run(tmp_path):
    session_id = "sess-block"
    _write_track(
        tmp_path,
        session_id,
        {"edits": [{"ts": "t1", "tool_name": "Edit", "agent_id": "agent-1"}], "runs": []},
    )

    result = _run_hook(
        _stop_payload(str(tmp_path), session_id, agent_id="agent-1"), cwd=tmp_path
    )
    assert result.returncode == 2
    assert "blocked" in result.stderr

    track = json.loads((tmp_path / ".claude" / "dod_track" / f"{session_id}.json").read_text())
    assert track["gate_state"]["consecutive_blocks"] == 1


def test_echo_json_worker_not_blocked_by_other_workers_unrun_edit(tmp_path):
    session_id = "sess-parallel"
    _write_track(
        tmp_path,
        session_id,
        {"edits": [{"ts": "t1", "tool_name": "Edit", "agent_id": "agent-y"}], "runs": []},
    )

    result = _run_hook(
        _stop_payload(str(tmp_path), session_id, agent_id="agent-x"), cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_echo_json_passes_when_own_green_run_after_own_edit(tmp_path):
    session_id = "sess-green"
    _write_track(
        tmp_path,
        session_id,
        {
            "edits": [
                {"ts": "2026-07-16T10:00:00.000000", "tool_name": "Edit", "agent_id": "agent-1"}
            ],
            "runs": [
                {
                    "ts": "2026-07-16T10:00:05.000000",
                    "tool_name": "Bash",
                    "command": "python -m pytest tools/ -q",
                    "outcome": "green",
                    "agent_id": "agent-1",
                }
            ],
        },
    )

    result = _run_hook(
        _stop_payload(str(tmp_path), session_id, agent_id="agent-1"), cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_echo_json_blocks_when_green_run_belongs_to_other_agent(tmp_path):
    session_id = "sess-green-other-agent"
    _write_track(
        tmp_path,
        session_id,
        {
            "edits": [
                {"ts": "2026-07-16T10:00:00.000000", "tool_name": "Edit", "agent_id": "agent-1"}
            ],
            "runs": [
                {
                    "ts": "2026-07-16T10:00:05.000000",
                    "tool_name": "Bash",
                    "command": "python -m pytest tools/ -q",
                    "outcome": "green",
                    "agent_id": "agent-2",
                }
            ],
        },
    )

    result = _run_hook(
        _stop_payload(str(tmp_path), session_id, agent_id="agent-1"), cwd=tmp_path
    )
    assert result.returncode == 2
    assert "blocked" in result.stderr


def test_echo_json_safety_valve_after_two_consecutive_blocks(tmp_path):
    session_id = "sess-valve"
    _write_track(
        tmp_path,
        session_id,
        {"edits": [{"ts": "t1", "agent_id": "agent-1"}], "runs": []},
    )

    r1 = _run_hook(_stop_payload(str(tmp_path), session_id, agent_id="agent-1"), cwd=tmp_path)
    assert r1.returncode == 2

    r2 = _run_hook(_stop_payload(str(tmp_path), session_id, agent_id="agent-1"), cwd=tmp_path)
    assert r2.returncode == 2

    r3 = _run_hook(_stop_payload(str(tmp_path), session_id, agent_id="agent-1"), cwd=tmp_path)
    assert r3.returncode == 0
    assert "safety valve" in r3.stderr

    track = json.loads((tmp_path / ".claude" / "dod_track" / f"{session_id}.json").read_text())
    assert track["gate_state"]["consecutive_blocks"] == 0
    actions = [g["action"] for g in track["gate_log"]]
    assert actions == ["blocked", "blocked", "skipped_after_2_blocks"]

    r4 = _run_hook(_stop_payload(str(tmp_path), session_id, agent_id="agent-1"), cwd=tmp_path)
    assert r4.returncode == 2


def test_echo_json_doc_only_md_edit_passes_without_run(tmp_path):
    session_id = "sess-doc-only-md"
    _write_track(
        tmp_path,
        session_id,
        {
            "edits": [
                {
                    "ts": "t1",
                    "tool_name": "Edit",
                    "file_path": "docs/NOTES.md",
                    "agent_id": "agent-1",
                }
            ],
            "runs": [],
        },
    )

    result = _run_hook(
        _stop_payload(str(tmp_path), session_id, agent_id="agent-1"), cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_echo_json_doc_only_jsonl_routing_log_edit_passes_without_run(tmp_path):
    session_id = "sess-doc-only-jsonl"
    _write_track(
        tmp_path,
        session_id,
        {
            "edits": [
                {
                    "ts": "t1",
                    "tool_name": "Edit",
                    "file_path": "logs/routing-log.jsonl",
                    "agent_id": "agent-1",
                }
            ],
            "runs": [],
        },
    )

    result = _run_hook(
        _stop_payload(str(tmp_path), session_id, agent_id="agent-1"), cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_echo_json_doc_only_subset_for_agent_ignores_other_agents_non_doc_edit(tmp_path):
    session_id = "sess-doc-only-subset"
    _write_track(
        tmp_path,
        session_id,
        {
            "edits": [
                {
                    "ts": "t1",
                    "tool_name": "Edit",
                    "file_path": "docs/NOTES.md",
                    "agent_id": "agent-1",
                },
                {
                    "ts": "t2",
                    "tool_name": "Edit",
                    "file_path": "tools/x.py",
                    "agent_id": "agent-2",
                },
            ],
            "runs": [],
        },
    )

    result = _run_hook(
        _stop_payload(str(tmp_path), session_id, agent_id="agent-1"), cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_echo_json_unknown_file_path_still_blocks(tmp_path):
    session_id = "sess-unknown-path"
    _write_track(
        tmp_path,
        session_id,
        {
            "edits": [
                {"ts": "t1", "tool_name": "Edit", "file_path": None, "agent_id": "agent-1"}
            ],
            "runs": [],
        },
    )

    result = _run_hook(
        _stop_payload(str(tmp_path), session_id, agent_id="agent-1"), cwd=tmp_path
    )
    assert result.returncode == 2
    assert "blocked" in result.stderr


def test_echo_json_mixed_extensions_still_blocks(tmp_path):
    session_id = "sess-mixed"
    _write_track(
        tmp_path,
        session_id,
        {
            "edits": [
                {
                    "ts": "t1",
                    "tool_name": "Edit",
                    "file_path": "README.md",
                    "agent_id": "agent-1",
                },
                {
                    "ts": "t2",
                    "tool_name": "Edit",
                    "file_path": "tools/x.py",
                    "agent_id": "agent-1",
                },
            ],
            "runs": [],
        },
    )

    result = _run_hook(
        _stop_payload(str(tmp_path), session_id, agent_id="agent-1"), cwd=tmp_path
    )
    assert result.returncode == 2
    assert "blocked" in result.stderr


def test_echo_json_fallback_payload_without_agent_id_excludes_main_includes_subagent(tmp_path):
    session_id = "sess-fallback"
    _write_track(
        tmp_path,
        session_id,
        {
            "edits": [
                {"ts": "t1", "tool_name": "Edit", "agent_id": None},
                {"ts": "t2", "tool_name": "Edit", "agent_id": "agent-1"},
            ],
            "runs": [],
        },
    )
    payload = _stop_payload(str(tmp_path), session_id)
    del payload["agent_id"]

    result = _run_hook(payload, cwd=tmp_path)
    assert result.returncode == 2
    assert "blocked" in result.stderr


def test_echo_json_fallback_payload_without_agent_id_main_only_passes(tmp_path):
    session_id = "sess-fallback-main-only"
    _write_track(
        tmp_path,
        session_id,
        {"edits": [{"ts": "t1", "tool_name": "Edit", "agent_id": None}], "runs": []},
    )
    payload = _stop_payload(str(tmp_path), session_id)
    del payload["agent_id"]

    result = _run_hook(payload, cwd=tmp_path)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------
# Byte-safe stdin: raw UTF-8 bytes without text=True/encoding on
# subprocess must not crash the hook.
# ---------------------------------------------------------------------


def test_echo_json_raw_utf8_bytes_stdin_no_crash(tmp_path):
    payload = _stop_payload(str(tmp_path), "sess-utf8")
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=raw,
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0


def test_echo_json_malformed_json_fails_open():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="{not valid json",
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert result.stderr == ""
