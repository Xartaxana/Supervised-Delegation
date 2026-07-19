"""Tests for tools/journal_echo.py (the PostToolUse hook that echoes
the journal validator). Style: by the example of
tools/test_tier_echo.py (unit tests of pure logic + a subprocess smoke
of the whole hook via stdin) and tools/test_journal_validator.py (real
tmp_path git repos for git-mode: _git/_init_repo/_write_journal -- the
same scheme).

Ported from HQ 2026-07-20.

Covers the module's DoD literally:
 1. a non-journal path -> silence;
 2. a journal with a clean new line (a git repo with a HEAD) -> silence;
 3. a new line missing category -> JSON with "JOURNAL ECHO: 1", the
    defect text matches the validator's own message;
 4. several defects -> the count and "+K more" beyond 3;
 5. a non-git directory -> the standalone fallback works (the defect
    is still caught);
 6. a malformed payload/missing file -> a silent exit 0;
 7. an append-only violation (editing an old line) -> caught in
    git mode;
 8. non-ASCII in the defect text -> ASCII output on the stderr channel,
    readable on the stdout (JSON) channel.
Adversarial: a giant journal line does not hang (subprocess with a hard
timeout -- a hang fails loudly as TimeoutExpired, instead of the test
itself hanging forever). Boundary tests (rule 6a) for the limits this
module introduces: MAX_HEAD_MESSAGES=3 (exactly 3 -- no suffix; 4 --
"+1 more") and MAX_MESSAGE_LEN=500 (exactly 500 -- not truncated; 500+50
-- truncated to 500), plus GIT_TIMEOUT_SECONDS (the timeout value is
actually passed through to subprocess.run and handled as "no HEAD").

Run from the repo root: python -m pytest tools/test_journal_echo.py -q
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import journal_echo  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "journal_echo.py"


# ---------------------------------------------------------------------
# helpers -- journal lines (by the example of test_journal_validator._line)
# ---------------------------------------------------------------------


def _line(event="delegated", ts="2026-07-10T08:00:00", agent="builder",
          category="implementation", notes="note",
          worker_ref="cli:2026-07-10T08:00:00", **kw) -> str:
    obj = {"ts": ts, "event": event, "agent": agent, "category": category,
           "notes": notes, "worker_ref": worker_ref}
    obj.update(kw)
    return json.dumps(obj, ensure_ascii=False)


HEAD_LINE = _line(event="delegated", task_id="t-001", model="sonnet")
HEAD_TEXT = HEAD_LINE + "\n"


# ---------------------------------------------------------------------
# helpers -- real git repos (by the example of test_journal_validator._git/_init_repo)
# ---------------------------------------------------------------------


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
    """git-init, writes journal_path with text, commits -- HEAD is now =
    text. Returns the journal file's path."""
    _init_repo(root)
    _write_journal(root, text)
    _git(root, "add", "logs/routing-log.jsonl")
    _git(root, "commit", "-q", "-m", "seed journal")
    return root / "logs" / "routing-log.jsonl"


# ---------------------------------------------------------------------
# helpers -- running the hook
# ---------------------------------------------------------------------


def _post_tool_use_payload(file_path, tool_name="Edit") -> dict:
    return {
        "session_id": "sess-1",
        "transcript_path": "/x/transcript.jsonl",
        "cwd": ".",
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": str(file_path)},
        "tool_response": {"filePath": str(file_path), "success": True},
        "tool_use_id": "tu-1",
    }


def _run_hook(payload, timeout=10, env=None) -> subprocess.CompletedProcess:
    # env=None -> subprocess.run inherits the current process environment
    # unchanged. TIER ECHO subprocess-level tests pass a MODIFIED env (see
    # _env_with_home) so the CHILD process's Path.home() resolves to a
    # tmp_path sandbox -- monkeypatching journal_echo._projects_root in
    # THIS (parent) process has no effect on the subprocess, since main()
    # runs in a separate Python interpreter (confirmed empirically before
    # writing these tests: Path.home() in a subprocess DOES follow an
    # overridden USERPROFILE/HOME env var on this machine).
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env=env,
    )


# ---------------------------------------------------------------------
# helpers -- TIER ECHO at write time: a fake HOME + subagent transcripts
# ---------------------------------------------------------------------


def _assistant_line(model):
    return {"type": "assistant", "message": {"model": model}}


def _write_agent_transcript(home: Path, agent_id: str, lines,
                            proj="proj-slug", sess="sess-id") -> Path:
    """Writes a transcript at the real on-disk layout:
    <home>/.claude/projects/<proj>/<sess>/subagents/agent-<id>.jsonl."""
    path = home / ".claude" / "projects" / proj / sess / "subagents" / f"agent-{agent_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) if not isinstance(line, str) else line for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


def _env_with_home(home: Path) -> dict:
    """Overrides USERPROFILE/HOME for the CHILD hook process -- Path.home()
    in main() then resolves into the tmp_path/"home" sandbox, not this
    machine's real home directory (see the _run_hook docstring)."""
    env = dict(os.environ)
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    return env


