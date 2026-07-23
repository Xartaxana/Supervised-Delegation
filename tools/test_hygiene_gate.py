"""Unit/smoke tests for tools/hygiene_gate.py. Covers: (1) a narrow run
is green (this file itself), (2) the 4 detection classes positively, a
clean command negatively, a non-Bash tool, (3) the adversarial battery
for an interactive surface (DoD rule 11): empty stdin, malformed JSON,
a non-ASCII command, a very long command (>100KB), nested quotes --
exit 0 with no traceback in every case.

Ported from HQ 2026-07-20.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hygiene_gate  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "hygiene_gate.py"


def _run_hook(raw_input, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=raw_input,
        capture_output=True,
        **kwargs,
    )


def _bash_payload(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


# ---------------------------------------------------------------------
# decide() -- pure logic
# ---------------------------------------------------------------------


def test_decide_non_bash_tool_is_silent_pass():
    exit_code, output = hygiene_gate.decide({"tool_name": "Edit", "tool_input": {}})
    assert exit_code == 0
    assert output is None


def test_decide_powershell_tool_checked_too():
    payload = {"tool_name": "PowerShell", "tool_input": {"command": "cd foo && ls"}}
    exit_code, output = hygiene_gate.decide(payload)
    assert exit_code == 0
    assert output is not None
    assert hygiene_gate.MSG_CD_PREFIX in output["hookSpecificOutput"]["additionalContext"]


def test_decide_clean_command_is_silent_pass():
    exit_code, output = hygiene_gate.decide(_bash_payload("python -m pytest tools/ -q"))
    assert exit_code == 0
    assert output is None


def test_decide_cd_prefix_and_amp_triggers():
    exit_code, output = hygiene_gate.decide(_bash_payload("cd gateway && python x.py"))
    assert exit_code == 0
    ctx = output["hookSpecificOutput"]["additionalContext"]
    assert hygiene_gate.MSG_CD_PREFIX in ctx


def test_decide_cd_prefix_with_semicolon_triggers():
    exit_code, output = hygiene_gate.decide(_bash_payload("cd gateway; python x.py"))
    assert exit_code == 0
    ctx = output["hookSpecificOutput"]["additionalContext"]
    assert hygiene_gate.MSG_CD_PREFIX in ctx


def test_decide_bare_cd_without_continuation_does_not_trigger():
    # "cd gateway" alone is a legal form (a permission prompt is only for
    # the cd&&/cd; SEQUENCE form).
    exit_code, output = hygiene_gate.decide(_bash_payload("cd gateway"))
    assert exit_code == 0
    assert output is None


def test_decide_cd_in_middle_of_command_does_not_trigger():
    # cd not at the start of the command -- not a prefix.
    exit_code, output = hygiene_gate.decide(_bash_payload("echo hi && cd gateway"))
    assert exit_code == 0
    assert output is None


def test_decide_redirect_stderr_triggers():
    exit_code, output = hygiene_gate.decide(_bash_payload("python x.py 2>&1"))
    assert exit_code == 0
    ctx = output["hookSpecificOutput"]["additionalContext"]
    assert hygiene_gate.MSG_REDIRECT_STDERR in ctx


def test_decide_python_dash_c_triggers():
    exit_code, output = hygiene_gate.decide(_bash_payload('python -c "print(1)"'))
    assert exit_code == 0
    ctx = output["hookSpecificOutput"]["additionalContext"]
    assert hygiene_gate.MSG_PYTHON_DASH_C in ctx


def test_decide_python_heredoc_triggers():
    exit_code, output = hygiene_gate.decide(_bash_payload("python - <<EOF\nprint(1)\nEOF"))
    assert exit_code == 0
    ctx = output["hookSpecificOutput"]["additionalContext"]
    assert hygiene_gate.MSG_PYTHON_DASH_C in ctx


def test_decide_python3_dash_c_does_not_trigger():
    # Command hygiene names literally "python -c" -- "python3 -c" is not
    # the same token, deliberately not generalized (see module docstring).
    exit_code, output = hygiene_gate.decide(_bash_payload('python3 -c "print(1)"'))
    assert exit_code == 0
    assert output is None


def test_decide_python_dash_m_pytest_does_not_trigger_dash_c():
    exit_code, output = hygiene_gate.decide(_bash_payload("python -m pytest tools/ -q"))
    assert exit_code == 0
    assert output is None


def test_decide_word_boundary_mypython_does_not_trigger():
    exit_code, output = hygiene_gate.decide(_bash_payload("mypython -c foo"))
    assert exit_code == 0
    assert output is None


def test_decide_journal_bypass_redirect_triggers():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("echo done >> logs/routing-log.jsonl")
    )
    assert exit_code == 0
    ctx = output["hookSpecificOutput"]["additionalContext"]
    assert hygiene_gate.MSG_JOURNAL_BYPASS in ctx


def test_decide_journal_bypass_printf_triggers():
    exit_code, output = hygiene_gate.decide(
        _bash_payload('printf \'{"event":"x"}\' logs/routing-log.jsonl')
    )
    assert exit_code == 0
    ctx = output["hookSpecificOutput"]["additionalContext"]
    assert hygiene_gate.MSG_JOURNAL_BYPASS in ctx


def test_decide_journal_bypass_requires_routing_log_substring():
    # A redirect into an arbitrary file WITHOUT "routing-log" is not
    # about the journal -- class (d) does not trigger (deliberate
    # choice, see module docstring -- the class header is "write to the
    # journal", not "any redirect").
    exit_code, output = hygiene_gate.decide(_bash_payload("ls > out.txt"))
    assert exit_code == 0
    assert output is None


def test_decide_journal_bypass_case_insensitive():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("echo x >> LOGS/ROUTING-LOG.JSONL")
    )
    assert exit_code == 0
    ctx = output["hookSpecificOutput"]["additionalContext"]
    assert hygiene_gate.MSG_JOURNAL_BYPASS in ctx


# ---------------------------------------------------------------------
# v2 (ported from HQ) -- git-statement/commit-message false positives of
# class (d)
# ---------------------------------------------------------------------


def test_v2_regress_fp_evidence_literal_add_commit_heredoc_push_no_warn():
    # (a) regression -- the FP shape that motivated the v2 port,
    # verbatim: git add of the journal path && git commit -m with a bash
    # here-string containing the journal path INSIDE the message text,
    # && git push -- git writes nothing to the journal, WARN must not
    # fire.
    command = (
        "git add logs/routing-log.jsonl && git commit -m \"$(cat <<'EOF'\n"
        "text mentioning logs/routing-log.jsonl inside\n"
        "EOF\n"
        ')" && git push'
    )
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is None


def test_v2_git_add_path_alone_no_warn():
    # (b) git add of the journal path, no commit/push -- not about a write.
    exit_code, output = hygiene_gate.decide(_bash_payload("git add logs/routing-log.jsonl"))
    assert exit_code == 0
    assert output is None


def test_p5_grep_journal_path_read_only_no_warn():
    # A read-only grep against the journal path must not warn --
    # _is_journal_bypass() requires ">" or printf/echo in the command;
    # a plain grep has neither.
    exit_code, output = hygiene_gate.decide(
        _bash_payload("grep -n pattern logs/routing-log.jsonl")
    )
    assert exit_code == 0
    assert output is None


def test_p5_rg_journal_path_read_only_no_warn():
    # Same class, ripgrep instead of grep.
    exit_code, output = hygiene_gate.decide(
        _bash_payload("rg pattern logs/routing-log.jsonl")
    )
    assert exit_code == 0
    assert output is None


def test_p5_grep_with_context_flags_journal_path_no_warn():
    # Boundary: grep's -A/-B/-C context flags do not introduce a ">"
    # into the command (not a shell redirect) -- still silent.
    exit_code, output = hygiene_gate.decide(
        _bash_payload("grep -A2 -B2 pattern logs/routing-log.jsonl")
    )
    assert exit_code == 0
    assert output is None


def test_v2_git_commit_message_mentions_routing_log_and_arrow_no_warn():
    command = (
        'git commit -m "Update routing-log format: '
        'old-field -> new-field mapping documented"'
    )
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is None


def test_v2_git_diff_journal_path_with_unrelated_redirect_no_warn():
    # The motivating case for port (2), NOT covered by message-stripping
    # (there is no -m at all): git diff with the journal path as an
    # argument, plus a redirect of git's OWN output to another file --
    # not about writing to the journal.
    exit_code, output = hygiene_gate.decide(
        _bash_payload("git diff logs/routing-log.jsonl > /tmp/out.txt")
    )
    assert exit_code == 0
    assert output is None


def test_v2_git_log_journal_path_piped_no_warn():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("git log -- logs/routing-log.jsonl | head")
    )
    assert exit_code == 0
    assert output is None


def test_v2_git_show_journal_path_no_warn():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("git show HEAD:logs/routing-log.jsonl")
    )
    assert exit_code == 0
    assert output is None


def test_v2_git_status_journal_path_no_warn():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("git status logs/routing-log.jsonl")
    )
    assert exit_code == 0
    assert output is None


def test_v2_unclosed_quote_in_message_not_stripped_but_git_statement_still_masked():
    # A git-statement "git commit ..." (valid OR with an unclosed
    # quote -- masking does not distinguish) falls under
    # GIT_STATEMENT_RE wholesale regardless of the nested quote, so any
    # substring/indicator INSIDE it is silenced by this SECOND layer --
    # WARN does not fire. This is an extension of the already-documented
    # residual gap of class (d) (see module docstring): git commit, even
    # syntactically broken, is not treated as a journal writer -- accepted
    # under the same "WARN is not a security boundary" principle, not a
    # regression of real protection (echo/printf with an unclosed quote
    # is still detected -- see the next test).
    command = 'git commit -m "unterminated message mentions routing-log > oops'
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is None


def test_v2_unclosed_quote_in_non_git_write_command_still_triggers():
    # Same "an unclosed quote must not silently suppress detection"
    # class, but on a REAL writer (echo, not git) -- neither
    # _strip_commit_messages (no "git commit") nor _mask_git_statements
    # (no "git") participate here at all -- the substring/indicator
    # stays visible to the detector as before, WARN fires. This is the
    # real, preserved part of the fail-safe guarantee.
    command = 'echo "unterminated message mentions routing-log > oops'
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is not None


def test_v2_powershell_herestring_message_fully_stripped_no_warn():
    command = (
        "git commit -m @'\n"
        "Update routing-log.jsonl format: old -> new mapping\n"
        "'@"
    )
    exit_code, output = hygiene_gate.decide(
        {"tool_name": "PowerShell", "tool_input": {"command": command}}
    )
    assert exit_code == 0
    assert output is None


def test_v2_two_message_arguments_both_stripped_no_warn():
    command = (
        'git commit -m "first paragraph, clean" '
        '-m "second paragraph mentions routing-log and > arrow"'
    )
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is None


def test_v2_all_crapola_inside_message_no_warn():
    command = 'git commit -m "echo > logs/routing-log.jsonl"'
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is None


def test_v2_single_quoted_message_stripped_no_warn():
    command = "git commit -m 'notes about routing-log.jsonl -> archived'"
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is None


def test_v2_message_flag_long_form_equals_form_stripped_no_warn():
    command = '''git commit --message="routing-log rewritten, old -> new"'''
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is None


def test_v2_non_commit_git_command_not_scrubbed_by_message_stripper():
    # Message-stripping applies ONLY to git commit.
    command = "echo x > logs/routing-log.jsonl"
    assert not hygiene_gate.GIT_COMMIT_RE.search(command)


# --- (c) true positives survive the ports (not weakened) ---


def test_v2_true_positive_echo_after_git_commit_chain_still_triggers():
    command = 'git commit -m "x" && echo evil >> logs/routing-log.jsonl'
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is not None
    assert hygiene_gate.MSG_JOURNAL_BYPASS in output["hookSpecificOutput"]["additionalContext"]


def test_v2_true_positive_sed_inside_command_substitution_outside_message_still_triggers():
    command = "$(sed -n '1p' logs/routing-log.jsonl > logs/routing-log.jsonl.bak)"
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is not None


def test_v2_true_positive_printf_still_triggers_regress():
    exit_code, output = hygiene_gate.decide(
        _bash_payload('printf \'{"event":"x"}\' >> logs/routing-log.jsonl')
    )
    assert exit_code == 0
    assert output is not None


# --- whitelist boundary: an unlisted git subcommand is NOT silenced ---


def test_v2_git_rm_not_in_whitelist_still_triggers_if_it_would_otherwise():
    # "git rm" is not in the whitelist (add/commit/push/diff/log/show/
    # status) -- a deliberate, direct whitelist-boundary test: the
    # constructed command still triggers as ordinary "text with a path
    # and `>`", since masking is not applied to unlisted subcommands.
    command = "git rm logs/routing-log.jsonl > /tmp/log.txt"
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is not None


def test_v2_git_reset_not_in_whitelist_still_triggers():
    command = "git reset -- logs/routing-log.jsonl > /tmp/x.txt"
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is not None


# --- subprocess-level smoke for the evidence shape (DoD) ---


def test_echo_json_v2_regress_evidence_exit0_no_stdout():
    command = (
        "git add logs/routing-log.jsonl && git commit -m \"$(cat <<'EOF'\n"
        "text mentioning logs/routing-log.jsonl inside\n"
        "EOF\n"
        ')" && git push'
    )
    payload = _bash_payload(command)
    result = _run_hook(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert result.returncode == 0
    assert result.stdout.strip() == b""
    assert result.stderr == b""


def test_decide_multiple_classes_all_listed():
    command = 'cd gateway && python -c "print(1)" 2>&1'
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    ctx = output["hookSpecificOutput"]["additionalContext"]
    assert hygiene_gate.MSG_CD_PREFIX in ctx
    assert hygiene_gate.MSG_REDIRECT_STDERR in ctx
    assert hygiene_gate.MSG_PYTHON_DASH_C in ctx


def test_decide_hook_specific_output_shape():
    exit_code, output = hygiene_gate.decide(_bash_payload("cd x && y"))
    assert exit_code == 0
    hso = output["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    # permissionDecision is absent -- the warning must not touch the
    # permission path.
    assert "permissionDecision" not in hso
    assert isinstance(hso["additionalContext"], str) and hso["additionalContext"]


def test_decide_missing_command_is_silent_pass():
    exit_code, output = hygiene_gate.decide({"tool_name": "Bash", "tool_input": {}})
    assert exit_code == 0
    assert output is None


def test_decide_non_string_command_is_silent_pass():
    exit_code, output = hygiene_gate.decide(
        {"tool_name": "Bash", "tool_input": {"command": 123}}
    )
    assert exit_code == 0
    assert output is None


def test_decide_non_dict_payload_is_silent_pass():
    exit_code, output = hygiene_gate.decide(["not", "a", "dict"])
    assert exit_code == 0
    assert output is None


def test_decide_non_dict_tool_input_is_silent_pass():
    exit_code, output = hygiene_gate.decide({"tool_name": "Bash", "tool_input": "oops"})
    assert exit_code == 0
    assert output is None


# ---------------------------------------------------------------------
# subprocess level: exit code, stdout JSON, fail-open
# ---------------------------------------------------------------------


def test_echo_json_clean_command_exit0_no_stdout():
    payload = _bash_payload("python -m pytest tools/ -q")
    result = _run_hook(json.dumps(payload), text=True, encoding="utf-8")
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert result.stderr == ""


def test_echo_json_dirty_command_exit0_with_stdout_json():
    payload = _bash_payload("cd gateway && python x.py 2>&1")
    result = _run_hook(json.dumps(payload), text=True, encoding="utf-8")
    assert result.returncode == 0
    data = json.loads(result.stdout)
    hso = data["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert hygiene_gate.MSG_CD_PREFIX in hso["additionalContext"]
    assert hygiene_gate.MSG_REDIRECT_STDERR in hso["additionalContext"]


def test_echo_json_non_bash_tool_exit0_no_stdout():
    payload = {"tool_name": "Task", "tool_input": {"subagent_type": "builder"}}
    result = _run_hook(json.dumps(payload), text=True, encoding="utf-8")
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# --- adversarial battery (DoD rule 11) ---


def test_adversarial_empty_stdin():
    result = _run_hook("", text=True, encoding="utf-8")
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert result.stderr == ""


def test_adversarial_malformed_json():
    result = _run_hook("{not valid json", text=True, encoding="utf-8")
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert result.stderr == ""


def test_adversarial_non_ascii_command_raw_utf8_bytes():
    # Raw UTF-8 bytes on stdin, WITHOUT text=True -- the exact form the
    # harness actually feeds the child process.
    payload = _bash_payload("cd répo && vérifie 2>&1")
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    result = _run_hook(raw)
    assert result.returncode == 0
    stdout_text = result.stdout.decode("utf-8")
    data = json.loads(stdout_text)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert hygiene_gate.MSG_CD_PREFIX in ctx
    assert hygiene_gate.MSG_REDIRECT_STDERR in ctx


def test_adversarial_very_long_command_no_crash():
    long_command = "python -m pytest " + ("a" * 100_000) + " -q"
    payload = _bash_payload(long_command)
    result = _run_hook(json.dumps(payload), text=True, encoding="utf-8")
    assert result.returncode == 0
    assert result.stderr == ""


def test_adversarial_nested_quotes_no_crash():
    command = """python -c "print('he said \\"hi\\" 2>&1')" """
    payload = _bash_payload(command)
    result = _run_hook(json.dumps(payload), text=True, encoding="utf-8")
    assert result.returncode == 0
    assert result.stderr == ""
    data = json.loads(result.stdout)
    assert hygiene_gate.MSG_PYTHON_DASH_C in data["hookSpecificOutput"]["additionalContext"]


def test_adversarial_null_bytes_in_json_string_no_crash():
    payload = {"tool_name": "Bash", "tool_input": {"command": "cd x && \x00 2>&1"}}
    result = _run_hook(json.dumps(payload), text=True, encoding="utf-8")
    assert result.returncode == 0
    assert result.stderr == ""
