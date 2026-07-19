# -*- coding: utf-8 -*-
"""Tests for tools/permission_audit.py.

Ported from HQ 2026-07-20.

Covers: matches_allow (the * prefix semantics, a cd prefix breaking a
match), sandbox heuristics (multi-line, $(...), a for loop), the
broad-wildcard detector (refinement b, positive/negative), and the
transcript snapshot (refinement a -- the scan must not see bytes
appended AFTER the snapshot).
"""
from __future__ import annotations

import json
from pathlib import Path

import permission_audit as pa


# --- matches_allow: prefix semantics ---

def test_matches_allow_prefix_star():
    patterns = [("Bash", "git push *")]
    assert pa.matches_allow("Bash", "git push origin main", patterns)
    assert not pa.matches_allow("Bash", "git pull", patterns)


def test_matches_allow_exact_tool_only_no_pattern():
    # a bare tool name with no "(...)" -> pattern == "" -> allows any command for that tool
    patterns = [("WebSearch", "")]
    assert pa.matches_allow("WebSearch", "anything at all", patterns)


def test_matches_allow_cd_prefix_breaks_match():
    # the allowlist pattern starts with "python", but the call starts with
    # "cd dir && python" -- a cd prefix breaks the from-the-start match
    # (command hygiene point 3).
    patterns = [("Bash", "python metrics.py*")]
    assert not pa.matches_allow("Bash", "cd gateway && python metrics.py", patterns)
    assert pa.matches_allow("Bash", "python metrics.py --days 1", patterns)


def test_matches_allow_wrong_tool_no_match():
    patterns = [("PowerShell", "git add *")]
    assert not pa.matches_allow("Bash", "git add -A", patterns)


# --- sandbox_flags: "cannot be statically analyzed" heuristics ---

def test_sandbox_flags_multiline():
    flags = pa.sandbox_flags("echo one\necho two")
    assert any("multi-line" in f for f in flags)


def test_sandbox_flags_command_substitution():
    flags = pa.sandbox_flags('echo "$(date)"')
    assert any("substitution" in f for f in flags)


def test_sandbox_flags_for_loop():
    flags = pa.sandbox_flags("for f in *.txt; do cat $f; done")
    assert any("for...do" in f for f in flags)


def test_sandbox_flags_clean_command_no_flags():
    assert pa.sandbox_flags("git status") == []


# --- is_broad_wildcard / scan_broad_wildcards: refinement (b) ---

def test_is_broad_wildcard_bare_interpreter_positive():
    # a known finding: Bash(python *) in settings.local.json
    reason = pa.is_broad_wildcard("Bash", "python *")
    assert reason is not None
    assert "python" in reason


def test_is_broad_wildcard_code_flag_positive():
    reason = pa.is_broad_wildcard("Bash", "python -c *")
    assert reason is not None
    reason2 = pa.is_broad_wildcard("Bash", "bash -c *")
    assert reason2 is not None


def test_is_broad_wildcard_code_flag_with_open_quote_positive():
    # a real-world shape: "python -c ' *" -- -c with an unclosed opening
    # quote right before the asterisk, the same arbitrary code as a bare
    # "python -c *".
    reason = pa.is_broad_wildcard("Bash", "python -c ' *")
    assert reason is not None


def test_is_broad_wildcard_env_prefix_before_interpreter_positive():
    # "PYTHONUTF8=1 python -c ' *" -- the pattern's head is VAR=val, not
    # the interpreter name; the detector must skip the assignment prefix.
    reason = pa.is_broad_wildcard("Bash", "PYTHONUTF8=1 python -c ' *")
    assert reason is not None


def test_is_broad_wildcard_narrow_pattern_negative():
    # a specific script with a flag after the interpreter -- not bare arbitrary code
    assert pa.is_broad_wildcard("Bash", "python metrics.py *") is None
    assert pa.is_broad_wildcard("Bash", "git push *") is None


def test_is_broad_wildcard_module_flag_positive():
    # `python -m *` lets through an arbitrary MODULE -- the same class of
    # arbitrary execution as -c/-e; a SPECIFIC module (python -m pytest
    # ...) is not a finding.
    assert pa.is_broad_wildcard("Bash", "python -m *") is not None
    assert pa.is_broad_wildcard("Bash", "python -m pytest tools/ gateway/ -q") is None