def _parse_stdout_json(stdout: str) -> dict:
    payload = json.loads(stdout)
    hook_output = payload["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PostToolUse"
    return hook_output


# ---------------------------------------------------------------------
# _extract_file_path -- pure logic
# ---------------------------------------------------------------------


def test_extract_file_path_present():
    assert journal_echo._extract_file_path({"tool_input": {"file_path": "/x/y.jsonl"}}) == "/x/y.jsonl"


def test_extract_file_path_missing_tool_input():
    assert journal_echo._extract_file_path({}) is None


def test_extract_file_path_tool_input_not_dict():
    assert journal_echo._extract_file_path({"tool_input": "not-a-dict"}) is None


def test_extract_file_path_missing_file_path_key():
    assert journal_echo._extract_file_path({"tool_input": {}}) is None


def test_extract_file_path_not_a_string():
    assert journal_echo._extract_file_path({"tool_input": {"file_path": 42}}) is None


def test_extract_file_path_empty_string():
    assert journal_echo._extract_file_path({"tool_input": {"file_path": ""}}) is None


# ---------------------------------------------------------------------
# _is_journal_path -- pure logic, both separator styles + boundaries
# ---------------------------------------------------------------------


def test_is_journal_path_forward_slash():
    assert journal_echo._is_journal_path("D:/repo/logs/routing-log.jsonl") is True


def test_is_journal_path_backslash():
    assert journal_echo._is_journal_path("D:\\repo\\logs\\routing-log.jsonl") is True


def test_is_journal_path_mixed_separators():
    assert journal_echo._is_journal_path("D:\\repo/logs\\routing-log.jsonl") is True


def test_is_journal_path_relative_two_components():
    assert journal_echo._is_journal_path("logs/routing-log.jsonl") is True


def test_is_journal_path_different_filename():
    assert journal_echo._is_journal_path("D:/repo/logs/other-log.jsonl") is False


def test_is_journal_path_prefix_collision_not_a_match():
    # "xlogs" is not "logs" component-wise -- not a substring match.
    assert journal_echo._is_journal_path("D:/repo/xlogs/routing-log.jsonl") is False


def test_is_journal_path_single_component_not_enough():
    assert journal_echo._is_journal_path("routing-log.jsonl") is False


def test_is_journal_path_empty_string():
    assert journal_echo._is_journal_path("") is False


# ---------------------------------------------------------------------
# _repo_root -- pure logic
# ---------------------------------------------------------------------


def test_repo_root_is_parent_of_parent(tmp_path):
    journal_path = tmp_path / "logs" / "routing-log.jsonl"
    assert journal_echo._repo_root(str(journal_path)) == tmp_path.resolve()


# ---------------------------------------------------------------------
# build_context -- pure logic, including MAX_HEAD_MESSAGES boundaries
# ---------------------------------------------------------------------


def test_build_context_single_violation_no_suffix():
    ctx = journal_echo.build_context(["line 2: msg one"])
    assert ctx == "JOURNAL ECHO: 1 defect(s) in new lines: line 2: msg one"


def test_build_context_exactly_three_boundary_no_more_suffix():
    ctx = journal_echo.build_context(["m1", "m2", "m3"])
    assert ctx == "JOURNAL ECHO: 3 defect(s) in new lines: m1; m2; m3"
    assert "more" not in ctx


def test_build_context_beyond_boundary_four_adds_one_more():
    ctx = journal_echo.build_context(["m1", "m2", "m3", "m4"])
    assert ctx == "JOURNAL ECHO: 4 defect(s) in new lines: m1; m2; m3; +1 more"


def test_build_context_many_beyond_boundary_counts_correctly():
    msgs = [f"m{i}" for i in range(10)]
    ctx = journal_echo.build_context(msgs)
    assert ctx == "JOURNAL ECHO: 10 defect(s) in new lines: m0; m1; m2; +7 more"


def test_build_context_static_english_template_not_mangled():
    # The static English prefix is a literal, never passed through
    # either sanitizer.
    ctx = journal_echo.build_context(["msg"])
    assert ctx.startswith("JOURNAL ECHO: 1 defect(s) in new lines: ")


def test_build_context_long_message_truncated_via_per_item_sanitize():
    # Default ascii_only=False (the raw/stdout path) -- still truncated
    # at the same MAX_MESSAGE_LEN ceiling, just without ascii-replace.
    long_msg = "m" * (journal_echo.MAX_MESSAGE_LEN + 100)
    ctx = journal_echo.build_context([long_msg])
    assert ("m" * journal_echo.MAX_MESSAGE_LEN) in ctx
    assert ("m" * (journal_echo.MAX_MESSAGE_LEN + 1)) not in ctx


def test_build_context_default_ascii_only_false_keeps_non_ascii_readable():
    # additionalContext (default -- ascii_only=False) carries RAW (not
    # '?'-mangled) non-ASCII dynamic content -- the coordinator sees
    # readable text.
    ctx = journal_echo.build_context(["message with non-ASCII: café"])
    assert "café" in ctx
    assert "?" not in ctx


def test_build_context_ascii_only_true_replaces_non_ascii_for_stderr():
    # ascii_only=True (used for the stderr duplicate) -- the same
    # dynamic content is ascii-sanitized, as before. The static prefix
    # itself stays as-is (a literal, not dynamic) -- so ctx AS A WHOLE
    # isn't required to be pure ASCII, only the INSERTED dynamic part.
    ctx = journal_echo.build_context(["message with non-ASCII: café"], ascii_only=True)
    assert "café" not in ctx
    assert "?" in ctx


def test_build_context_static_prefix_never_sanitized_in_either_mode():
    # The static English prefix is a literal, never passed through
    # either _raw_sanitize or _ascii_sanitize, in EITHER mode.
    ctx_raw = journal_echo.build_context(["msg"], ascii_only=False)
    ctx_ascii = journal_echo.build_context(["msg"], ascii_only=True)
    prefix = "JOURNAL ECHO: 1 defect(s) in new lines: "
    assert ctx_raw.startswith(prefix)
    assert ctx_ascii.startswith(prefix)


# ---------------------------------------------------------------------
# _raw_sanitize / _ascii_sanitize -- pure logic, including MAX_MESSAGE_LEN boundaries
# ---------------------------------------------------------------------


def test_raw_sanitize_non_ascii_kept_as_is():
    result = journal_echo._raw_sanitize("café")
    assert result == "café"


def test_raw_sanitize_control_chars_stripped():
    result = journal_echo._raw_sanitize("a\x00b\x1fc")
    assert result == "abc"


def test_raw_sanitize_at_max_len_boundary_not_truncated():
    s = "a" * journal_echo.MAX_MESSAGE_LEN
    result = journal_echo._raw_sanitize(s)
    assert result == s
    assert len(result) == journal_echo.MAX_MESSAGE_LEN


def test_raw_sanitize_beyond_max_len_boundary_truncated():
    s = "a" * (journal_echo.MAX_MESSAGE_LEN + 50)
    result = journal_echo._raw_sanitize(s)
    assert len(result) == journal_echo.MAX_MESSAGE_LEN
    assert result == "a" * journal_echo.MAX_MESSAGE_LEN


def test_ascii_sanitize_non_ascii_replaced():
    result = journal_echo._ascii_sanitize("café")
    assert result == "caf?"
    assert result.isascii()


def test_ascii_sanitize_control_chars_stripped():
    result = journal_echo._ascii_sanitize("a\x00b\x1fc")
    assert result == "abc"


def test_ascii_sanitize_at_max_len_boundary_not_truncated():
    s = "a" * journal_echo.MAX_MESSAGE_LEN
    result = journal_echo._ascii_sanitize(s)
    assert result == s
    assert len(result) == journal_echo.MAX_MESSAGE_LEN


def test_ascii_sanitize_beyond_max_len_boundary_truncated():
    s = "a" * (journal_echo.MAX_MESSAGE_LEN + 50)
    result = journal_echo._ascii_sanitize(s)
    assert len(result) == journal_echo.MAX_MESSAGE_LEN
    assert result == "a" * journal_echo.MAX_MESSAGE_LEN


# ---------------------------------------------------------------------
# _get_head_text -- git wiring, including the GIT_TIMEOUT_SECONDS boundary
# ---------------------------------------------------------------------


def test_get_head_text_real_repo_success(tmp_path):
    _seed_committed_journal(tmp_path, HEAD_TEXT)
    assert journal_echo._get_head_text(tmp_path) == HEAD_TEXT


def test_get_head_text_not_a_repo_returns_none(tmp_path):
    # tmp_path is NEVER git-init'ed.
    assert journal_echo._get_head_text(tmp_path) is None


def test_get_head_text_file_not_on_head_returns_none(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "other.txt").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "other.txt")
    _git(tmp_path, "commit", "-q", "-m", "no journal yet")
    assert journal_echo._get_head_text(tmp_path) is None


