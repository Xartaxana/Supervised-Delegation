"""Battery for tools/critic_verdict_check.py.

Covers: valid verdict/*, invalid combinations, fence extraction (missing /
unclosed / duplicate blocks), broken/non-object JSON, per-field required
checks, non-ASCII data vs ASCII diagnostics, empty input, a large-text
boundary, and non-UTF-8 input (utf-16 file, arbitrary invalid-UTF-8 bytes).

NOTE (manifest gap, flagged in the builder report rather than resolved
silently): the reference implementation this file was ported from also
carries an anti-drift battery comparing its hardcoded rules against a
tools/critic_verdict.schema.json file. That schema file is not part of
this task's owns/write basket for this toolkit, so it is not shipped
here and those anti-drift cases are not reproduced -- every other case
(acceptance keys, cross-field rules, fence extraction, ASCII/non-ASCII,
boundaries, CLI contract) IS reproduced below, hardcoded directly
against this file's own VERDICT_ENUM/validate_verdict rather than
derived from a schema.

Run: python -m pytest tools/test_critic_verdict_check.py -q
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import critic_verdict_check as cvc

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER_PATH = REPO_ROOT / "tools" / "critic_verdict_check.py"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _wrap(obj, prefix="Findings go here.\n\n", suffix="\n"):
    return prefix + "```json\n" + json.dumps(obj, ensure_ascii=False, indent=2) + "\n```" + suffix


def _base_fit():
    return {
        "verdict": "fit",
        "blockers": [],
        "class_completeness": "axis 3 covered, no analogs found",
        "trail": {
            "read": ["tools/critic_verdict_check.py"],
            "reruns": [
                {
                    "command": "python -m pytest tools/test_critic_verdict_check.py -q",
                    "result": "42 passed",
                }
            ],
        },
    }


def _base_fit_with_fixes():
    obj = _base_fit()
    obj["verdict"] = "fit_with_fixes"
    obj["fixes"] = ["add a boundary test for N"]
    return obj


def _base_blocker():
    obj = _base_fit()
    obj["verdict"] = "blocker"
    obj["blockers"] = ["critical finding: race condition in X"]
    return obj


def _run_cli(args, input_text=None):
    return subprocess.run(
        [sys.executable, str(CHECKER_PATH)] + args,
        cwd=str(REPO_ROOT),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=15,
    )


# ---------------------------------------------------------------------------
# valid verdicts (acceptance keys)
# ---------------------------------------------------------------------------


def test_valid_fit_empty_blockers():
    ok, errors, obj = cvc.check_text(_wrap(_base_fit()))
    assert ok, errors
    assert obj["verdict"] == "fit"


def test_valid_fit_with_fixes_nonempty_fixes():
    ok, errors, obj = cvc.check_text(_wrap(_base_fit_with_fixes()))
    assert ok, errors


def test_valid_blocker_nonempty_blockers():
    ok, errors, obj = cvc.check_text(_wrap(_base_blocker()))
    assert ok, errors


# ---------------------------------------------------------------------------
# verdict/blockers/fixes cross-field rules
# ---------------------------------------------------------------------------


def test_fit_with_fixes_missing_fixes_fails():
    obj = _base_fit_with_fixes()
    del obj["fixes"]
    ok, errors, _ = cvc.check_text(_wrap(obj))
    assert not ok
    assert any("fixes" in e for e in errors)


def test_fit_with_fixes_empty_fixes_fails():
    obj = _base_fit_with_fixes()
    obj["fixes"] = []
    ok, errors, _ = cvc.check_text(_wrap(obj))
    assert not ok
    assert any("fixes" in e for e in errors)


def test_blocker_with_empty_blockers_fails():
    obj = _base_blocker()
    obj["blockers"] = []
    ok, errors, _ = cvc.check_text(_wrap(obj))
    assert not ok
    assert any("blockers" in e for e in errors)


def test_fit_with_nonempty_blockers_fails():
    obj = _base_fit()
    obj["blockers"] = ["not actually empty"]
    ok, errors, _ = cvc.check_text(_wrap(obj))
    assert not ok
    assert any("blockers" in e for e in errors)


def test_verdict_outside_enum_fails():
    obj = _base_fit()
    obj["verdict"] = "meh"
    ok, errors, _ = cvc.check_text(_wrap(obj))
    assert not ok
    assert any("verdict" in e for e in errors)


# ---------------------------------------------------------------------------
# fence extraction
# ---------------------------------------------------------------------------


def test_no_json_block_fails():
    ok, errors, _ = cvc.check_text("Just prose, no fenced block anywhere.")
    assert not ok
    assert any("no fenced" in e for e in errors)


def test_two_blocks_uses_last():
    first = {"verdict": "meh"}  # malformed on purpose - must NOT be used
    second = _base_fit_with_fixes()
    text = (
        "Draft:\n```json\n"
        + json.dumps(first)
        + "\n```\n\nFinal:\n```json\n"
        + json.dumps(second)
        + "\n```\n"
    )
    ok, errors, obj = cvc.check_text(text)
    assert ok, errors
    assert obj["verdict"] == "fit_with_fixes"


def test_unclosed_fence_reports_no_block():
    text = "Findings.\n```json\n" + json.dumps(_base_fit())
    ok, errors, _ = cvc.check_text(text)
    assert not ok
    assert any("no fenced" in e for e in errors)


def test_broken_json_fails():
    text = "Findings.\n```json\n{not valid json,,,\n```\n"
    ok, errors, _ = cvc.check_text(text)
    assert not ok
    assert any("invalid JSON" in e for e in errors)


def test_json_array_instead_of_object_fails():
    text = "Findings.\n```json\n[1, 2, 3]\n```\n"
    ok, errors, _ = cvc.check_text(text)
    assert not ok
    assert any("not an object" in e for e in errors)


# ---------------------------------------------------------------------------
# per-field required checks (named field in diagnostic)
# ---------------------------------------------------------------------------


def test_missing_verdict_field():
    obj = _base_fit()
    del obj["verdict"]
    ok, errors, _ = cvc.check_text(_wrap(obj))
    assert not ok
    assert any("verdict" in e for e in errors)


def test_missing_blockers_field():
    obj = _base_fit()
    del obj["blockers"]
    ok, errors, _ = cvc.check_text(_wrap(obj))
    assert not ok
    assert any("blockers" in e for e in errors)


def test_missing_class_completeness_field():
    obj = _base_fit()
    del obj["class_completeness"]
    ok, errors, _ = cvc.check_text(_wrap(obj))
    assert not ok
    assert any("class_completeness" in e for e in errors)


def test_missing_trail_field():
    obj = _base_fit()
    del obj["trail"]
    ok, errors, _ = cvc.check_text(_wrap(obj))
    assert not ok
    assert any("trail" in e for e in errors)


def test_trail_without_read_fails():
    obj = _base_fit()
    del obj["trail"]["read"]
    ok, errors, _ = cvc.check_text(_wrap(obj))
    assert not ok
    assert any("trail.read" in e for e in errors)


def test_trail_without_reruns_fails():
    obj = _base_fit()
    del obj["trail"]["reruns"]
    ok, errors, _ = cvc.check_text(_wrap(obj))
    assert not ok
    assert any("trail.reruns" in e for e in errors)


def test_reruns_element_without_command_fails():
    obj = _base_fit()
    obj["trail"]["reruns"] = [{"result": "3 passed"}]
    ok, errors, _ = cvc.check_text(_wrap(obj))
    assert not ok
    assert any("command" in e for e in errors)


def test_reruns_element_without_result_fails():
    obj = _base_fit()
    obj["trail"]["reruns"] = [{"command": "pytest -q"}]
    ok, errors, _ = cvc.check_text(_wrap(obj))
    assert not ok
    assert any("result" in e for e in errors)


# ---------------------------------------------------------------------------
# ASCII diagnostics vs non-ASCII data; empty input; large input boundary
# ---------------------------------------------------------------------------


def test_cyrillic_data_is_valid_and_output_is_ascii():
    obj = _base_blocker()
    obj["blockers"] = ["Кириллический текст находки блокера"]
    obj["class_completeness"] = "ось 3 покрыта, ось 7 в очередь порта"
    ok, errors, _ = cvc.check_text(_wrap(obj))
    assert ok, errors

    result = _run_cli(["-"], input_text=_wrap(obj))
    assert result.returncode == 0
    assert result.stdout.strip().startswith("VERDICT OK:")
    result.stdout.encode("ascii")  # raises UnicodeEncodeError if not ASCII


def test_diagnostics_are_ascii_even_with_cyrillic_input():
    obj = _base_fit()
    obj["blockers"] = ["Кириллический не-пустой blockers при fit"]
    result = _run_cli(["-"], input_text=_wrap(obj))
    assert result.returncode == 1
    result.stderr.encode("ascii")  # raises UnicodeEncodeError if not ASCII


def test_empty_input_fails():
    ok, errors, _ = cvc.check_text("")
    assert not ok
    assert any("no fenced" in e for e in errors)


def test_large_input_with_trailing_block_works():
    padding = "x" * 120_000
    text = padding + "\n\n" + _wrap(_base_fit_with_fixes())
    ok, errors, obj = cvc.check_text(text)
    assert ok, errors
    assert obj["verdict"] == "fit_with_fixes"


# ---------------------------------------------------------------------------
# non-UTF-8 input (the file-open branch must not leak a raw traceback on
# decode failure -- fail-closed with an ASCII line)
# ---------------------------------------------------------------------------


def test_cli_utf16_file_fails_clean_no_traceback(tmp_path):
    p = tmp_path / "verdict_utf16.txt"
    p.write_text(_wrap(_base_fit()), encoding="utf-16")
    result = _run_cli([str(p)])
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout
    assert "INVALID VERDICT: input is not valid UTF-8" in result.stderr
    result.stderr.encode("ascii")
    result.stdout.encode("ascii")


def test_cli_arbitrary_invalid_bytes_file_fails_clean_no_traceback(tmp_path):
    p = tmp_path / "verdict_garbage.bin"
    p.write_bytes(bytes([0xFF, 0xFE, 0x00, 0xD8, 0xFF, 0xFF, 0x80, 0x81] * 50))
    result = _run_cli([str(p)])
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout
    assert "INVALID VERDICT: input is not valid UTF-8" in result.stderr
    result.stderr.encode("ascii")
    result.stdout.encode("ascii")


def test_cli_stdin_invalid_bytes_fails_clean_no_traceback():
    # Same failure class as the file branch (fix the class, not the
    # instance): invalid bytes on stdin. PYTHONIOENCODING pins the
    # child's stdin to strict utf-8 so the case is deterministic across
    # locales (a default Windows locale may decode 0xFF permissively as
    # cp1251).
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, str(CHECKER_PATH), "-"],
        cwd=str(REPO_ROOT),
        input=bytes([0xFF, 0xFE, 0x80, 0x81] * 20),
        capture_output=True,
        timeout=15,
        env=env,
    )
    assert result.returncode == 1
    stderr = result.stderr.decode("ascii")
    stdout = result.stdout.decode("ascii")
    assert "Traceback" not in stderr
    assert "Traceback" not in stdout
    assert "INVALID VERDICT: input is not valid UTF-8" in stderr


# ---------------------------------------------------------------------------
# CLI contract (file path and stdin "-")
# ---------------------------------------------------------------------------


def test_cli_valid_file_exit_zero(tmp_path):
    p = tmp_path / "verdict.txt"
    p.write_text(_wrap(_base_fit()), encoding="utf-8")
    result = _run_cli([str(p)])
    assert result.returncode == 0
    assert "VERDICT OK: fit, blockers: 0, fixes: 0" in result.stdout


def test_cli_invalid_file_exit_one(tmp_path):
    p = tmp_path / "verdict.txt"
    obj = _base_fit()
    del obj["class_completeness"]
    p.write_text(_wrap(obj), encoding="utf-8")
    result = _run_cli([str(p)])
    assert result.returncode == 1
    assert "INVALID VERDICT" in result.stderr
    assert "class_completeness" in result.stderr


def test_cli_stdin_dash_valid():
    result = _run_cli(["-"], input_text=_wrap(_base_blocker()))
    assert result.returncode == 0
    assert "VERDICT OK: blocker" in result.stdout


def test_cli_missing_argument_exit_one():
    result = _run_cli([])
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# enum values -- each verdict enum member is individually accepted
# (a bounded, hardcoded mirror of the schema-driven anti-drift test the
# reference implementation carries -- see the module docstring's note)
# ---------------------------------------------------------------------------


def test_verdict_enum_each_value_accepted_one_case_per_value():
    builders = {
        "fit": _base_fit,
        "fit_with_fixes": _base_fit_with_fixes,
        "blocker": _base_blocker,
    }
    assert set(builders) == set(cvc.VERDICT_ENUM)
    for value, builder in builders.items():
        ok, errors, obj = cvc.check_text(_wrap(builder()))
        assert ok, errors
        assert obj["verdict"] == value
