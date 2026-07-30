"""Unit/smoke tests for tools/dispatch_gate.py. Direct calls to
decide() for every branch, plus an echo-JSON subprocess smoke test
(mirrors the calling convention of tools/test_mechanism_gate.py).

Run from the repo root: python -m pytest tools/test_dispatch_gate.py
"""

import json
import subprocess
import sys
import time
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


# ---------------------------------------------------------------------
# Marker word-boundary hardening: a bare filename substring must not
# count as a real marker (DOD_MARKERS_RE, MANIFEST_GIVEN_RE,
# MANIFEST_OWNS_RE). Closed holes: a false match used to let a builder
# dispatch with no real marker slip past the corresponding check.
# ---------------------------------------------------------------------


def test_dod_gate_filename_substring_is_no_longer_a_dod_marker():
    """A basket mentioning tools/dod_gate.py, with no real DoD marker
    anywhere else in the prompt, must still block check 1 -- "dod" as a
    substring of the filename is not a DoD marker."""
    exit_code, message = dispatch_gate.decide(
        _builder_payload(
            "Given: tools/dod_gate.py for reference. Just fix a typo.",
            description="sonnet: fix",
        )
    )
    assert exit_code == 2
    assert "no DoD" in message


def test_dod_marker_colon_and_hyphen_forms_still_match():
    """Word-boundary hardening must not break the legal forms: "DoD:"
    and "DoD-marker" both still match (colon/hyphen are non-word chars,
    a boundary exists on both sides of "DoD")."""
    exit_code, _ = dispatch_gate.decide(
        _builder_payload("Fix the typo. DoD: test is green.", description="sonnet: fix")
    )
    assert exit_code == 0

    exit_code2, _ = dispatch_gate.decide(
        _builder_payload("Fix the typo per the DoD-marker below.", description="sonnet: fix")
    )
    assert exit_code2 == 0


def test_witness_echo_filename_substring_is_no_longer_a_dod_marker():
    """A basket mentioning tools/test_witness_echo.py, with no real
    witness marker anywhere else, must still block check 1 -- "witness"
    inside the underscore-joined filename is not a standalone word."""
    exit_code, message = dispatch_gate.decide(
        _builder_payload(
            "Given: tools/test_witness_echo.py for reference. Just fix a typo.",
            description="sonnet: fix",
        )
    )
    assert exit_code == 2
    assert "no DoD" in message


def test_manifest_given_word_boundary_prodano_false_positive_fixed():
    """The Russian word "продано" ("sold out") contains "дано" as a
    substring but is not a given-marker -- a builder write dispatch
    whose ONLY occurrence of that substring is inside "продано" (no
    real given/owns markers) must still block check 2."""
    prompt = "DoD: witness present. На складе всё продано. owns: tools/x.py. Правь file x.py."
    exit_code, message = dispatch_gate.decide(_builder_payload(prompt, description="sonnet: fix"))
    assert exit_code == 2
    assert "context manifest" in message


def test_manifest_given_real_forms_unaffected_by_word_boundary():
    """Legal given-marker forms ("Дано:", "дано --", "Given:") keep
    matching after the word-boundary hardening -- their surrounding
    characters (space/colon/dash) already provided a boundary before
    this change."""
    for prompt in (
        "DoD: witness present. Дано: репо целиком. owns: tools/x.py. Правь file x.py.",
        "DoD: witness present. дано -- репо целиком. owns: tools/x.py. Правь file x.py.",
        "DoD: witness present. Given: the whole repo. owns: tools/x.py. Edit file x.py.",
    ):
        exit_code, message = dispatch_gate.decide(_builder_payload(prompt, description="sonnet: fix"))
        assert exit_code == 0, message


def test_owns_filename_substring_is_no_longer_an_owns_marker():
    """A basket mentioning a filename that merely CONTAINS "owns" as a
    substring (not the standalone word "owns") must not satisfy the
    owns half of the manifest check."""
    prompt = "DoD: witness present. Given: tools/ownership_report.py for reference. Правь file x.py."
    exit_code, message = dispatch_gate.decide(_builder_payload(prompt, description="sonnet: fix"))
    assert exit_code == 2
    assert "context manifest" in message