def test_get_head_text_timeout_returns_none(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs.get("timeout"))
    monkeypatch.setattr(journal_echo.subprocess, "run", fake_run)
    assert journal_echo._get_head_text(tmp_path) is None


def test_get_head_text_passes_configured_timeout(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))
    monkeypatch.setattr(journal_echo.subprocess, "run", fake_run)
    journal_echo._get_head_text(tmp_path)
    assert captured["timeout"] == journal_echo.GIT_TIMEOUT_SECONDS


def test_get_head_text_git_binary_missing_returns_none(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git not found")
    monkeypatch.setattr(journal_echo.subprocess, "run", fake_run)
    assert journal_echo._get_head_text(tmp_path) is None


# ---------------------------------------------------------------------
# main() end-to-end -- subprocess smoke, DoD 1-8 + adversarial
# ---------------------------------------------------------------------


def test_echo_non_journal_path_silent(tmp_path):
    # DoD 1: a non-journal path -> silence, even if the file exists.
    other_file = tmp_path / "not-a-journal.txt"
    other_file.write_text("irrelevant content", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(other_file))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_git_mode_clean_new_line_silent(tmp_path):
    # DoD 2: a journal with a clean new line (a git repo with a HEAD) -> silence.
    journal_path = _seed_committed_journal(tmp_path)
    new_line = _line(event="delegated", ts="2026-07-10T08:10:00", task_id="t-002",
                      model="sonnet", notes="second task, clean")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_git_mode_missing_category_defect(tmp_path):
    # DoD 3: a new line missing category -> JSON "JOURNAL ECHO: 1", the
    # defect text matches the validator's own message. additionalContext
    # (stdout, raw) must carry it READABLE; stderr (the ascii-only
    # duplicate) carries the same (already-ASCII) English text.
    journal_path = _seed_committed_journal(tmp_path)
    bad_line = _line(event="delegated", ts="2026-07-10T08:10:00", task_id="t-002",
                      model="sonnet", category="")
    journal_path.write_text(HEAD_TEXT + bad_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "JOURNAL ECHO: 1 defect(s) in new lines:" in ctx
    assert "'category'" in ctx
    assert "missing/invalid required field" in ctx
    # stdout wire bytes are pure ASCII themselves (ensure_ascii=True
    # escapes non-ASCII into \uXXXX on the wire; JSON parsing recovers
    # the readable text).
    assert result.stdout.isascii()
    assert "'category'" in result.stderr


def test_echo_git_mode_multiple_defects_count_and_more_suffix(tmp_path):
    # DoD 4: several defects -> the count and "+K more" beyond 3.
    journal_path = _seed_committed_journal(tmp_path)
    bad_lines = [
        _line(event="delegated", ts=f"2026-07-10T08:1{i}:00", task_id=f"t-00{i + 2}",
              model="sonnet", notes="")
        for i in range(4)
    ]
    journal_path.write_text(HEAD_TEXT + "".join(l + "\n" for l in bad_lines), encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "JOURNAL ECHO: 4 defect(s) in new lines:" in ctx
    assert "+1 more" in ctx


def test_echo_standalone_fallback_non_git_dir_catches_defect(tmp_path):
    # DoD 5: a non-git directory -> the standalone fallback works (the
    # defect is still caught). tmp_path is NEVER git-init'ed.
    bad_text = _line(event="delegated", ts="2026-07-10T08:00:00", task_id="t-001",
                      model="sonnet", agent="")
    _write_journal(tmp_path, bad_text + "\n")
    journal_path = tmp_path / "logs" / "routing-log.jsonl"
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    assert "JOURNAL ECHO: 1 defect(s)" in hook_output["additionalContext"]
    assert "'agent'" in hook_output["additionalContext"]


def test_echo_standalone_fallback_non_git_dir_clean_silent(tmp_path):
    # The symmetric positive case for the standalone fallback: a clean
    # file in a non-git directory -> silence (not just "the fallback
    # catches a defect", but also "the fallback doesn't false-positive
    # on a clean input").
    _write_journal(tmp_path, HEAD_TEXT)
    journal_path = tmp_path / "logs" / "routing-log.jsonl"
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_malformed_json_payload_silent_exit():
    # DoD 6a: a malformed payload -> a silent exit 0.
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="{not valid json",
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_payload_not_a_dict_silent_exit():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="[1, 2, 3]",
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_missing_file_on_disk_silent_exit(tmp_path):
    # DoD 6b: the path is journal-shaped, but the file isn't on disk -> a silent exit 0.
    missing = tmp_path / "logs" / "routing-log.jsonl"
    result = _run_hook(_post_tool_use_payload(missing))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_git_mode_append_only_violation_caught(tmp_path):
    # DoD 7: editing an EXISTING journal line (an append-only violation)
    # -- caught in git mode.
    journal_path = _seed_committed_journal(tmp_path)
    modified_head_line = _line(event="delegated", ts="2026-07-10T08:00:00", task_id="t-001",
                                model="sonnet", notes="MODIFIED after the fact")
    journal_path.write_text(modified_head_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    assert "append-only" in hook_output["additionalContext"]
    assert "JOURNAL ECHO: 1 defect(s)" in hook_output["additionalContext"]


def test_echo_non_ascii_defect_text_readable_in_stdout_ascii_in_stderr(tmp_path):
    # DoD 8: non-ASCII in the defect text -- TWO channels, TWO different
    # outcomes. event is not a valid enum token, and it contains
    # non-ASCII -- validate_new_lines embeds it into the message via
    # repr() literally (printable non-ASCII is not escaped).
    #  - stdout (additionalContext, JSON, to the coordinator): the
    #    dynamic part stays READABLE (raw) -- json.dumps(ensure_ascii=True)
    #    itself escapes non-ASCII into \uXXXX on the wire, json.loads()
    #    recovers readable text; the wire bytes of stdout itself stay
    #    pure ASCII (the \uXXXX escapes themselves are ASCII).
    #  - stderr (plain text, this machine's console stream): the same
    #    dynamic part is ascii-sanitized -- non-ASCII replaced with '?',
    #    as before.
    journal_path = _seed_committed_journal(tmp_path)
    bad_line = _line(event="tâche_cible", ts="2026-07-10T08:10:00")
    journal_path.write_text(HEAD_TEXT + bad_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0

    assert result.stdout.isascii()  # the stdout wire bytes are pure ASCII
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "JOURNAL ECHO: 1 defect(s) in new lines:" in ctx  # the static part is intact
    assert "tâche_cible" in ctx  # the dynamic part is READABLE after json.loads()

    assert "tâche_cible" not in result.stderr  # the dynamic part is scrubbed in stderr
    assert "?" in result.stderr  # non-ASCII in the dynamic part is replaced with '?'


def test_echo_giant_line_does_not_hang(tmp_path):
    # Adversarial: a giant journal line does not hang the hook. A hard
    # subprocess timeout means TimeoutExpired fails loudly if the code
    # hangs, instead of the test itself hanging forever.
    journal_path = _seed_committed_journal(tmp_path)
    giant_notes = "x" * (2 * 1024 * 1024)  # 2MB single-line payload
    giant_line = _line(event="delegated", ts="2026-07-10T08:10:00", task_id="t-002",
                        model="sonnet", notes=giant_notes)
    journal_path.write_text(HEAD_TEXT + giant_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path), timeout=20)
    assert result.returncode == 0
    # The line is valid (every required field present) -> silence, despite its size.
    assert result.stdout == ""
    assert result.stderr == ""



# =======================================================================
# TIER ECHO at write time (this port's extension) -- pure logic
# =======================================================================


# ---------------------------------------------------------------------
# _extract_declared_word -- pure logic
# ---------------------------------------------------------------------


def test_extract_declared_word_direct_match():
    assert journal_echo._extract_declared_word("sonnet") == "sonnet"


def test_extract_declared_word_substring_in_full_model_id():
    assert journal_echo._extract_declared_word("claude-opus-4-8") == "opus"


def test_extract_declared_word_case_insensitive():
    assert journal_echo._extract_declared_word("Claude-FABLE-5") == "fable"


def test_extract_declared_word_not_a_string():
    assert journal_echo._extract_declared_word(None) is None
    assert journal_echo._extract_declared_word(42) is None


def test_extract_declared_word_empty_string():
    assert journal_echo._extract_declared_word("") is None


def test_extract_declared_word_no_known_word():
    assert journal_echo._extract_declared_word("gpt-4") is None


def test_extract_declared_word_picks_first_known_tier_words_order():
    # "opus-sonnet-hybrid" contains BOTH words as substrings -- the pick
    # order is tier_echo.KNOWN_TIER_WORDS order (haiku, sonnet, opus,
    # fable), NOT the order they appear in the string itself ("opus" is
    # physically earlier in the string, but "sonnet" is earlier in
    # KNOWN_TIER_WORDS).
    assert journal_echo._extract_declared_word("opus-sonnet-hybrid") == "sonnet"


# ---------------------------------------------------------------------
# _projects_root / _find_agent_transcript -- pure logic (monkeypatched)
# ---------------------------------------------------------------------


def test_find_agent_transcript_match(tmp_path, monkeypatch):
    path = _write_agent_transcript(tmp_path, "abc123", [_assistant_line("claude-opus-4-8")])
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    assert journal_echo._find_agent_transcript("abc123") == str(path)


def test_find_agent_transcript_id_with_dashes(tmp_path, monkeypatch):
    path = _write_agent_transcript(tmp_path, "abc-123-xyz", [_assistant_line("claude-opus-4-8")])
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    assert journal_echo._find_agent_transcript("abc-123-xyz") == str(path)


def test_find_agent_transcript_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    assert journal_echo._find_agent_transcript("no-such-id") is None


def test_find_agent_transcript_glob_error_returns_none(monkeypatch):
    class _BoomRoot:
        def glob(self, pattern):
            raise OSError("boom")

    monkeypatch.setattr(journal_echo, "_projects_root", lambda: _BoomRoot())
    assert journal_echo._find_agent_transcript("x") is None


# ---------------------------------------------------------------------
# _collect_tier_events -- pure logic (monkeypatched _projects_root)
# ---------------------------------------------------------------------


def _delegated_obj(**kw):
    obj = {"ts": "2026-07-10T08:10:00", "event": "delegated", "agent": "builder",
            "category": "implementation", "notes": "note", "task_id": "t-002",
            "model": "sonnet", "worker_ref": "agent:abc123"}
    obj.update(kw)
    return json.dumps(obj, ensure_ascii=False)


def test_collect_tier_events_full_match_silent(tmp_path, monkeypatch):
    _write_agent_transcript(tmp_path, "abc123", [_assistant_line("claude-sonnet-5")])
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    events = journal_echo._collect_tier_events([_delegated_obj(model="sonnet")], [])
    assert events == []


def test_collect_tier_events_mismatch(tmp_path, monkeypatch):
    _write_agent_transcript(tmp_path, "abc123", [_assistant_line("claude-opus-4-8")])
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    events = journal_echo._collect_tier_events([_delegated_obj(model="fable")], [])
    assert len(events) == 1
    line_no, kind, declared_word, counts = events[0]
    assert (line_no, kind, declared_word) == (1, "mismatch", "fable")
    assert counts == {"claude-opus-4-8": 1}


def test_collect_tier_events_partial_match_informational(tmp_path, monkeypatch):
    _write_agent_transcript(
        tmp_path, "abc123",
        [_assistant_line("claude-fable-1"), _assistant_line("claude-sonnet-5")],
    )
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    events = journal_echo._collect_tier_events([_delegated_obj(model="fable")], [])
    assert len(events) == 1
    assert events[0][1] == "info"


def test_collect_tier_events_synthetic_excluded_stays_silent(tmp_path, monkeypatch):
    # A transcript with a REAL model (matching the declared tier) plus a
    # synthetic line -- tier_echo.iter_transcript_models' filter must
    # exclude synthetic, or it would break "full match".
    _write_agent_transcript(
        tmp_path, "abc123",
        [_assistant_line("claude-sonnet-5"), {"type": "assistant", "message": {"model": "<synthetic>"}}],
    )
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    events = journal_echo._collect_tier_events([_delegated_obj(model="sonnet")], [])
    assert events == []


def test_collect_tier_events_transcript_not_found_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    events = journal_echo._collect_tier_events([_delegated_obj()], [])
    assert events == []


def test_collect_tier_events_no_declared_word_skips(tmp_path, monkeypatch):
    _write_agent_transcript(tmp_path, "abc123", [_assistant_line("claude-opus-4-8")])
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    events = journal_echo._collect_tier_events([_delegated_obj(model="gpt-4")], [])
    assert events == []


def test_collect_tier_events_event_outside_trigger_set_skipped(tmp_path, monkeypatch):
    _write_agent_transcript(tmp_path, "abc123", [_assistant_line("claude-opus-4-8")])
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    events = journal_echo._collect_tier_events([_delegated_obj(event="decomposable", model="fable")], [])
    assert events == []


def test_collect_tier_events_worker_ref_cli_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    events = journal_echo._collect_tier_events(
        [_delegated_obj(worker_ref="cli:2026-07-10T08:00:00", model="fable")], [])
    assert events == []


def test_collect_tier_events_worker_ref_retro_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    events = journal_echo._collect_tier_events(
        [_delegated_obj(worker_ref="retro:2026-07-10T08:00:00", model="fable")], [])
    assert events == []


def test_collect_tier_events_worker_ref_missing_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    obj = json.loads(_delegated_obj())
    del obj["worker_ref"]
    events = journal_echo._collect_tier_events([json.dumps(obj)], [])
    assert events == []


def test_collect_tier_events_agent_empty_id_boundary_skipped(tmp_path, monkeypatch):
    # Boundary: worker_ref == "agent:" (an empty id) -- the regex requires
    # 1+ characters, does not match -- skipped, not a crash.
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    events = journal_echo._collect_tier_events([_delegated_obj(worker_ref="agent:", model="fable")], [])
    assert events == []


def test_collect_tier_events_agent_id_with_dashes_boundary_matches(tmp_path, monkeypatch):
    _write_agent_transcript(tmp_path, "ab-12-cd", [_assistant_line("claude-opus-4-8")])
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    events = journal_echo._collect_tier_events(
        [_delegated_obj(worker_ref="agent:ab-12-cd", model="fable")], [])
    assert len(events) == 1
    assert events[0][1] == "mismatch"


def test_collect_tier_events_malformed_json_line_skipped_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    events = journal_echo._collect_tier_events(["{not valid json"], [])
    assert events == []


def test_collect_tier_events_line_numbering_accounts_for_head_lines(tmp_path, monkeypatch):
    _write_agent_transcript(tmp_path, "abc123", [_assistant_line("claude-opus-4-8")])
    monkeypatch.setattr(journal_echo, "_projects_root", lambda: tmp_path / ".claude" / "projects")
    head_lines = ["dummy head line 1", "dummy head line 2"]
    events = journal_echo._collect_tier_events([_delegated_obj(model="fable")], head_lines)
    assert events[0][0] == 3  # len(head_lines) + idx(0) + 1


# ---------------------------------------------------------------------
# build_tier_segment -- pure logic, including MAX_TIER_LINES boundaries
# ---------------------------------------------------------------------


def test_build_tier_segment_empty_list():
    assert journal_echo.build_tier_segment([]) == ""


def test_build_tier_segment_mismatch_exact_format():
    ev = (2, "mismatch", "fable", {"claude-opus-4-8": 1})
    seg = journal_echo.build_tier_segment([ev])
    assert seg == "TIER ECHO: line 2 model='fable' vs measured claude-opus-4-8=1 MISMATCH"


def test_build_tier_segment_info_exact_format_no_mismatch_word():
    ev = (2, "info", "fable", {"claude-fable-1": 1, "claude-sonnet-5": 1})
    seg = journal_echo.build_tier_segment([ev])
    assert seg == "TIER ECHO: line 2 measured claude-fable-1=1, claude-sonnet-5=1"
    assert "MISMATCH" not in seg


def test_build_tier_segment_exactly_five_boundary_no_more_suffix():
    events = [(i, "mismatch", "fable", {"claude-opus-4-8": 1}) for i in range(1, 6)]
    seg = journal_echo.build_tier_segment(events)
    assert "more" not in seg
    assert seg.count("TIER ECHO") == 5


def test_build_tier_segment_beyond_boundary_six_adds_one_more():
    events = [(i, "mismatch", "fable", {"claude-opus-4-8": 1}) for i in range(1, 7)]
    seg = journal_echo.build_tier_segment(events)
    assert seg.count("TIER ECHO") == 5
    assert seg.endswith("; +1 more")


def test_build_tier_segment_ascii_only_true_sanitizes_model_name():
    # The static literal "TIER ECHO: line N ..." stays as-is even in
    # ascii_only mode (the same principle as build_context), so the
    # line as a whole isn't required to be pure ASCII -- only the
    # DYNAMIC part (the model name) must be ascii-sanitized.
    ev = (2, "mismatch", "fable", {"modèle-café": 1})
    seg = journal_echo.build_tier_segment([ev], ascii_only=True)
    assert "modèle-café" not in seg
    assert "?" in seg


def test_build_tier_segment_ascii_only_false_keeps_model_name_readable():
    ev = (2, "mismatch", "fable", {"modèle-café": 1})
    seg = journal_echo.build_tier_segment([ev], ascii_only=False)
    assert "modèle-café" in seg


def test_build_tier_segment_static_literal_stays_intact_in_both_modes():
    # "TIER ECHO: line N ..." is a static literal, the same principle as
    # build_context: never passed through a sanitizer, in either mode.
    ev = (2, "mismatch", "fable", {"claude-opus-4-8": 1})
    seg_raw = journal_echo.build_tier_segment([ev], ascii_only=False)
    seg_ascii = journal_echo.build_tier_segment([ev], ascii_only=True)
    assert seg_raw.startswith("TIER ECHO: line 2 model=")
    assert seg_ascii.startswith("TIER ECHO: line 2 model=")


# ---------------------------------------------------------------------
# combine_context -- pure logic
# ---------------------------------------------------------------------


def test_combine_context_only_violations_matches_build_context_output():
    violations = ["line 2: msg one"]
    assert journal_echo.combine_context(violations, []) == journal_echo.build_context(violations)


def test_combine_context_only_tier_events_no_violations_still_prints():
    ev = (2, "mismatch", "fable", {"claude-opus-4-8": 1})
    ctx = journal_echo.combine_context([], [ev])
    assert ctx == journal_echo.build_tier_segment([ev])
    assert "JOURNAL ECHO" not in ctx


def test_combine_context_both_joined_with_semicolon():
    violations = ["line 2: msg one"]
    ev = (3, "mismatch", "fable", {"claude-opus-4-8": 1})
    ctx = journal_echo.combine_context(violations, [ev])
    assert ctx == journal_echo.build_context(violations) + "; " + journal_echo.build_tier_segment([ev])


def test_combine_context_both_empty_yields_empty_string():
    assert journal_echo.combine_context([], []) == ""


# =======================================================================
# TIER ECHO at write time -- subprocess end-to-end (DoD a-h + boundaries)
# =======================================================================


def test_echo_tier_dod_a_full_match_silent(tmp_path):
    # DoD (a): a delegated with agent:<id>, a transcript with one model
    # of the same tier -> silence.
    journal_path = _seed_committed_journal(tmp_path)
    home = tmp_path / "home"
    _write_agent_transcript(home, "abc123", [_assistant_line("claude-sonnet-5")])
    new_line = _line(event="delegated", ts="2026-07-10T08:10:00", task_id="t-002",
                      model="sonnet", worker_ref="agent:abc123", notes="clean tier match")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path), env=_env_with_home(home))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_tier_dod_b_mismatch_fable_declared_opus_measured(tmp_path):
    # DoD (b): fable declared, opus in the transcript -> a MISMATCH line.
    journal_path = _seed_committed_journal(tmp_path)
    home = tmp_path / "home"
    _write_agent_transcript(home, "fbl001", [_assistant_line("claude-opus-4-8")])
    new_line = _line(event="delegated", ts="2026-07-10T08:10:00", task_id="t-002",
                      model="fable", worker_ref="agent:fbl001", notes="mismatch case")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path), env=_env_with_home(home))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert ctx == "TIER ECHO: line 2 model='fable' vs measured claude-opus-4-8=1 MISMATCH"
    assert ctx in result.stderr