def test_is_broad_wildcard_non_matching_tool_negative():
    assert pa.is_broad_wildcard("WebFetch", "python *") is None


def test_scan_broad_wildcards_reads_both_settings_files(tmp_path, monkeypatch):
    repo = tmp_path
    claude_dir = repo / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(git fetch *)"]}}), encoding="utf-8")
    (claude_dir / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(python *)", "Bash(git add *)"]}}),
        encoding="utf-8")
    monkeypatch.setattr(pa, "REPO", repo)
    found = pa.scan_broad_wildcards()
    assert len(found) == 1
    fname, tool, pat, reason = found[0]
    assert fname == "settings.local.json"
    assert tool == "Bash"
    assert pat == "python *"


# --- transcript snapshot: refinement (a) ---

def _write_tool_use(path, cmd, ts="2026-07-14T10:00:00Z"):
    line = {
        "timestamp": ts,
        "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}
        ]},
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def test_iter_tool_calls_ignores_bytes_written_after_snapshot(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    transcript = projects / "session.jsonl"
    _write_tool_use(transcript, "echo before-snapshot")
    monkeypatch.setattr(pa, "CLAUDE_PROJECTS", projects)

    snapshot = pa.snapshot_transcripts()
    assert len(snapshot) == 1
    assert snapshot[0][2] == transcript.stat().st_size  # size_at_snapshot is fixed

    # a live session keeps appending to the transcript AFTER the snapshot
    _write_tool_use(transcript, "echo after-snapshot")

    calls = list(pa.iter_tool_calls(None, snapshot=snapshot))
    cmds = [c[4] for c in calls]
    assert "echo before-snapshot" in " ".join(cmds)
    assert not any("after-snapshot" in c for c in cmds)


def test_iter_tool_calls_without_snapshot_sees_full_current_file(tmp_path, monkeypatch):
    # with no explicit snapshot (snapshot=None), iter_tool_calls takes a
    # fresh one itself -- so it sees everything written BEFORE the call.
    projects = tmp_path / "projects"
    projects.mkdir()
    transcript = projects / "session.jsonl"
    _write_tool_use(transcript, "echo one")
    _write_tool_use(transcript, "echo two")
    monkeypatch.setattr(pa, "CLAUDE_PROJECTS", projects)

    calls = list(pa.iter_tool_calls(None))
    cmds = [c[4] for c in calls]
    assert "echo one" in cmds
    assert "echo two" in cmds


# --- collect_suspects: end-to-end assembly ---

def test_collect_suspects_flags_missing_allowlist_match(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": []}}), encoding="utf-8")
    monkeypatch.setattr(pa, "REPO", repo)

    projects = tmp_path / "projects"
    projects.mkdir()
    transcript = projects / "session.jsonl"
    _write_tool_use(transcript, "some-random-tool --flag")
    monkeypatch.setattr(pa, "CLAUDE_PROJECTS", projects)

    suspects, total = pa.collect_suspects(None)
    assert total == 1
    assert len(suspects) == 1
    assert "no allowlist match" in suspects[0][4]


# --- _default_project_key / PROJECT_KEY: generic derivation (this port's addition) ---


def test_default_project_key_replaces_colon_backslash_underscore():
    # Empirically verified against a live ~/.claude/projects listing:
    # colon, backslash, and underscore each become a dash.

    class _FakeResolved:
        def __str__(self):
            return r"D:\Some_Repo"

    class _FakePath:
        def resolve(self):
            return _FakeResolved()

    assert pa._default_project_key(_FakePath()) == "D--Some-Repo"


def test_default_project_key_is_deterministic_for_real_repo_path(tmp_path):
    repo = tmp_path / "a_b" / "c-d"
    repo.mkdir(parents=True)
    key1 = pa._default_project_key(repo)
    key2 = pa._default_project_key(repo)
    assert key1 == key2
    assert "\\" not in key1 and "/" not in key1 and ":" not in key1 and "_" not in key1


class _FakeResolved:
    def __init__(self, s):
        self._s = s

    def __str__(self):
        return self._s


class _FakePath:
    def __init__(self, s):
        self._s = s

    def resolve(self):
        return _FakeResolved(self._s)


def test_default_project_key_dot_and_space_boundaries_deterministic():
    # Slug-boundary check (queue item 1, toolkit-release-v040): the
    # replacement regex is [\\/:_] -> "-" -- it does NOT touch dots or
    # spaces. This is UNVERIFIED AGAINST REAL HARNESS NAMING (the
    # function's own docstring says so); do not treat the asserted value
    # below as "the correct slug" -- it only locks in this function's
    # CURRENT, deterministic output as a regression contract, so a future
    # change to the regex shows up as a diff instead of silently drifting.
    # If a real install's actual ~/.claude/projects/<slug> differs on a
    # dot/space path, the escape hatch is the CLAUDE_PROJECTS env
    # override (_resolve_claude_projects), not "fixing" this regex blind.
    dot_path = r"D:\Repo.With.Dots"
    key_a = pa._default_project_key(_FakePath(dot_path))
    key_b = pa._default_project_key(_FakePath(dot_path))
    assert key_a == key_b  # deterministic
    assert key_a == "D--Repo.With.Dots"  # current behavior: dots pass through

    space_path = r"D:\Repo With Spaces"
    key_c = pa._default_project_key(_FakePath(space_path))
    key_d = pa._default_project_key(_FakePath(space_path))
    assert key_c == key_d  # deterministic
    assert key_c == "D--Repo With Spaces"  # current behavior: spaces pass through


# --- _resolve_claude_projects / CLAUDE_PROJECTS env override (queue item 1) ---


def test_resolve_claude_projects_env_override_takes_full_path(tmp_path, monkeypatch):
    override_dir = tmp_path / "custom_projects_dir"
    monkeypatch.setenv("CLAUDE_PROJECTS", str(override_dir))
    resolved = pa._resolve_claude_projects(pa.REPO)
    assert resolved == override_dir


def test_resolve_claude_projects_no_env_falls_back_to_slug(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECTS", raising=False)
    resolved = pa._resolve_claude_projects(pa.REPO)
    expected = Path.home() / ".claude" / "projects" / pa._default_project_key(pa.REPO)
    assert resolved == expected


def test_claude_projects_env_override_works_end_to_end(tmp_path, monkeypatch):
    # The override is a FULL path, taking effect before the transcripts
    # scan even runs -- wire it the way main() would (CLAUDE_PROJECTS
    # resolved once at import time; here we simulate that by setting the
    # module attribute directly, same pattern the rest of this file uses
    # to point CLAUDE_PROJECTS at a fixture directory) and confirm a
    # transcript placed there is actually read.
    override_dir = tmp_path / "custom_projects_dir"
    override_dir.mkdir()
    transcript = override_dir / "session.jsonl"
    _write_tool_use(transcript, "echo overridden-transcript")
    monkeypatch.setenv("CLAUDE_PROJECTS", str(override_dir))

    resolved = pa._resolve_claude_projects(pa.REPO)
    assert resolved == override_dir
    monkeypatch.setattr(pa, "CLAUDE_PROJECTS", resolved)

    calls = list(pa.iter_tool_calls(None))
    cmds = [c[4] for c in calls]
    assert "echo overridden-transcript" in " ".join(cmds)


# --- check_transcripts_present: warn-on-empty (queue item 1) ---


def test_check_transcripts_present_warns_when_dir_missing(tmp_path, capsys):
    missing = tmp_path / "does_not_exist"
    warned = pa.check_transcripts_present(missing)
    assert warned is True
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert str(missing) in err
    assert "CLAUDE_PROJECTS" in err


def test_check_transcripts_present_warns_when_dir_empty(tmp_path, capsys):
    empty = tmp_path / "empty_projects"
    empty.mkdir()
    warned = pa.check_transcripts_present(empty)
    assert warned is True
    err = capsys.readouterr().err
    assert "WARNING" in err


def test_check_transcripts_present_no_warn_when_populated(tmp_path, capsys):
    populated = tmp_path / "populated_projects"
    populated.mkdir()
    _write_tool_use(populated / "session.jsonl", "echo something")
    warned = pa.check_transcripts_present(populated)
    assert warned is False
    err = capsys.readouterr().err
    assert err == ""

