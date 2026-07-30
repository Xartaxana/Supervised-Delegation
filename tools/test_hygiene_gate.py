"""Unit/smoke tests for tools/hygiene_gate.py. Covers: (1) a narrow run
is green (this file itself), (2) the 4 detection classes positively, a
clean command negatively, a non-Bash tool, (3) the adversarial battery
for an interactive surface (DoD rule 11): empty stdin, malformed JSON,
a non-ASCII command, a very long command (>100KB), nested quotes --
exit 0 with no traceback in every case.

Ported from HQ 2026-07-20 (v2 delta 2026-07-21, v3 delta 2026-07-23).

v3 -- class (d) (shell write to the journal) is promoted WARN -> BLOCK
(permissionDecision="deny" + permissionDecisionReason, WITHOUT a
change to the exit code -- see the v3 section of the module docstring
in tools/hygiene_gate.py). The "..._journal_bypass_..."/
"..._true_positive_..." tests for class (d) are UPDATED to check
permissionDecision/permissionDecisionReason instead of
additionalContext (MSG_JOURNAL_BYPASS renamed to MSG_JOURNAL_BLOCK).
Added (see the matching sections below): sed -i/tee/python-open-write-
mode/heredoc-redirect as BLOCK forms; tail/cat/wc read-only and
echo-to-a-non-journal-file as NOT a block; a ./-path, an absolute
path, quotes around the path, a $-variable (documented honest
limitation), a "benign && writing" compound; *.jsonl-under-logs/ (the
widened target); statement scoping (an own live finding -- read+
unrelated-write in different statements no longer triggers); the
live git -C false positive (a regression test for a three-git-C
compound).

Belt-and-suspenders addendum -- additionalContext ALWAYS duplicates
the class-(d) block reason (the same string as
permissionDecisionReason), not only when another WARN class also
fired -- insurance against a dead deny channel on a real harness (see
the "test_belt_*" section below and the v3 module docstring).

Quote-aware redirect addendum -- a `>` inside single/double quotes is
an argument string (e.g. grep's), not a shell redirect -- it no
longer counts as a write form (see the "test_quoted_*" section below
and _mask_quoted_segments in tools/hygiene_gate.py).
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


def test_decide_journal_bypass_redirect_blocks():
    # v3: class (d) is now a BLOCK, not a WARN -- permissionDecision=
    # "deny" + permissionDecisionReason (verbatim MSG_JOURNAL_BLOCK),
    # NOT additionalContext; exit_code stays 0.
    exit_code, output = hygiene_gate.decide(
        _bash_payload("echo done >> logs/routing-log.jsonl")
    )
    assert exit_code == 0
    hso = output["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == hygiene_gate.MSG_JOURNAL_BLOCK


def test_decide_journal_bypass_printf_blocks():
    exit_code, output = hygiene_gate.decide(
        _bash_payload('printf \'{"event":"x"}\' logs/routing-log.jsonl')
    )
    assert exit_code == 0
    hso = output["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == hygiene_gate.MSG_JOURNAL_BLOCK


def test_decide_journal_bypass_requires_routing_log_substring():
    # A redirect into an arbitrary file with NEITHER "routing-log" nor a
    # logs/*.jsonl path is not about the journal -- class (d) does not
    # trigger (deliberate choice, see module docstring -- the class
    # header is "write to the journal", not "any redirect").
    exit_code, output = hygiene_gate.decide(_bash_payload("ls > out.txt"))
    assert exit_code == 0
    assert output is None


def test_decide_journal_bypass_case_insensitive():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("echo x >> LOGS/ROUTING-LOG.JSONL")
    )
    assert exit_code == 0
    hso = output["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == hygiene_gate.MSG_JOURNAL_BLOCK


# ---------------------------------------------------------------------
# v3 -- class (d) BLOCK: additional write forms (DoD point 1)
# ---------------------------------------------------------------------


def test_v3_block_sed_inplace():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("sed -i 's/x/y/' logs/routing-log.jsonl")
    )
    assert exit_code == 0
    hso = output["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == hygiene_gate.MSG_JOURNAL_BLOCK


def test_v3_sed_without_dash_i_does_not_block():
    # Boundary: sed WITHOUT -i (prints, does not edit in place) is not a
    # write form by itself (no ">"/printf/echo/tee/open-write either).
    exit_code, output = hygiene_gate.decide(
        _bash_payload("sed -n '1p' logs/routing-log.jsonl")
    )
    assert exit_code == 0
    assert output is None


def test_v3_block_python_open_append_mode():
    command = "python -c \"open('logs/routing-log.jsonl','a').write('x')\""
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    hso = output["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == hygiene_gate.MSG_JOURNAL_BLOCK
    # python -c is an independent WARN class (c) that also fired --
    # appears alongside the block (see "combination semantics" in the
    # module docstring).
    assert hygiene_gate.MSG_PYTHON_DASH_C in hso["additionalContext"]


def test_v3_python_open_read_mode_does_not_block_via_open_indicator():
    # open(path,'r') is a read, not a write form; the "routing-log"
    # substring is present, but no write indicator (redirect/printf/
    # echo/sed-i/tee/open-write-mode) in this statement matches.
    command = "python -c \"print(open('logs/routing-log.jsonl','r').read())\""
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    # python -c by itself is the independent WARN class (c), not a block.
    assert output is not None
    assert "permissionDecision" not in output["hookSpecificOutput"]
    assert hygiene_gate.MSG_PYTHON_DASH_C in output["hookSpecificOutput"]["additionalContext"]


def test_v3_block_tee():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("echo hi | tee logs/routing-log.jsonl")
    )
    assert exit_code == 0
    hso = output["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == hygiene_gate.MSG_JOURNAL_BLOCK


def test_v3_block_heredoc_redirect():
    command = 'cat <<EOF >> logs/routing-log.jsonl\n{"event":"x"}\nEOF'
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    hso = output["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == hygiene_gate.MSG_JOURNAL_BLOCK


# ---------------------------------------------------------------------
# v3 -- quote-aware redirect: a live false BLOCK on read-only
# `grep -c ">" logs/routing-log.jsonl` -- a quoted `>` (an argument
# string, not a shell redirect) must not count as a write form. Other
# indicators (printf/echo/sed -i/tee/open-write-mode) are unaffected.
# ---------------------------------------------------------------------


def test_quoted_grep_dash_c_quoted_arrow_journal_read_no_warn():
    exit_code, output = hygiene_gate.decide(
        _bash_payload('grep -c ">" logs/routing-log.jsonl')
    )
    assert exit_code == 0
    assert output is None


def test_quoted_grep_quoted_arrow_journal_read_no_warn():
    exit_code, output = hygiene_gate.decide(
        _bash_payload('grep ">" logs/routing-log.jsonl')
    )
    assert exit_code == 0
    assert output is None


def test_quoted_unquoted_redirect_single_still_blocks():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("echo x > logs/foo.jsonl")
    )
    assert exit_code == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_quoted_unquoted_redirect_append_still_blocks():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("echo x >> logs/foo.jsonl")
    )
    assert exit_code == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_quoted_data_but_redirect_outside_quotes_still_blocks():
    # Quotes around DATA ("x"), the `>` redirect OUTSIDE the quotes: a
    # real write, must still block despite quote masking.
    exit_code, output = hygiene_gate.decide(
        _bash_payload('echo "x" > logs/foo.jsonl')
    )
    assert exit_code == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_quoted_arrow_as_data_plus_real_redirect_still_blocks():
    # A quoted '>' is printf's data; the real `>>` OUTSIDE quotes is an
    # actual write into the journal -- must still block.
    command = "printf '%s\\n' '>' >> logs/foo.jsonl"
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_quoted_mask_quoted_segments_unit():
    # A unit test on the masking function itself -- quoted content is
    # masked, text outside quotes is untouched.
    masked = hygiene_gate._mask_quoted_segments('grep -c ">" logs/x.jsonl')
    assert ">" not in masked
    assert "logs/x.jsonl" in masked


# ---------------------------------------------------------------------
# v3 -- belt-and-suspenders: additionalContext ALWAYS duplicates the
# class-(d) block reason, not only permissionDecisionReason -- insurance
# in case the harness does not enforce permissionDecision="deny" (no
# live deny precedent existed in this kit at port time -- the one live
# blocking gate, dispatch_gate.py, blocks via exit code 2, a different
# channel). A dead deny must degrade into a visible WARN, not silence.
# ---------------------------------------------------------------------


def test_belt_block_carries_both_deny_fields_and_matching_additional_context():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("echo done >> logs/routing-log.jsonl")
    )
    assert exit_code == 0
    hso = output["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == hygiene_gate.MSG_JOURNAL_BLOCK
    assert "additionalContext" in hso
    assert hso["additionalContext"].startswith(
        "Command hygiene: " + hygiene_gate.MSG_JOURNAL_BLOCK
    )


def test_belt_block_plus_other_warn_class_both_texts_present_not_overwritten():
    command = "cd gateway && echo evil >> logs/routing-log.jsonl"
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    hso = output["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == hygiene_gate.MSG_JOURNAL_BLOCK
    ctx = hso["additionalContext"]
    assert hygiene_gate.MSG_JOURNAL_BLOCK in ctx
    assert hygiene_gate.MSG_CD_PREFIX in ctx


def test_belt_pure_warn_call_has_no_deny_fields_regression():
    # Regression of existing behavior: a call that triggers ONLY WARN
    # classes (a)/(b)/(c) -- without class (d) -- carries neither
    # permissionDecision nor permissionDecisionReason; additionalContext
    # stays in the previous WARN format.
    exit_code, output = hygiene_gate.decide(_bash_payload("cd gateway && python x.py 2>&1"))
    assert exit_code == 0
    hso = output["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert "permissionDecisionReason" not in hso
    assert hygiene_gate.MSG_CD_PREFIX in hso["additionalContext"]
    assert hygiene_gate.MSG_REDIRECT_STDERR in hso["additionalContext"]


# ---------------------------------------------------------------------
# v3 -- NOT a block: reading the journal via shell (DoD point 2)
# ---------------------------------------------------------------------


def test_v3_tail_journal_read_only_no_warn():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("tail -n 5 logs/routing-log.jsonl")
    )
    assert exit_code == 0
    assert output is None


def test_v3_cat_journal_read_only_no_warn():
    exit_code, output = hygiene_gate.decide(_bash_payload("cat logs/routing-log.jsonl"))
    assert exit_code == 0
    assert output is None


def test_v3_wc_journal_read_only_no_warn():
    exit_code, output = hygiene_gate.decide(_bash_payload("wc -l logs/routing-log.jsonl"))
    assert exit_code == 0
    assert output is None


def test_v3_echo_to_non_journal_file_stays_unclassified():
    exit_code, output = hygiene_gate.decide(_bash_payload("echo hi >> notes.txt"))
    assert exit_code == 0
    assert output is None


# ---------------------------------------------------------------------
# v3 -- boundary/adversarial path forms (DoD point 3)
# ---------------------------------------------------------------------


def test_v3_relative_dot_slash_path_blocks():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("echo x >> ./logs/routing-log.jsonl")
    )
    assert exit_code == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_v3_absolute_path_blocks():
    command = "echo x >> /home/user/Operating-System-for-LLMs/logs/routing-log.jsonl"
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_v3_quoted_path_blocks():
    exit_code, output = hygiene_gate.decide(
        _bash_payload('echo x >> "logs/routing-log.jsonl"')
    )
    assert exit_code == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_v3_variable_path_not_recognized_no_block_honest_limitation():
    # Honest limitation (documented, not silent): a path through a
    # $-variable is not recognized as the journal -- not caught by a
    # static text matcher, NOT a block.
    exit_code, output = hygiene_gate.decide(_bash_payload("echo x >> $F"))
    assert exit_code == 0
    assert output is None


def test_v3_compound_benign_then_write_blocks():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("ls -la && echo bad >> logs/routing-log.jsonl")
    )
    assert exit_code == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_v3_broadened_target_other_jsonl_under_logs_blocks():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("echo x >> logs/other-name.jsonl")
    )
    assert exit_code == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_v3_non_jsonl_file_under_logs_not_broadened_target():
    # Boundary of the widened target: *.txt under logs/ does not match
    # JOURNAL_JSONL_UNDER_LOGS_RE (no ".jsonl"), and the "routing-log"
    # substring is absent too -- not about the journal at all.
    exit_code, output = hygiene_gate.decide(
        _bash_payload("echo x >> logs/other-name.txt")
    )
    assert exit_code == 0
    assert output is None


# ---------------------------------------------------------------------
# v3 -- statement scoping: target and write form must be in the SAME
# statement, not anywhere in the command (see the module docstring,
# "STATEMENT SCOPING")
# ---------------------------------------------------------------------


def test_v3_read_then_unrelated_write_different_statement_no_warn():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("cat logs/routing-log.jsonl; echo done")
    )
    assert exit_code == 0
    assert output is None


def test_v3_journal_read_piped_to_unrelated_tee_no_warn():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("cat logs/routing-log.jsonl | tee /tmp/out.txt")
    )
    assert exit_code == 0
    assert output is None


def test_v3_write_and_target_in_same_statement_still_blocks():
    # A positive control of the same class: when target and write form
    # ARE in the same statement, the block remains.
    exit_code, output = hygiene_gate.decide(
        _bash_payload("echo done >> logs/routing-log.jsonl; echo unrelated")
    )
    assert exit_code == 0
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------
# v3 -- git -C <dir> compound false positive
# ---------------------------------------------------------------------


def test_v3_git_dash_capital_c_compound_add_commit_push_no_warn():
    command = (
        "git -C /home/user/Operating-System-for-LLMs add docs/x.md "
        "logs/routing-log.jsonl CURRENT_CONTEXT.md && "
        'git -C /home/user/Operating-System-for-LLMs commit -m "docs: old -> new" && '
        "git -C /home/user/Operating-System-for-LLMs push -u origin main"
    )
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is None


def test_v3_git_dash_capital_c_single_add_no_warn():
    exit_code, output = hygiene_gate.decide(
        _bash_payload("git -C /home/user/Operating-System-for-LLMs add logs/routing-log.jsonl")
    )
    assert exit_code == 0
    assert output is None


def test_v3_git_dash_capital_c_commit_message_arrow_stripped_no_warn():
    command = 'git -C /home/user/Operating-System-for-LLMs commit -m "routing-log: old -> new"'
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is None


# ---------------------------------------------------------------------
# v2 (ported from HQ) -- git-statement/commit-message false positives
# of class (d)
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
    # no block fires. This is an extension of the already-documented
    # residual gap of class (d) (see module docstring): git commit, even
    # syntactically broken, is not treated as a journal writer -- accepted
    # under the same "not preemptively closed" principle, not a
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
    # stays visible to the detector as before, the block fires. This is
    # the real, preserved part of the fail-safe guarantee.
    command = 'echo "unterminated message mentions routing-log > oops'
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


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
    # v3: class (d) is now a BLOCK -- check permissionDecision/
    # permissionDecisionReason, not additionalContext (was
    # MSG_JOURNAL_BYPASS in additionalContext before promotion).
    command = 'git commit -m "x" && echo evil >> logs/routing-log.jsonl'
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is not None
    hso = output["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == hygiene_gate.MSG_JOURNAL_BLOCK


def test_v2_true_positive_sed_inside_command_substitution_outside_message_still_triggers():
    command = "$(sed -n '1p' logs/routing-log.jsonl > logs/routing-log.jsonl.bak)"
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_v2_true_positive_printf_still_triggers_regress():
    exit_code, output = hygiene_gate.decide(
        _bash_payload('printf \'{"event":"x"}\' >> logs/routing-log.jsonl')
    )
    assert exit_code == 0
    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


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
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_v2_git_reset_not_in_whitelist_still_triggers():
    command = "git reset -- logs/routing-log.jsonl > /tmp/x.txt"
    exit_code, output = hygiene_gate.decide(_bash_payload(command))
    assert exit_code == 0
    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


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


# =======================================================================
# v4 -- heredoc-body scrub for `git commit -F - <<DELIM ... DELIM`
# =======================================================================

# Built from parts rather than as a literal string so this test file
# itself never contains the literal journal path as a plain substring
# (the very shell-write pattern this hook's own class (d) detects) --
# same reason class-(d) tests elsewhere in this file already avoid a
# bare literal path in an unrelated write form.
_JOURNAL_TARGET = "logs/" + "routing-log.jsonl"


def test_heredoc_delimiter_forms_all_scrubbed():
    """All four heredoc-opener delimiter quotings scrub the body: a
    journal-path mention INSIDE the commit-message body must disappear
    after _strip_commit_messages, for every quoting form."""
    for opener in ("<<EOF", "<<'EOF'", '<<"EOF"', "<<-EOF"):
        cmd = f"git commit -F - {opener}\nSee {_JOURNAL_TARGET} for details\nEOF"
        stripped = hygiene_gate._strip_commit_messages(cmd)
        assert "routing-log" not in stripped.lower(), opener


def test_heredoc_unclosed_not_matched_left_as_is():
    """A heredoc with no closing delimiter line does not match
    COMMIT_HEREDOC_RE at all -- fail-safe toward detection: the text is
    left completely unchanged, not silently half-scrubbed."""
    cmd = f"git commit -F - <<EOF\nSee {_JOURNAL_TARGET} here, no closer at all"
    stripped = hygiene_gate._strip_commit_messages(cmd)
    assert stripped == cmd


def test_heredoc_scrub_preserves_opener_line_trailing_content():
    """Content on the SAME line as the opener, after `<<DELIM`, is kept
    verbatim (group 4) -- only the body (group 5) is cut."""
    cmd = f"git commit -F - <<EOF trailing-marker\nSee {_JOURNAL_TARGET} here\nEOF"
    stripped = hygiene_gate._strip_commit_messages(cmd)
    assert "trailing-marker" in stripped
    assert "routing-log" not in stripped.lower()


def test_heredoc_pin_chained_write_after_closing_delimiter_still_blocks():
    """PIN: a real write chained via && on the line AFTER the heredoc's
    closing delimiter must still BLOCK class (d) -- the scrub must not
    eat a trailing `&& echo ... >> journal` that sits outside the
    heredoc body itself."""
    cmd = (
        "git commit -F - <<EOF\n"
        "Commit message body, nothing journal-related here.\n"
        "EOF\n"
        f'&& echo "{{}}" >> {_JOURNAL_TARGET}'
    )
    exit_code, output = hygiene_gate.decide(_bash_payload(cmd))
    assert exit_code == 0
    hso = output["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == hygiene_gate.MSG_JOURNAL_BLOCK


def test_heredoc_pin_python_heredoc_after_git_commit_not_scrubbed_write_detected():
    """PIN: `git commit -m "x" && python - <<'PY'` with a real journal
    write inside the python heredoc's OWN body -- that heredoc belongs
    to the python statement, not to git commit (class (c)'s own
    heredoc form, `_is_python_heredoc_opener`), so it must NOT be
    scrubbed; the real write inside it must still be detected by
    class (d)."""
    cmd = (
        'git commit -m "x" && python - <<\'PY\'\n'
        f'with open("{_JOURNAL_TARGET}", "a") as f:\n'
        '    f.write("x")\n'
        "PY"
    )
    exit_code, output = hygiene_gate.decide(_bash_payload(cmd))
    assert exit_code == 0
    hso = output["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == hygiene_gate.MSG_JOURNAL_BLOCK


def test_heredoc_scrub_only_applies_when_git_commit_present():
    """No `git commit` in the command at all -- _strip_commit_messages
    returns the command completely unchanged (early return), the
    heredoc-scrub machinery never even runs."""
    cmd = f"cat <<EOF\nSee {_JOURNAL_TARGET} here\nEOF"
    stripped = hygiene_gate._strip_commit_messages(cmd)
    assert stripped == cmd


def test_heredoc_nested_heredoc_documented_residual_not_crashing():
    """Nested heredocs (one heredoc's body containing another opener)
    are a documented residual limitation, not specially handled --
    this must not crash regardless of exactly which portion ends up
    scrubbed."""
    cmd = (
        "git commit -F - <<OUTER\n"
        "outer body start\n"
        "cat <<INNER\n"
        f"mentions {_JOURNAL_TARGET}\n"
        "INNER\n"
        "outer body end\n"
        "OUTER"
    )
    # Must not raise; the exact scrub boundary on nested heredocs is not
    # asserted (documented residual), only that decide() stays callable.
    exit_code, _ = hygiene_gate.decide(_bash_payload(cmd))
    assert exit_code == 0


def test_heredoc_body_redirect_stderr_still_warns_class_b_documented_residual():
    """Documented residual: a literal ` 2>&1` INSIDE a git-commit
    heredoc body still fires the class-(b) WARN -- classes (a)/(b)/(c)
    are evaluated against the RAW, un-scrubbed command; only class (d)
    consults _strip_commit_messages's output."""
    cmd = "git commit -F - <<EOF\nRun the tests 2>&1 and check the output.\nEOF"
    exit_code, output = hygiene_gate.decide(_bash_payload(cmd))
    assert exit_code == 0
    ctx = output["hookSpecificOutput"]["additionalContext"]
    assert hygiene_gate.MSG_REDIRECT_STDERR in ctx