def test_echo_tier_dod_c_mid_worker_informational_no_mismatch(tmp_path):
    # DoD (c): a mid-worker -- transcript fable+sonnet with fable
    # declared -> an informational line, no MISMATCH.
    journal_path = _seed_committed_journal(tmp_path)
    home = tmp_path / "home"
    _write_agent_transcript(
        home, "mid001",
        [_assistant_line("claude-fable-1"), _assistant_line("claude-sonnet-5")],
    )
    new_line = _line(event="delegated", ts="2026-07-10T08:10:00", task_id="t-002",
                      model="fable", worker_ref="agent:mid001", notes="mid-worker case")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path), env=_env_with_home(home))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert ctx == "TIER ECHO: line 2 measured claude-fable-1=1, claude-sonnet-5=1"
    assert "MISMATCH" not in ctx


def test_echo_tier_dod_d_worker_ref_cli_skipped_silent(tmp_path):
    # DoD (d), part 1: worker_ref cli:xxx -> skipped without a warning (silence).
    journal_path = _seed_committed_journal(tmp_path)
    new_line = _line(event="accepted", ts="2026-07-10T08:10:00", task_id="t-001",
                      agent="builder", by="opus", witness="tests pass", model="sonnet",
                      worker_ref="cli:2026-07-10T08:10:00", notes="accepted via cli ref")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_tier_dod_d_worker_ref_retro_skipped_silent(tmp_path):
    # DoD (d), part 2: worker_ref retro:xxx -> skipped without a warning.
    journal_path = _seed_committed_journal(tmp_path)
    new_line = _line(event="accepted", ts="2026-07-10T08:10:00", task_id="t-001",
                      agent="builder", by="opus", witness="tests pass", model="sonnet",
                      worker_ref="retro:2026-07-10T08:10:00", notes="accepted via retro ref")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_tier_dod_d_worker_ref_absent_skipped_silent(tmp_path):
    # DoD (d), part 3: worker_ref absent entirely -> skipped without a warning.
    journal_path = _seed_committed_journal(tmp_path)
    obj = {"ts": "2026-07-10T08:10:00", "event": "accepted", "agent": "builder",
           "category": "implementation", "notes": "accepted, no worker_ref field",
           "task_id": "t-001", "by": "opus", "witness": "tests pass", "model": "sonnet"}
    new_line = json.dumps(obj, ensure_ascii=False)
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_tier_dod_e_transcript_not_found_silent(tmp_path):
    # DoD (e): a transcript not found -> silence.
    journal_path = _seed_committed_journal(tmp_path)
    home = tmp_path / "home"  # No transcript is created here at all.
    new_line = _line(event="delegated", ts="2026-07-10T08:10:00", task_id="t-002",
                      model="sonnet", worker_ref="agent:doesnotexist123", notes="clean")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path), env=_env_with_home(home))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_tier_dod_f_form_defect_and_mismatch_together(tmp_path):
    # DoD (f): a form defect + a MISMATCH together -> both in one additionalContext.
    journal_path = _seed_committed_journal(tmp_path)
    home = tmp_path / "home"
    _write_agent_transcript(home, "fbl002", [_assistant_line("claude-opus-4-8")])
    bad_line = _line(event="delegated", ts="2026-07-10T08:10:00", task_id="t-002",
                      model="fable", category="", worker_ref="agent:fbl002",
                      notes="defect and mismatch together")
    journal_path.write_text(HEAD_TEXT + bad_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path), env=_env_with_home(home))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "JOURNAL ECHO: 1 defect(s)" in ctx
    assert "'category'" in ctx
    assert "TIER ECHO: line 2 model='fable' vs measured claude-opus-4-8=1 MISMATCH" in ctx
    # Both segments are joined with "; ".
    assert "; TIER ECHO" in ctx


