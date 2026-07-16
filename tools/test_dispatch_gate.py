"""Unit/smoke tests for tools/dispatch_gate.py. Direct calls to
decide() for every branch, plus an echo-JSON subprocess smoke test
(mirrors the calling convention of tools/test_mechanism_gate.py).

Run from the repo root: python -m pytest tools/test_dispatch_gate.py
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dispatch_gate  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "dispatch_gate.py"


def _run_hook(payload) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload) if isinstance(payload, dict) else payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _builder_payload(prompt: str, description=None) -> dict:
    tool_input = {"subagent_type": "builder", "prompt": prompt}
    if description is not None:
        tool_input["description"] = description
    return {"tool_name": "Task", "tool_input": tool_input}


# ---------------------------------------------------------------------
# Not Task/Agent -- always passes.
# ---------------------------------------------------------------------


def test_non_task_tool_passes():
    exit_code, message = dispatch_gate.decide({"tool_name": "Bash", "tool_input": {}})
    assert exit_code == 0
    assert message == ""


def test_missing_tool_input_does_not_crash():
    exit_code, message = dispatch_gate.decide({"tool_name": "Task"})
    assert exit_code == 0


# ---------------------------------------------------------------------
# Check 1: DoD markers for builder.
# ---------------------------------------------------------------------


def test_builder_without_dod_markers_blocks():
    exit_code, message = dispatch_gate.decide(
        _builder_payload("Just fix a typo in file x.py.", description="sonnet: fix typo")
    )
    assert exit_code == 2
    assert "no DoD" in message
    assert "rule 11" in message


def test_builder_with_dod_literal_passes_check1():
    exit_code, message = dispatch_gate.decide(
        _builder_payload("Fix the typo. DoD: test is green.", description="sonnet: fix")
    )
    assert exit_code == 0


def test_builder_with_acceptance_criteria_en_passes_check1():
    exit_code, message = dispatch_gate.decide(
        _builder_payload(
            "Fix the typo. Acceptance criteria: the test passes.",
            description="sonnet: fix",
        )
    )
    assert exit_code == 0


def test_builder_with_criteria_priyomki_cyrillic_passes_check1():
    exit_code, message = dispatch_gate.decide(
        _builder_payload(
            "Fix the typo. Критерии приёмки: тест зелёный.", description="sonnet: fix"
        )
    )
    assert exit_code == 0


def test_builder_with_witness_passes_check1():
    exit_code, message = dispatch_gate.decide(
        _builder_payload("Fix the typo, attach a witness.", description="sonnet: fix")
    )
    assert exit_code == 0


def test_builder_with_verification_run_en_passes_check1():
    exit_code, message = dispatch_gate.decide(
        _builder_payload(
            "Fix the typo and run a verification run.", description="sonnet: fix"
        )
    )
    assert exit_code == 0


def test_builder_with_progon_cyrillic_passes_check1():
    exit_code, message = dispatch_gate.decide(
        _builder_payload(
            "Почини опечатку и прогони проверочный прогон.", description="sonnet: fix"
        )
    )
    assert exit_code == 0


def test_dod_marker_case_insensitive():
    exit_code, _ = dispatch_gate.decide(
        _builder_payload("a fix. dod: green test.", description="sonnet: x")
    )
    assert exit_code == 0


# ---------------------------------------------------------------------
# Write-indicator word-boundary behavior (Cyrillic root "правь"
# matches only as a standalone word, not as a substring of
# "поправь"/"исправь").
# ---------------------------------------------------------------------


def test_pravj_word_boundary_does_not_match_poprav_or_isprav():
    exit_code, message = dispatch_gate.decide(
        _builder_payload(
            "DoD: test is green. Please, поправь опечатку in file x.py.",
            description="sonnet: fix typo",
        )
    )
    assert exit_code == 0

    exit_code2, message2 = dispatch_gate.decide(
        _builder_payload(
            "DoD: test is green. Please, исправь опечатку in file x.py.",
            description="sonnet: fix typo",
        )
    )
    assert exit_code2 == 0


def test_pravj_word_boundary_still_matches_standalone_word():
    exit_code, message = dispatch_gate.decide(
        _builder_payload(
            "DoD: test is green. Правь file x.py per the spec.",
            description="sonnet: fix",
        )
    )
    assert exit_code == 2
    assert "context manifest" in message


# ---------------------------------------------------------------------
# Check 2: manifest on a writing builder dispatch.
# ---------------------------------------------------------------------


def test_builder_readonly_no_write_indicators_skips_check2():
    exit_code, message = dispatch_gate.decide(
        _builder_payload(
            "Read file x.py and tell me what's in it. DoD: an explicit yes/no answer.",
            description="sonnet: read",
        )
    )
    assert exit_code == 0


def test_builder_write_indicator_without_manifest_blocks():
    exit_code, message = dispatch_gate.decide(
        _builder_payload(
            "DoD: test is green. Edit file x.py per the spec.", description="sonnet: fix"
        )
    )
    assert exit_code == 2
    assert "context manifest" in message
    assert "given/owns" in message


def test_builder_write_indicator_with_full_manifest_passes():
    prompt = (
        "DoD: test is green, witness attached. Create file x.py. "
        "MANIFEST: given -- the whole repo; owns -- tools/x.py."
    )
    exit_code, message = dispatch_gate.decide(
        _builder_payload(prompt, description="sonnet: write x")
    )
    assert exit_code == 0


def test_builder_write_indicator_with_only_owns_blocks():
    prompt = "DoD: witness present. owns: tools/x.py. Modify file x.py."
    exit_code, message = dispatch_gate.decide(
        _builder_payload(prompt, description="sonnet: write x")
    )
    assert exit_code == 2
    assert "context manifest" in message


def test_builder_write_indicator_with_only_given_blocks():
    prompt = "DoD: witness present. Given: the whole repo. Create file x.py."
    exit_code, message = dispatch_gate.decide(
        _builder_payload(prompt, description="sonnet: write x")
    )
    assert exit_code == 2
    assert "context manifest" in message


def test_builder_given_and_owns_both_present_passes():
    prompt = (
        "DoD: witness present. Given: the whole repo. owns: tools/x.py. Create file x.py."
    )
    exit_code, _ = dispatch_gate.decide(_builder_payload(prompt, description="sonnet: write x"))
    assert exit_code == 0


def test_builder_write_indicator_en_forms_without_manifest_block():
    for phrase in ["write file x.py", "create file x.py", "edit file x.py", "modify file x.py"]:
        exit_code, message = dispatch_gate.decide(
            _builder_payload(f"DoD: witness present. Please {phrase}.", description="sonnet: x")
        )
        assert exit_code == 2, phrase
        assert "context manifest" in message


# ---------------------------------------------------------------------
# Check 3: description starts with a leading token + separator.
# ---------------------------------------------------------------------


def test_missing_description_skips_check3():
    exit_code, message = dispatch_gate.decide(
        _builder_payload("Read the file. DoD: an explicit answer.")
    )
    assert exit_code == 0
    assert message == ""


def test_description_with_no_separator_blocks():
    exit_code, message = dispatch_gate.decide(
        _builder_payload("Read the file. DoD: an explicit answer.", description="fixbugnow")
    )
    assert exit_code == 2
    assert "worker's tier" in message
    assert "rule 7" in message


def test_description_with_leading_token_and_separator_passes():
    # A FORM-only check (see module docstring): any leading token
    # followed by a space/colon/dash passes, regardless of whether the
    # token names a real tier -- this template has no fixed model list.
    for prefix in ["sonnet: ", "sonnet-", "sonnet ", "haiku: ", "opus: ", "fable: "]:
        exit_code, message = dispatch_gate.decide(
            _builder_payload(
                "Read the file. DoD: an explicit answer.", description=f"{prefix}does the work"
            )
        )
        assert exit_code == 0, f"prefix {prefix!r} should pass, got {message!r}"


def test_description_check_applies_to_critic():
    payload = {
        "tool_name": "Task",
        "tool_input": {"subagent_type": "critic", "prompt": "Review the diff.", "description": "reviewthediff"},
    }
    exit_code, message = dispatch_gate.decide(payload)
    assert exit_code == 2
    assert "worker's tier" in message


def test_description_check_applies_to_scout():
    payload = {
        "tool_name": "Task",
        "tool_input": {"subagent_type": "scout", "prompt": "Find the file.", "description": "findfile"},
    }
    exit_code, message = dispatch_gate.decide(payload)
    assert exit_code == 2
    assert "worker's tier" in message


def test_description_check_passes_for_critic_with_leading_token():
    payload = {
        "tool_name": "Task",
        "tool_input": {
            "subagent_type": "critic",
            "prompt": "Review the diff.",
            "description": "opus: review diff",
        },
    }
    exit_code, message = dispatch_gate.decide(payload)
    assert exit_code == 0


# ---------------------------------------------------------------------
# Point 4: critic/scout -- checks 1 and 2 do not apply.
# ---------------------------------------------------------------------


def test_critic_without_dod_markers_not_blocked_by_check1():
    payload = {
        "tool_name": "Task",
        "tool_input": {
            "subagent_type": "critic",
            "prompt": "Review the diff, not a single DoD word here.",
            "description": "opus: review",
        },
    }
    exit_code, message = dispatch_gate.decide(payload)
    assert exit_code == 0


def test_scout_write_indicator_without_manifest_not_blocked_by_check2():
    payload = {
        "tool_name": "Task",
        "tool_input": {
            "subagent_type": "scout",
            "prompt": "Edit file x and create file notes.md (this is NOT builder, check 2 does not apply).",
            "description": "haiku: scout",
        },
    }
    exit_code, message = dispatch_gate.decide(payload)
    assert exit_code == 0


# ---------------------------------------------------------------------
# Priority 1 -> 2 -> 3 when several checks fail at once.
# ---------------------------------------------------------------------


def test_priority_dod_wins_over_label():
    exit_code, message = dispatch_gate.decide(
        _builder_payload("Edit file x.py.", description="fixitnow")
    )
    assert exit_code == 2
    assert "no DoD" in message


def test_priority_manifest_wins_over_label():
    prompt = "DoD: witness present. Edit file x.py."
    exit_code, message = dispatch_gate.decide(_builder_payload(prompt, description="fixitnow"))
    assert exit_code == 2
    assert "context manifest" in message


# ---------------------------------------------------------------------
# echo-JSON subprocess smoke tests.
# ---------------------------------------------------------------------


def test_echo_json_blocks_builder_without_dod():
    result = _run_hook(_builder_payload("Just a fix.", description="sonnet: fix"))
    assert result.returncode == 2
    assert "no DoD" in result.stderr


def test_echo_json_passes_builder_with_dod():
    result = _run_hook(
        _builder_payload("Fix it. DoD: test is green.", description="sonnet: fix")
    )
    assert result.returncode == 0
    assert result.stderr == ""


def test_echo_json_blocks_missing_manifest():
    result = _run_hook(
        _builder_payload("DoD: test is green. Edit file x.py.", description="sonnet: fix")
    )
    assert result.returncode == 2
    assert "context manifest" in result.stderr


def test_echo_json_blocks_bad_label():
    result = _run_hook(
        _builder_payload("Read the file. DoD: an answer.", description="fixbugnow")
    )
    assert result.returncode == 2
    assert "worker's tier" in result.stderr


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


# ---------------------------------------------------------------------
# Byte-safe stdin: the hook must decode stdin as UTF-8 explicitly (not
# through the platform's locale encoding) -- proven with two forms,
# ASCII-safe \uXXXX escapes AND raw UTF-8 bytes fed without
# text=True/encoding on subprocess (the exact way a harness feeds a
# child process's stdin).
# ---------------------------------------------------------------------

_CYRILLIC_MANIFEST_PAYLOAD = {
    "tool_name": "Task",
    "tool_input": {
        "subagent_type": "builder",
        "prompt": (
            "DoD: критерии приёмки — тест зелёный, witness приложен. "
            "Дано: репо целиком. owns: tools/x.py. Правь файл x.py по спеке."
        ),
        "description": "sonnet: fix",
    },
}


def test_cyrillic_markers_recognized_via_ascii_safe_json_escapes():
    raw = json.dumps(_CYRILLIC_MANIFEST_PAYLOAD, ensure_ascii=True).encode("ascii")
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=raw,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_cyrillic_markers_recognized_via_raw_utf8_bytes():
    raw = json.dumps(_CYRILLIC_MANIFEST_PAYLOAD, ensure_ascii=False).encode("utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=raw,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