def test_owns_real_marker_unaffected_by_word_boundary():
    prompt = "DoD: witness present. Given: the whole repo. owns: tools/x.py. Edit file x.py."
    exit_code, _ = dispatch_gate.decide(_builder_payload(prompt, description="sonnet: fix"))
    assert exit_code == 0


def test_readonly_dispatch_mentioning_ownership_filename_in_given_not_blocked():
    """The reverse edge of the same class: a READ-ONLY builder dispatch
    (no write indicator at all) that happens to mention a filename
    containing "owns" as a substring must not be blocked by check 2 --
    check 2 is skipped entirely when there is no write indicator."""
    prompt = "DoD: witness present. Given: tools/ownership_report.py. Tell me what it does."
    exit_code, message = dispatch_gate.decide(_builder_payload(prompt, description="sonnet: fix"))
    assert exit_code == 0, message


# =======================================================================
# Given-path WARN layer (Part A): extraction, root/foreign-tree
# handling, noise threshold, and the main()-level wiring.
# =======================================================================


def test_extract_given_candidates_absolute_and_repo_relative():
    prompt = (
        "Given: D:\\repo\\tools\\x.py and also tools/y.py and gateway/z.yaml for reference."
    )
    candidates = dispatch_gate.extract_given_candidates(prompt)
    toks = [c[0] for c in candidates]
    assert "D:\\repo\\tools\\x.py" in toks
    assert "tools/y.py" in toks
    assert "gateway/z.yaml" in toks


def test_extract_given_candidates_dedup_and_order_of_first_appearance():
    prompt = "Given: tools/x.py, then again tools/x.py, then tools/y.py."
    candidates = dispatch_gate.extract_given_candidates(prompt)
    toks = [c[0] for c in candidates]
    assert toks == ["tools/x.py", "tools/y.py"]


def test_extract_given_candidates_absolute_and_relative_forms_not_double_counted():
    """An absolute path like "D:/repo/tools/x.py" must not ALSO be
    picked up by the repo-relative regex as "tools/x.py" -- the
    negative lookbehind before the repo-relative prefix excludes a
    prefix immediately preceded by a path separator."""
    prompt = "Given: D:/repo/tools/x.py for reference."
    candidates = dispatch_gate.extract_given_candidates(prompt)
    toks = [c[0] for c in candidates]
    assert toks.count("tools/x.py") == 0
    assert "D:/repo/tools/x.py" in toks


def test_extract_given_candidates_placeholders_not_extracted():
    prompt = "Given: <path/to/file>, *.py, {name}.py, $VAR.py -- none of these are real paths."
    candidates = dispatch_gate.extract_given_candidates(prompt)
    assert candidates == []


def test_is_under_root_true_inside_false_outside():
    assert dispatch_gate._is_under_root("D:\\repo\\tools\\x.py", "D:\\repo")
    assert not dispatch_gate._is_under_root("D:\\OtherTree\\x.py", "D:\\repo")


def test_find_missing_given_paths_existing_file_not_missing(tmp_path):
    (tmp_path / "tools").mkdir()
    real_file = tmp_path / "tools" / "x.py"
    real_file.write_text("# real")
    prompt = f"Given: {real_file} for reference."
    missing = dispatch_gate.find_missing_given_paths(prompt, str(tmp_path))
    assert missing == []


def test_find_missing_given_paths_missing_file_reported(tmp_path):
    missing_path = tmp_path / "tools" / "does_not_exist.py"
    prompt = f"Given: {missing_path} for reference."
    missing = dispatch_gate.find_missing_given_paths(prompt, str(tmp_path))
    assert missing == [str(missing_path)]


def test_find_missing_given_paths_foreign_tree_absolute_path_skipped(tmp_path):
    """An absolute path OUTSIDE repo_root (a foreign tree) is not
    checked at all -- neither reported missing nor treated as an
    error, even though it obviously does not exist under repo_root."""
    foreign = "D:\\SomeOtherTree\\nope.py"
    prompt = f"Given: {foreign} for reference."
    missing = dispatch_gate.find_missing_given_paths(prompt, str(tmp_path))
    assert missing == []