def test_echo_tier_dod_g_synthetic_lines_not_counted(tmp_path):
    # DoD (g): synthetic lines in the transcript are not counted -- a
    # real model matching the declared tier gives complete silence, not
    # a false mismatch/informational from a counted synthetic line.
    journal_path = _seed_committed_journal(tmp_path)
    home = tmp_path / "home"
    _write_agent_transcript(
        home, "syn001",
        [_assistant_line("claude-sonnet-5"), {"type": "assistant", "message": {"model": "<synthetic>"}}],
    )
    new_line = _line(event="delegated", ts="2026-07-10T08:10:00", task_id="t-002",
                      model="sonnet", worker_ref="agent:syn001", notes="synthetic filtered")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path), env=_env_with_home(home))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_tier_dod_h_more_than_five_tier_lines_shows_more_suffix(tmp_path):
    # DoD (h): >5 tier lines -> "+K more".
    journal_path = _seed_committed_journal(tmp_path)
    home = tmp_path / "home"
    n = 6
    for i in range(n):
        _write_agent_transcript(home, f"agentid{i}", [_assistant_line("claude-opus-4-8")],
                                 proj=f"proj{i}", sess=f"sess{i}")
    new_lines = [
        _line(event="delegated", ts=f"2026-07-10T08:1{i}:00", task_id=f"t-00{2 + i}",
              model="fable", worker_ref=f"agent:agentid{i}", notes=f"mismatch #{i}")
        for i in range(n)
    ]
    journal_path.write_text(HEAD_TEXT + "".join(l + "\n" for l in new_lines), encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path), env=_env_with_home(home))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert ctx.count("TIER ECHO") == 5
    assert "+1 more" in ctx


