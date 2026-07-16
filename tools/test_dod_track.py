"""Unit/smoke tests for tools/dod_track.py -- direct calls to the pure
functions (build_fact/determine_outcome/is_verification_command) plus
an echo-JSON subprocess smoke test.

Run from the repo root: python -m pytest tools/test_dod_track.py
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dod_track  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "dod_track.py"


def _run_hook(payload: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------
# build_fact -- pure logic.
# ---------------------------------------------------------------------


def test_build_fact_edit_tool_logged():
    for tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        kind, entry = dod_track.build_fact({"tool_name": tool_name})
        assert kind == "edit"
        assert entry["tool_name"] == tool_name
        assert "ts" in entry
        # No tool_input at all -- file_path is unknown -> None.
        assert entry["file_path"] is None


def test_build_fact_irrelevant_tool_ignored():
    assert dod_track.build_fact({"tool_name": "Read"}) is None
    assert dod_track.build_fact({"tool_name": "Grep"}) is None


def test_build_fact_bash_non_verification_command_ignored():
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
        "tool_response": {"stdout": "ok", "stderr": ""},
    }
    assert dod_track.build_fact(payload) is None


def test_build_fact_bash_verification_command_green():
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "python -m pytest tools/ -q"},
        "tool_response": {"stdout": "5 passed in 0.12s", "stderr": ""},
    }
    kind, entry = dod_track.build_fact(payload)
    assert kind == "run"
    assert entry["outcome"] == "green"
    assert entry["command"] == "python -m pytest tools/ -q"


def test_build_fact_powershell_verification_command_green():
    # Some harness environments run shell commands via the PowerShell
    # tool rather than Bash -- verification runs must be visible to
    # the track regardless of which shell tool ran them.
    payload = {
        "tool_name": "PowerShell",
        "tool_input": {"command": "python -m pytest tools/ -q"},
        "tool_response": {"stdout": "131 passed in 2.64s", "stderr": ""},
    }
    kind, entry = dod_track.build_fact(payload)
    assert kind == "run"
    assert entry["outcome"] == "green"
    assert entry["tool_name"] == "PowerShell"


def test_build_fact_powershell_non_verification_command_ignored():
    payload = {
        "tool_name": "PowerShell",
        "tool_input": {"command": "Get-ChildItem tools"},
        "tool_response": {"stdout": "ok", "stderr": ""},
    }
    assert dod_track.build_fact(payload) is None


def test_build_fact_bash_verification_command_red_on_failure_text():
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "pytest tools/"},
        "tool_response": {
            "stdout": "",
            "stderr": "Traceback (most recent call last):\n1 failed, 0 passed",
        },
    }
    kind, entry = dod_track.build_fact(payload)
    assert kind == "run"
    assert entry["outcome"] == "red"


def test_build_fact_bash_verification_command_red_on_ambiguous_output():
    # Neither a failure nor a success indicator -- the safe default is
    # "red" (an unrecognized output is not a confirmed green run).
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "python -m pytest --collect-only"},
        "tool_response": {"stdout": "no tests ran", "stderr": ""},
    }
    kind, entry = dod_track.build_fact(payload)
    assert kind == "run"
    assert entry["outcome"] == "red"


def test_build_fact_rc_field_overrides_text_when_present():
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "python -m pytest tools/"},
        "tool_response": {"stdout": "something failed", "rc": 0},
    }
    kind, entry = dod_track.build_fact(payload)
    assert entry["outcome"] == "green"

    payload2 = {
        "tool_name": "Bash",
        "tool_input": {"command": "python -m pytest tools/"},
        "tool_response": {"stdout": "5 passed", "exit_code": 1},
    }
    _, entry2 = dod_track.build_fact(payload2)
    assert entry2["outcome"] == "red"


def test_is_verification_command_matches_spec_forms():
    assert dod_track.is_verification_command("pytest")
    assert dod_track.is_verification_command("python -m pytest tools/ -q")
    assert dod_track.is_verification_command("python test_something.py")
    assert not dod_track.is_verification_command("ls -la")
    assert not dod_track.is_verification_command("git status")


def test_gate_infra_self_tests_are_verification_commands():
    # Testing the gates themselves is a legitimate deliverable in this
    # deployment -- running their own test files IS a valid witness,
    # both the canonical and a narrow target.
    for cmd in [
        "pytest tools/test_dod_gate.py",
        "python -m pytest tools/test_dispatch_gate.py -q",
        "pytest tools/test_dod_track.py",
        "python -m pytest tools/test_main_gate.py -q",
    ]:
        assert dod_track.is_verification_command(cmd), cmd


def test_gate_infra_self_test_build_fact_produces_run():
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "pytest tools/test_dod_gate.py -q"},
        "tool_response": {"stdout": "5 passed in 0.01s", "stderr": ""},
    }
    kind, entry = dod_track.build_fact(payload)
    assert kind == "run"
    assert entry["outcome"] == "green"
    assert entry["command"] == "pytest tools/test_dod_gate.py -q"


def test_canonical_command_recognized_as_verification():
    assert dod_track.is_verification_command("python -m pytest tools/ gateway/ -q")


def test_narrow_target_command_recognized_as_verification():
    assert dod_track.is_verification_command("pytest tools/test_dispatch_gate.py -q")


def test_both_canonical_and_narrow_forms_produce_run_facts():
    canonical_payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "python -m pytest tools/ gateway/ -q"},
        "tool_response": {"stdout": "381 passed in 4.20s", "stderr": ""},
    }
    kind, entry = dod_track.build_fact(canonical_payload)
    assert kind == "run"
    assert entry["outcome"] == "green"

    narrow_payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "pytest tools/test_dispatch_gate.py -q"},
        "tool_response": {"stdout": "30 passed in 0.50s", "stderr": ""},
    }
    kind2, entry2 = dod_track.build_fact(narrow_payload)
    assert kind2 == "run"
    assert entry2["outcome"] == "green"


# ---------------------------------------------------------------------
# Non-pytest witness forms -- a Node script, a UI screenshot run.
# ---------------------------------------------------------------------


def test_node_script_recognized_as_verification_command():
    assert dod_track.is_verification_command("node run_check.js")
    assert dod_track.is_verification_command("node scripts/verify.mjs")
    assert not dod_track.is_verification_command("node --version")


def test_ui_screenshot_command_recognized_as_verification_command():
    assert dod_track.is_verification_command("node take_screenshot.js")
    assert dod_track.is_verification_command("python run_playwright_check.py --screenshot")
    assert dod_track.is_verification_command("python capture_ui.py --puppeteer")


def test_node_script_outcome_uses_same_text_heuristics():
    green_payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "node run_check.js"},
        "tool_response": {"stdout": "All checks passed", "stderr": ""},
    }
    kind, entry = dod_track.build_fact(green_payload)
    assert kind == "run"
    assert entry["outcome"] == "green"

    red_payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "node run_check.js"},
        "tool_response": {"stdout": "", "stderr": "Error: check failed"},
    }
    kind2, entry2 = dod_track.build_fact(red_payload)
    assert entry2["outcome"] == "red"


def test_ui_witness_command_silent_output_defaults_red():
    # A documented limitation: a script with no textual confirmation
    # (neither passed/ok nor failed/error/traceback) still lands on
    # the safe "red" default -- even though the command is now
    # recognized (visible in the track) rather than invisible entirely.
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "node take_screenshot.js"},
        "tool_response": {"stdout": "screenshot.png saved", "stderr": ""},
    }
    kind, entry = dod_track.build_fact(payload)
    assert kind == "run"
    assert entry["outcome"] == "red"


# ---------------------------------------------------------------------
# build_fact() edit records carry file_path.
# ---------------------------------------------------------------------


def test_build_fact_edit_includes_file_path_from_tool_input():
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "tools/dod_gate.py", "old_string": "a", "new_string": "b"},
    }
    kind, entry = dod_track.build_fact(payload)
    assert kind == "edit"
    assert entry["file_path"] == "tools/dod_gate.py"


def test_build_fact_edit_file_path_missing_key_defaults_to_none():
    payload = {"tool_name": "Write", "tool_input": {"content": "x"}}
    kind, entry = dod_track.build_fact(payload)
    assert kind == "edit"
    assert entry["file_path"] is None


def test_build_fact_edit_file_path_non_string_defaults_to_none():
    payload = {"tool_name": "MultiEdit", "tool_input": {"file_path": 12345}}
    kind, entry = dod_track.build_fact(payload)
    assert kind == "edit"
    assert entry["file_path"] is None


def test_build_fact_edit_file_path_for_each_edit_tool_name():
    for tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        payload = {"tool_name": tool_name, "tool_input": {"file_path": f"docs/{tool_name}.md"}}
        kind, entry = dod_track.build_fact(payload)
        assert kind == "edit"
        assert entry["file_path"] == f"docs/{tool_name}.md"


# ---------------------------------------------------------------------
# echo-JSON subprocess smoke tests.
# ---------------------------------------------------------------------


def test_echo_json_logs_edit(tmp_path):
    payload = {
        "session_id": "sess-1",
        "cwd": str(tmp_path),
        "tool_name": "Edit",
        "tool_input": {"file_path": "x.py"},
    }
    result = _run_hook(payload, cwd=tmp_path)
    assert result.returncode == 0, result.stderr

    track_path = tmp_path / ".claude" / "dod_track" / "sess-1.json"
    assert track_path.exists()
    data = json.loads(track_path.read_text(encoding="utf-8"))
    assert len(data["edits"]) == 1
    assert data["edits"][0]["tool_name"] == "Edit"
    assert data["edits"][0]["file_path"] == "x.py"
    assert data["runs"] == []


def test_echo_json_logs_green_and_red_runs_distinctly(tmp_path):
    session_id = "sess-2"
    green_payload = {
        "session_id": session_id,
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_input": {"command": "python -m pytest tools/ -q"},
        "tool_response": {"stdout": "3 passed in 0.05s", "stderr": ""},
    }
    red_payload = {
        "session_id": session_id,
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_input": {"command": "pytest tools/"},
        "tool_response": {"stdout": "", "stderr": "1 failed, 2 passed"},
    }

    r1 = _run_hook(green_payload, cwd=tmp_path)
    assert r1.returncode == 0, r1.stderr
    r2 = _run_hook(red_payload, cwd=tmp_path)
    assert r2.returncode == 0, r2.stderr

    track_path = tmp_path / ".claude" / "dod_track" / f"{session_id}.json"
    data = json.loads(track_path.read_text(encoding="utf-8"))
    assert len(data["runs"]) == 2
    assert data["runs"][0]["outcome"] == "green"
    assert data["runs"][1]["outcome"] == "red"


def test_echo_json_logs_gate_infra_self_test_run(tmp_path):
    payload = {
        "session_id": "sess-gate-infra",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_input": {"command": "pytest tools/test_dod_gate.py -q"},
        "tool_response": {"stdout": "12 passed in 0.30s", "stderr": ""},
    }
    result = _run_hook(payload, cwd=tmp_path)
    assert result.returncode == 0, result.stderr

    track_path = tmp_path / ".claude" / "dod_track" / "sess-gate-infra.json"
    data = json.loads(track_path.read_text(encoding="utf-8"))
    assert len(data["runs"]) == 1
    assert data["runs"][0]["outcome"] == "green"


def test_echo_json_ignores_unrelated_tool(tmp_path):
    payload = {
        "session_id": "sess-3",
        "cwd": str(tmp_path),
        "tool_name": "Read",
        "tool_input": {"file_path": "x.py"},
    }
    result = _run_hook(payload, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".claude" / "dod_track" / "sess-3.json").exists()


def test_echo_json_preserves_unknown_keys_written_by_other_hook(tmp_path):
    """dod_gate.py/main_gate.py write gate_state/main_gate_state/gate_log
    keys into the same file -- dod_track.py's own read-modify-write must
    not wipe them out."""
    session_id = "sess-4"
    track_path = tmp_path / ".claude" / "dod_track" / f"{session_id}.json"
    track_path.parent.mkdir(parents=True)
    track_path.write_text(
        json.dumps(
            {
                "edits": [],
                "runs": [],
                "gate_state": {"consecutive_blocks": 1},
                "gate_log": [{"action": "blocked", "reason": "no-green-run"}],
            }
        ),
        encoding="utf-8",
    )

    payload = {
        "session_id": session_id,
        "cwd": str(tmp_path),
        "tool_name": "Write",
        "tool_input": {"file_path": "y.py"},
    }
    result = _run_hook(payload, cwd=tmp_path)
    assert result.returncode == 0, result.stderr

    data = json.loads(track_path.read_text(encoding="utf-8"))
    assert len(data["edits"]) == 1
    assert data["gate_state"] == {"consecutive_blocks": 1}
    assert data["gate_log"] == [{"action": "blocked", "reason": "no-green-run"}]


# ---------------------------------------------------------------------
# Byte-safe stdin: a subprocess smoke test with raw UTF-8 bytes
# (ensure_ascii=False, input=bytes, no text=True/encoding on
# subprocess). A Cyrillic file_path is a meaningful check: if the raw-
# byte read + explicit UTF-8 decode were broken or absent, the
# platform's locale encoding could mangle non-ASCII text into
# mojibake, and entry["file_path"] would not match the original string.
# ---------------------------------------------------------------------


def test_echo_json_raw_utf8_bytes_stdin_preserves_cyrillic_file_path(tmp_path):
    session_id = "sess-utf8"
    payload = {
        "session_id": session_id,
        "cwd": str(tmp_path),
        "tool_name": "Edit",
        "tool_input": {"file_path": "докстринг/файл.py"},
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=raw,
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")

    track_path = tmp_path / ".claude" / "dod_track" / f"{session_id}.json"
    data = json.loads(track_path.read_text(encoding="utf-8"))
    assert data["edits"][0]["file_path"] == "докстринг/файл.py"