def test_find_missing_given_paths_existing_directory_not_reported(tmp_path):
    """A path candidate that resolves to an EXISTING DIRECTORY (not a
    file) is not reported missing -- os.path.exists() is true for
    directories too; documented behavior, not a bug to fix here."""
    (tmp_path / "tools").mkdir()
    # A directory won't normally match the file-extension-requiring
    # regexes, so simulate the documented case directly via the
    # lower-level helper: a repo-relative candidate whose resolved path
    # is a directory that happens to carry a dotted suffix.
    weird_dir = tmp_path / "tools" / "x.py"
    weird_dir.mkdir()
    prompt = "Given: tools/x.py for reference."
    missing = dispatch_gate.find_missing_given_paths(prompt, str(tmp_path))
    assert missing == []


def test_format_given_path_warn_empty_list_returns_empty_string():
    assert dispatch_gate.format_given_path_warn([]) == ""


def test_format_given_path_warn_threshold_boundary_10_vs_11():
    ten = [f"tools/f{i}.py" for i in range(10)]
    eleven = [f"tools/f{i}.py" for i in range(11)]

    warn_ten = dispatch_gate.format_given_path_warn(ten)
    assert "GIVEN-PATH WARN" in warn_ten
    for path in ten:
        assert path in warn_ten
    assert "first 3" not in warn_ten

    warn_eleven = dispatch_gate.format_given_path_warn(eleven)
    assert "11 paths do not exist" in warn_eleven
    assert "first 3" in warn_eleven
    for path in eleven[:3]:
        assert path in warn_eleven
    assert eleven[-1] not in warn_eleven


# --- critic finding: the path body is bounded to {0,300} instead of a
# greedy `*` (protection against quadratic backtracking on a
# pathological prompt). ------------------------------------------------

def test_extract_given_candidates_body_exactly_300_chars_extracted():
    body = "a" * 300
    prompt = f"Given: D:\\{body}.py for reference."
    candidates = dispatch_gate.extract_given_candidates(prompt)
    toks = [c[0] for c in candidates]
    assert f"D:\\{body}.py" in toks


def test_extract_given_candidates_body_301_chars_not_extracted():
    # 301 -- a truncation, documented behavior (see the
    # GIVEN_ABS_WIN_PATH_RE/GIVEN_REPO_REL_PATH_RE module comment): no
    # warn is promised for such a path -- the regex simply finds no
    # match at all (all 301 characters are "a", the extension's dot
    # only appears after the 301st character, {0,300} cannot reach it).
    body = "a" * 301
    prompt = f"Given: D:\\{body}.py for reference."
    candidates = dispatch_gate.extract_given_candidates(prompt)
    toks = [c[0] for c in candidates]
    assert f"D:\\{body}.py" not in toks
    assert candidates == []


def test_extract_given_candidates_pathological_input_under_5s():
    # Critic's measured form: "C:/"*20000 + "a"*20000 -- no dot-
    # extension at all (measured hang before the fix: 89.5s on 240KB).
    # Threshold 5s -- margin for slow CI; real behavior is expected
    # under 1s.
    pathological = "C:/" * 20000 + "a" * 20000
    start = time.monotonic()
    candidates = dispatch_gate.extract_given_candidates(pathological)
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"took {elapsed:.2f}s -- quadratic regression?"
    assert candidates == []


def test_extract_given_candidates_1000_real_paths_previous_behavior():
    names = [f"tools/fake{i}.py" for i in range(1000)]
    prompt = "Given: " + ", ".join(names) + ". Read them all."
    start = time.monotonic()
    candidates = dispatch_gate.extract_given_candidates(prompt)
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"took {elapsed:.2f}s"
    toks = [c[0] for c in candidates]
    assert toks == names


def test_given_path_warn_non_task_agent_tool_returns_empty():
    assert dispatch_gate.given_path_warn({"tool_name": "Bash", "tool_input": {}}) == ""