def test_echo_tier_exactly_five_tier_lines_no_more_suffix(tmp_path):
    # Boundary (rule 6a): EXACTLY 5 tier lines -> no "+more".
    journal_path = _seed_committed_journal(tmp_path)
    home = tmp_path / "home"
    n = 5
    for i in range(n):
        _write_agent_transcript(home, f"agentid{i}", [_assistant_line("claude-opus-4-8")],
                                 proj=f"proj{i}", sess=f"sess{i}")
    new_lines = [
        _line(event="delegated", ts=f"2026-07-10T08:1{i}:00", task_id=f"t-00{2 + i}",
              model="fable", worker_ref=f"agent:agentid{i}", notes=f"mismatch #{i}")
        for i in range(n)
    ]
    journal_path.write_text(HEAD_TEXT + "".join(l + "\n" for l in new_lines), encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path), env=_env_with_home(home))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert ctx.count("TIER ECHO") == 5
    assert "more" not in ctx


def test_echo_tier_worker_ref_agent_id_with_dashes_boundary(tmp_path):
    # Boundary: a dashed id -- the full pipeline (not just
    # _collect_tier_events directly).
    journal_path = _seed_committed_journal(tmp_path)
    home = tmp_path / "home"
    _write_agent_transcript(home, "ab-12-cd", [_assistant_line("claude-opus-4-8")])
    new_line = _line(event="delegated", ts="2026-07-10T08:10:00", task_id="t-002",
                      model="fable", worker_ref="agent:ab-12-cd", notes="dashed id")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path), env=_env_with_home(home))
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    assert "MISMATCH" in hook_output["additionalContext"]