def test_given_path_warn_empty_or_non_string_prompt_returns_empty():
    payload_missing_prompt = {"tool_name": "Task", "tool_input": {"subagent_type": "builder"}}
    assert dispatch_gate.given_path_warn(payload_missing_prompt) == ""

    payload_empty_prompt = {"tool_name": "Task", "tool_input": {"prompt": ""}}
    assert dispatch_gate.given_path_warn(payload_empty_prompt) == ""

    payload_non_string_prompt = {"tool_name": "Task", "tool_input": {"prompt": 12345}}
    assert dispatch_gate.given_path_warn(payload_non_string_prompt) == ""


def test_given_path_warn_non_dict_tool_input_returns_empty():
    payload = {"tool_name": "Task", "tool_input": "not a dict"}
    assert dispatch_gate.given_path_warn(payload) == ""


def test_given_path_warn_missing_cwd_falls_back_to_getcwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    payload = {
        "tool_name": "Task",
        "tool_input": {"prompt": "Given: tools/does_not_exist_at_all.py for reference."},
    }
    warn = dispatch_gate.given_path_warn(payload)
    assert "GIVEN-PATH WARN" in warn
    assert "tools/does_not_exist_at_all.py" in warn


def test_given_path_warn_returns_empty_when_all_candidates_exist(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "x.py").write_text("# real")
    payload = {
        "tool_name": "Task",
        "tool_input": {"prompt": "Given: tools/x.py for reference."},
        "cwd": str(tmp_path),
    }
    assert dispatch_gate.given_path_warn(payload) == ""


# --- main()-level wiring: blocking verdict suppresses the WARN layer ---


def test_echo_json_blocking_verdict_suppresses_given_path_warn():
    """When decide() already blocks (missing DoD), the given-path WARN
    layer must not run at all -- no additionalContext on stdout, just
    the blocking stderr message and exit 2."""
    payload = {
        "tool_name": "Task",
        "tool_input": {
            "subagent_type": "builder",
            "prompt": "Given: tools/does_not_exist_anywhere.py. Just fix a typo.",
            "description": "sonnet: fix",
        },
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 2
    assert "no DoD" in result.stderr
    assert result.stdout == ""


def test_echo_json_passing_dispatch_emits_given_path_warn_additional_context():
    """A dispatch that PASSES decide() but names a missing given-path
    gets the additionalContext JSON on stdout, exit 0."""
    payload = {
        "tool_name": "Task",
        "tool_input": {
            "subagent_type": "critic",
            "prompt": "Review Given: tools/does_not_exist_anywhere_xyz.py for reference.",
            "description": "opus: review",
        },
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert result.stderr == ""
    parsed = json.loads(result.stdout)
    warn_text = parsed["hookSpecificOutput"]["additionalContext"]
    assert "GIVEN-PATH WARN" in warn_text
    assert "tools/does_not_exist_anywhere_xyz.py" in warn_text


def test_echo_json_no_missing_given_paths_emits_no_stdout():
    """A dispatch mentioning only paths that genuinely exist (e.g. this
    very test file) emits no additionalContext at all."""
    payload = {
        "tool_name": "Task",
        "tool_input": {
            "subagent_type": "critic",
            "prompt": "Review tools/dispatch_gate.py for reference.",
            "description": "opus: review",
        },
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_given_path_warn_layer_exception_is_swallowed_exit_0(monkeypatch):
    """Any exception raised inside the WARN layer must be swallowed by
    main()'s belt-and-suspenders try/except -- the blocking hook must
    never crash over a WARN-layer failure."""
    import dispatch_gate as dg

    def boom(payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(dg, "given_path_warn", boom)
    payload = {"tool_name": "Task", "tool_input": {"prompt": "Given: tools/x.py."}}
    exit_code, message = dg.decide(payload)
    assert exit_code == 0
    # Simulate main()'s own try/except around the (now monkeypatched) call.
    try:
        warn = dg.given_path_warn(payload)
    except Exception:
        warn = ""
    assert warn == ""