def test_echo_tier_worker_ref_agent_empty_id_boundary_silent(tmp_path):
    # Boundary: worker_ref == "agent:" (an empty id) -- the full
    # pipeline, silence.
    journal_path = _seed_committed_journal(tmp_path)
    new_line = _line(event="delegated", ts="2026-07-10T08:10:00", task_id="t-002",
                      model="fable", worker_ref="agent:", notes="empty agent id")
    journal_path.write_text(HEAD_TEXT + new_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_echo_giant_invalid_line_does_not_hang_and_reports(tmp_path):
    # The same giant size, but the line HAS a defect (empty notes) -- the
    # hook must both not hang and correctly report the defect (the
    # message itself is short -- the notes value isn't embedded into the
    # violation text verbatim, see journal_validator.validate_new_lines).
    journal_path = _seed_committed_journal(tmp_path)
    giant_task_id_holder = "x" * (2 * 1024 * 1024)
    bad_line = json.dumps({
        "ts": "2026-07-10T08:10:00", "event": "delegated", "agent": "builder",
        "category": "implementation", "notes": "",
        "worker_ref": "cli:2026-07-10T08:10:00", "task_id": "t-002", "model": "sonnet",
        "_padding": giant_task_id_holder,
    }, ensure_ascii=False)
    journal_path.write_text(HEAD_TEXT + bad_line + "\n", encoding="utf-8")
    result = _run_hook(_post_tool_use_payload(journal_path), timeout=20)
    assert result.returncode == 0
    hook_output = _parse_stdout_json(result.stdout)
    ctx = hook_output["additionalContext"]
    assert "JOURNAL ECHO: 1 defect(s)" in ctx
    # The defect message itself is short (it complains about the empty
    # 'notes' field, doesn't embed the padding field's value) -- the
    # giant padding does NOT leak into the output.
    assert len(ctx) < 1000
