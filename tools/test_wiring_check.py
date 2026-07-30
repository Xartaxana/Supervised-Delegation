"""Tests for tools/wiring_check.py (D-0092/D-0093 -- generalized host-
wiring checker). All checks run subprocess `git` calls, so every test
builds an isolated tmp-repo fixture (`git init` in tmp_path) rather than
touching this repo's own .git; tests never assume anything about the
state of the OS repo they happen to run inside.

Boundaries covered per the task's DoD: a hook committed at mode 100644
(the class F-53/D-0093 exists to catch), a missing hook file, an
adoption-ledger "adopt" row with no live wiring behind it, and a
corrupt/unreadable ledger failing OPEN (a WARN, not a crash).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wiring_check  # noqa: E402


def _git(args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "host_repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)
    # An initial commit so ls-files/log have something to operate against.
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "README.md"], root)
    _git(["commit", "-q", "-m", "init"], root)
    return root


def _write_settings(root: Path, commands: list):
    settings_dir = root / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": c} for c in commands]}
            ]
        }
    }
    (settings_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")


def _add_githook(root: Path, name: str, executable: bool):
    githooks = root / ".githooks"
    githooks.mkdir(exist_ok=True)
    hook_path = githooks / name
    hook_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _git(["add", str(hook_path.relative_to(root))], root)
    if executable:
        _git(["update-index", "--chmod=+x", str(hook_path.relative_to(root))], root)
    _git(["commit", "-q", "-m", f"add {name}"], root)


# ---------------------------------------------------------------------
# check_git_hooks_path
# ---------------------------------------------------------------------


def test_hookspath_unset_is_an_issue(repo):
    issues = wiring_check.check_git_hooks_path(repo)
    assert issues == ["core.hooksPath not set"]


def test_hookspath_correct_no_issue(repo):
    _git(["config", "--local", "core.hooksPath", ".githooks"], repo)
    issues = wiring_check.check_git_hooks_path(repo)
    assert issues == []


def test_hookspath_wrong_target_is_an_issue(repo):
    _git(["config", "--local", "core.hooksPath", "some/other/dir"], repo)
    issues = wiring_check.check_git_hooks_path(repo)
    assert len(issues) == 1
    assert "does not resolve to" in issues[0]


# ---------------------------------------------------------------------
# check_required_hooks -- the mode-100644 boundary (F-53/D-0093)
# ---------------------------------------------------------------------


def test_required_hooks_missing_entirely(repo):
    issues = wiring_check.check_required_hooks(repo)
    # Both "file missing" AND "untracked in index" fire independently
    # for each of the two required hooks -- 4 issues total.
    assert any("hook file missing: .githooks/pre-commit" in i for i in issues)
    assert any("hook file missing: .githooks/commit-msg" in i for i in issues)
    assert any("pre-commit untracked" in i for i in issues)
    assert any("commit-msg untracked" in i for i in issues)


def test_required_hooks_committed_mode_100644_is_an_issue(repo):
    # The exact boundary this check exists for: a hook file present and
    # tracked, but committed WITHOUT the executable bit -- a dead gate
    # on a Linux clone even though it looks fine on Windows/NTFS.
    _add_githook(repo, "pre-commit", executable=False)
    _add_githook(repo, "commit-msg", executable=False)
    issues = wiring_check.check_required_hooks(repo)
    assert any("pre-commit" in i and "100644" in i for i in issues)
    assert any("commit-msg" in i and "100644" in i for i in issues)
    assert not any("missing" in i for i in issues)
    assert not any("untracked" in i for i in issues)


def test_required_hooks_committed_mode_100755_is_clean(repo):
    _add_githook(repo, "pre-commit", executable=True)
    _add_githook(repo, "commit-msg", executable=True)
    issues = wiring_check.check_required_hooks(repo)
    assert issues == []


def test_required_hooks_file_present_but_not_tracked(repo):
    # On disk, never git-added -- must report "untracked", NOT a mode
    # issue (there is no index entry to have a mode at all).
    githooks = repo / ".githooks"
    githooks.mkdir()
    (githooks / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    (githooks / "commit-msg").write_text("#!/bin/sh\n", encoding="utf-8")
    issues = wiring_check.check_required_hooks(repo)
    assert any("pre-commit untracked" in i for i in issues)
    assert any("commit-msg untracked" in i for i in issues)
    assert not any("missing" in i for i in issues)


# ---------------------------------------------------------------------
# check_harness_hooks
# ---------------------------------------------------------------------


def test_harness_hooks_missing_file_is_an_issue(repo):
    _write_settings(repo, ["python tools/nonexistent_hook.py"])
    issues = wiring_check.check_harness_hooks(repo)
    assert issues == ["hook file not found: tools/nonexistent_hook.py"]


def test_harness_hooks_existing_file_is_clean(repo):
    (repo / "tools").mkdir()
    (repo / "tools" / "my_hook.py").write_text("pass\n", encoding="utf-8")
    _write_settings(repo, ["python tools/my_hook.py"])
    issues = wiring_check.check_harness_hooks(repo)
    assert issues == []


def test_harness_hooks_no_settings_file_is_an_issue(repo):
    issues = wiring_check.check_harness_hooks(repo)
    assert len(issues) == 1
    assert "not readable" in issues[0]


def test_harness_hooks_invalid_json_is_an_issue(repo):
    settings_dir = repo / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text("{not valid json", encoding="utf-8")
    issues = wiring_check.check_harness_hooks(repo)
    assert len(issues) == 1
    assert "not valid JSON" in issues[0]


def test_harness_hooks_unparsed_command_form_is_an_issue(repo):
    _write_settings(repo, ["python3 tools/my_hook.py --flag"])
    issues = wiring_check.check_harness_hooks(repo)
    assert issues == ["unparsed hook command: python3 tools/my_hook.py --flag"]


def test_harness_hooks_dedupes_repeated_filename(repo):
    _write_settings(repo, ["python tools/missing.py", "python tools/missing.py"])
    issues = wiring_check.check_harness_hooks(repo)
    assert issues == ["hook file not found: tools/missing.py"]


# ---------------------------------------------------------------------
# check_untracked_enforcement_files
# ---------------------------------------------------------------------


def test_untracked_extra_file_under_githooks_is_an_issue(repo):
    _add_githook(repo, "pre-commit", executable=True)
    (repo / ".githooks" / "stray-script.sh").write_text("echo hi\n", encoding="utf-8")
    issues = wiring_check.check_untracked_enforcement_files(repo)
    assert issues == ["untracked enforcement file: .githooks/stray-script.sh"]


def test_untracked_no_extra_files_is_clean(repo):
    _add_githook(repo, "pre-commit", executable=True)
    issues = wiring_check.check_untracked_enforcement_files(repo)
    assert issues == []


def test_untracked_no_githooks_dir_is_clean(repo):
    # Absence of .githooks/ entirely is check_required_hooks's job to
    # report, not this check's.
    issues = wiring_check.check_untracked_enforcement_files(repo)
    assert issues == []


# ---------------------------------------------------------------------
# check_skills_casing -- a case-only mismatch on "SKILL.md" (a
# case-insensitive filesystem silently no-ops `git add` on a
# case-only rename)
# ---------------------------------------------------------------------


def _add_skill(root: Path, relpath: str, content: str = "---\nname: x\n---\nbody\n"):
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(["add", str(path.relative_to(root))], root)
    _git(["commit", "-q", "-m", f"add {relpath}"], root)


def test_skills_casing_no_skills_dir_is_clean(repo):
    issues = wiring_check.check_skills_casing(repo)
    assert issues == []


def test_skills_casing_not_a_git_repo_one_issue_never_raises(tmp_path):
    # A directory that is not a git repo at all -- git ls-files fails
    # (non-zero exit), fails OPEN to exactly one issue string, never
    # raises.
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    issues = wiring_check.check_skills_casing(not_a_repo)
    assert len(issues) == 1
    assert "cannot verify" in issues[0]


def test_skills_casing_git_missing_or_timeout_one_issue_never_raises(repo, monkeypatch):
    monkeypatch.setattr(wiring_check, "_run_git", lambda args, root: None)
    issues = wiring_check.check_skills_casing(repo)
    assert len(issues) == 1
    assert "cannot verify" in issues[0]


def test_skills_casing_reference_md_and_bak_ignored(repo):
    _add_skill(repo, ".claude/skills/onboarding/reference.md")
    _add_skill(repo, ".claude/skills/onboarding/SKILL.md.bak")
    _add_skill(repo, ".claude/skills/onboarding/SKILL.md")
    issues = wiring_check.check_skills_casing(repo)
    assert issues == []


def test_skills_casing_correct_skill_md_no_issue(repo):
    _add_skill(repo, ".claude/skills/onboarding/SKILL.md")
    issues = wiring_check.check_skills_casing(repo)
    assert issues == []


def test_skills_casing_lowercase_skill_md_is_an_issue(repo):
    _add_skill(repo, ".claude/skills/onboarding/skill.md")
    issues = wiring_check.check_skills_casing(repo)
    assert len(issues) == 1
    assert "onboarding/skill.md" in issues[0]
    assert "SKILL.md" in issues[0]


def test_skills_casing_mixed_case_skill_md_is_an_issue(repo):
    _add_skill(repo, ".claude/skills/permission-audit/Skill.md")
    issues = wiring_check.check_skills_casing(repo)
    assert len(issues) == 1
    assert "permission-audit/Skill.md" in issues[0]


def test_skills_casing_untracked_skill_is_invisible_documented_limit(repo):
    # An untracked skill file (never git-added) is invisible to this
    # check by construction -- git ls-files only reports tracked paths.
    path = repo / ".claude" / "skills" / "onboarding" / "skill.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("body", encoding="utf-8")
    issues = wiring_check.check_skills_casing(repo)
    assert issues == []


def test_skills_casing_issue_text_is_ascii_only(repo):
    _add_skill(repo, ".claude/skills/onboarding/skill.md")
    issues = wiring_check.check_skills_casing(repo)
    assert len(issues) == 1
    issues[0].encode("ascii")  # raises UnicodeEncodeError if not ASCII-only


# ---------------------------------------------------------------------
# check_adoption_ledger (D-0092) -- adopt-row-without-live-wiring
# boundary, and the fail-open-on-corrupt-ledger boundary
# ---------------------------------------------------------------------


_LEDGER_TEMPLATE = """# Adoption Ledger

| Kit mechanism | Status | Basis / trigger |
|---|---|---|
| Mechanism gate + symmetry map (`tools/mechanism_gate.py`, `.githooks/commit-msg`, `docs/SIBLING_MAP.md`) | adopt | |
| Skills (`.claude/skills/*`) | adopt | |
"""


def test_ledger_adopt_row_without_live_git_wiring_warns(repo):
    (repo / "ADOPTION_LEDGER.md").write_text(_LEDGER_TEMPLATE, encoding="utf-8")
    git_issues = ["core.hooksPath not set"]
    issues = wiring_check.check_adoption_ledger(repo, git_issues, [])
    assert len(issues) == 1
    assert "Mechanism gate" in issues[0]
    assert "adopt" in issues[0]
    # The "Skills" row is not reconciled at all -- deliberately out of
    # this check's narrow scope (see module docstring).
    assert not any("Skills" in i for i in issues)


def test_ledger_adopt_row_with_clean_git_wiring_no_warn(repo):
    (repo / "ADOPTION_LEDGER.md").write_text(_LEDGER_TEMPLATE, encoding="utf-8")
    issues = wiring_check.check_adoption_ledger(repo, [], [])
    assert issues == []


def test_ledger_absent_is_not_an_issue(repo):
    issues = wiring_check.check_adoption_ledger(repo, ["core.hooksPath not set"], [])
    assert issues == []


def test_ledger_non_adopt_status_not_reconciled(repo):
    text = (
        "| Kit mechanism | Status | Basis / trigger |\n"
        "|---|---|---|\n"
        "| Mechanism gate + symmetry map (`.githooks/commit-msg`) | deferred(x) | not yet |\n"
    )
    (repo / "ADOPTION_LEDGER.md").write_text(text, encoding="utf-8")
    issues = wiring_check.check_adoption_ledger(repo, ["core.hooksPath not set"], [])
    assert issues == []


def test_ledger_broken_encoding_fails_open_with_warn(repo):
    # Invalid UTF-8 bytes -- read_text(encoding="utf-8") raises
    # UnicodeDecodeError; must fail OPEN to a WARN, not propagate.
    (repo / "ADOPTION_LEDGER.md").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    issues = wiring_check.check_adoption_ledger(repo, ["core.hooksPath not set"], [])
    assert len(issues) == 1
    assert "not readable" in issues[0]


def test_ledger_harness_keyword_row_reconciled_against_harness_issues(repo):
    text = (
        "| Kit mechanism | Status | Basis / trigger |\n"
        "|---|---|---|\n"
        "| Tier verification / SessionStart (`tools/session_context.py`) | adopt | |\n"
    )
    (repo / "ADOPTION_LEDGER.md").write_text(text, encoding="utf-8")
    issues = wiring_check.check_adoption_ledger(repo, [], ["hook file not found: tools/x.py"])
    assert len(issues) == 1
    assert "harness-hooks wiring" in issues[0]


# ---------------------------------------------------------------------
# check_wiring() aggregation + never-raises contract
# ---------------------------------------------------------------------


def test_check_wiring_all_clean_ok_true(repo):
    _git(["config", "--local", "core.hooksPath", ".githooks"], repo)
    _add_githook(repo, "pre-commit", executable=True)
    _add_githook(repo, "commit-msg", executable=True)
    _write_settings(repo, [])
    result = wiring_check.check_wiring(repo)
    assert result == {"ok": True, "issues": []}


def test_check_wiring_aggregates_multiple_issue_sources(repo):
    # Nothing configured at all -- hooksPath unset, hooks missing,
    # no settings.json.
    result = wiring_check.check_wiring(repo)
    assert result["ok"] is False
    assert any("core.hooksPath not set" in i for i in result["issues"])
    assert any("not readable" in i for i in result["issues"])


def test_check_wiring_never_raises_on_totally_empty_dir(tmp_path):
    # Not even a git repo -- every git subprocess call fails to run
    # meaningfully; must degrade to issue strings, never an exception.
    empty = tmp_path / "not_a_repo"
    empty.mkdir()
    result = wiring_check.check_wiring(empty)
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert result["issues"]


# ---------------------------------------------------------------------
# CLI form
# ---------------------------------------------------------------------

SCRIPT = Path(__file__).resolve().parent / "wiring_check.py"


def test_cli_exit_0_when_clean(repo, monkeypatch):
    _git(["config", "--local", "core.hooksPath", ".githooks"], repo)
    _add_githook(repo, "pre-commit", executable=True)
    _add_githook(repo, "commit-msg", executable=True)
    _write_settings(repo, [])
    monkeypatch.setattr(wiring_check, "repo_root", lambda: repo)
    exit_code = wiring_check.main(["--check"])
    assert exit_code == 0


def test_cli_exit_1_when_issues(repo, monkeypatch):
    monkeypatch.setattr(wiring_check, "repo_root", lambda: repo)
    exit_code = wiring_check.main(["--check"])
    assert exit_code == 1


def test_cli_subprocess_smoke_runs_against_real_repo():
    # Smoke: the script runs standalone against ITS OWN real host repo
    # (this toolkit) without crashing, exits 0 or 1 (never anything
    # else, never a traceback on stderr).
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode in (0, 1)
    assert "Traceback" not in result.stderr


# ---------------------------------------------------------------------
# check_wiring(skip=...) -- a review fix: source-mode auditing
# routes through check_wiring's own aggregation instead of a hand-
# inlined duplicate, so a future check added to check_wiring() is
# never silently missing from --mode source.
# ---------------------------------------------------------------------


def test_check_wiring_default_skip_is_byte_identical_to_before(repo):
    # Regression pin for the skip parameter's default: calling
    # check_wiring(root) with no `skip` argument at all (positional
    # form AND the explicit empty-set form) reproduces exactly the
    # pre-existing aggregation.
    _git(["config", "--local", "core.hooksPath", ".githooks"], repo)
    _add_githook(repo, "pre-commit", executable=True)
    _add_githook(repo, "commit-msg", executable=True)
    _write_settings(repo, [])
    assert wiring_check.check_wiring(repo) == {"ok": True, "issues": []}
    assert wiring_check.check_wiring(repo, skip=frozenset()) == {"ok": True, "issues": []}


def test_check_wiring_skip_git_hooks_path_omits_that_check_only(repo):
    # hooksPath deliberately left unset (would normally be an issue)
    # but named in `skip` -- every OTHER check still runs and can
    # still report its own issues.
    _write_settings(repo, ["python tools/nonexistent_hook.py"])
    result = wiring_check.check_wiring(repo, skip=frozenset({"check_git_hooks_path"}))
    assert not any("hooksPath" in i for i in result["issues"])
    assert any("nonexistent_hook.py" in i for i in result["issues"])
    assert result["ok"] is False


def test_check_wiring_skip_unknown_name_is_a_silent_no_op(repo):
    # An unrecognized name in `skip` matches nothing in check_wiring's
    # own dispatch -- fails open the same as every check_* function's
    # own contract, never raises, never skips anything real.
    _git(["config", "--local", "core.hooksPath", ".githooks"], repo)
    _add_githook(repo, "pre-commit", executable=True)
    _add_githook(repo, "commit-msg", executable=True)
    _write_settings(repo, [])
    result = wiring_check.check_wiring(repo, skip=frozenset({"check_does_not_exist"}))
    assert result == {"ok": True, "issues": []}


# ---------------------------------------------------------------------
# --host-root / --kit-root / --mode (the installed-vs-source root-confusion finding:
# `python toolkit/tools/wiring_check.py --check` run from the staff
# repo used to treat toolkit/ as an installed HOST root and report a
# spurious core.hooksPath mismatch against the staff repo's OWN
# .githooks -- there was no way to say "this root is the kit's source
# tree, not an installed host" from the CLI at all.)
#
# All printed diagnostic strings are plain ASCII (critic fix #1 --
# see the module docstring's CLI MESSAGE ENCODING section), so no
# special PYTHONIOENCODING handling is needed for these assertions;
# the dedicated codepage-survival probes further below exercise the
# narrow-codepage boundary explicitly.
# ---------------------------------------------------------------------

KIT_ROOT = SCRIPT.resolve().parent.parent  # toolkit/, this repo's own kit source tree


def test_default_invocation_unchanged_on_a_real_installed_host(repo, monkeypatch):
    # DoD (a): a call with NONE of the new flags, against a root that
    # genuinely looks like an installed host (.githooks + .claude
    # present, hooksPath configured, hooks committed executable),
    # reproduces today's exact stdout/exit ("WIRING: OK", 0) -- no new
    # diagnostic banner sneaks in just because --mode defaults to
    # "installed" now. Uses monkeypatched repo_root() (the same
    # pattern as the pre-existing test_cli_exit_0/1_when_* tests) so
    # this is a true zero-flags in-process call, not merely a
    # subprocess whose cwd is ignored by repo_root().
    _git(["config", "--local", "core.hooksPath", ".githooks"], repo)
    _add_githook(repo, "pre-commit", executable=True)
    _add_githook(repo, "commit-msg", executable=True)
    _write_settings(repo, [])
    monkeypatch.setattr(wiring_check, "repo_root", lambda: repo)
    exit_code = wiring_check.main([])
    assert exit_code == 0


def test_default_invocation_pins_exact_real_toolkit_output():
    # The literal regression pin: running the unmodified CLI form
    # against THIS repo's real kit tree (toolkit/, which does carry
    # its own .githooks/.claude) must emit exactly today's known
    # output shape -- the P2 bug's own symptom (a hooksPath mismatch
    # against the ENCLOSING staff repo's config) is a pre-existing
    # fact this task deliberately leaves unchanged for default/
    # installed-mode calls; only --mode source is a legal escape hatch.
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "source mode" not in result.stdout
    assert "does not look like an installed host" not in result.stdout
    if result.returncode == 1:
        assert "does not resolve to" in result.stdout


def test_mode_source_on_real_toolkit_tree_is_clean_and_names_skips():
    # DoD (b): --mode source against the real toolkit/ tree from this
    # repo. Every check except check_git_hooks_path already passes
    # clean against this tree on its own merits (hooks committed
    # executable, settings.json's hook commands all resolve, no
    # untracked .githooks/ cruft, no ADOPTION_LEDGER.md shipped in
    # source) -- so skipping the one check that is genuinely
    # host-install-only makes the whole run clean, and the run must
    # say plainly which check(s) it skipped rather than silently
    # dropping a would-be finding.
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "source"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "source mode" in result.stdout
    assert "check_git_hooks_path" in result.stdout
    assert "WIRING: OK" in result.stdout
    assert "does not resolve to" not in result.stdout


def test_mode_source_explicit_kit_root_matches_default(tmp_path):
    # --kit-root overrides the default guess; pointed explicitly at the
    # same real toolkit/ tree, must reproduce the same clean result.
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "source", "--kit-root", str(KIT_ROOT)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "WIRING: OK" in result.stdout


def test_mode_installed_wrong_root_gets_loud_diagnostic_and_nonzero_exit(tmp_path):
    # DoD (c): --mode installed (explicit or default) against a
    # --host-root with NEITHER .githooks NOR .claude -- the exact
    # signature of "someone pointed this at a kit source checkout, or
    # any other non-host directory, by mistake" -- prints the loud
    # diagnostic BEFORE the normal report and exits non-zero
    # (a caller error, never reported as "OK").
    bad_root = tmp_path / "not_a_host"
    bad_root.mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "installed", "--host-root", str(bad_root)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert "does not look like an installed host" in result.stdout
    assert "--mode source" in result.stdout
    # The normal report still runs and follows the diagnostic.
    assert "WIRING:" in result.stdout


def test_mode_installed_wrong_root_diagnostic_forces_nonzero_even_if_hypothetically_clean(monkeypatch, tmp_path):
    # Defensive boundary: the diagnostic's non-zero exit must not rely
    # on check_wiring() also finding issues (it always will in
    # practice for a truly empty dir, but the forcing is explicit, not
    # incidental) -- patch check_wiring itself to report clean and
    # confirm main() still returns 1.
    bad_root = tmp_path / "not_a_host_either"
    bad_root.mkdir()
    monkeypatch.setattr(wiring_check, "check_wiring", lambda root: {"ok": True, "issues": []})
    exit_code = wiring_check.main(["--mode", "installed", "--host-root", str(bad_root)])
    assert exit_code == 1


def test_mode_installed_right_root_no_diagnostic(repo):
    # The mirror boundary: a --host-root that DOES have .githooks or
    # .claude (even if wiring itself is incomplete) never gets the
    # wrong-root diagnostic -- only the missing-BOTH-dirs signature
    # triggers it.
    (repo / ".claude").mkdir()
    exit_code = wiring_check.main(["--mode", "installed", "--host-root", str(repo)])
    assert exit_code == 1  # still dirty (no hooksPath etc.) -- just not the wrong-root case


def test_host_root_flag_checks_named_root_not_cwd(repo, tmp_path, monkeypatch):
    # DoD (d): --host-root must be the root actually audited, not
    # whatever repo_root()/cwd would otherwise guess. Point repo_root()
    # itself at an unrelated, totally-empty directory (never
    # containing anything) while passing a properly-wired --host-root
    # fixture; a clean result proves the fixture path was used, not
    # the decoy.
    decoy = tmp_path / "decoy_unrelated"
    decoy.mkdir()
    monkeypatch.setattr(wiring_check, "repo_root", lambda: decoy)
    _git(["config", "--local", "core.hooksPath", ".githooks"], repo)
    _add_githook(repo, "pre-commit", executable=True)
    _add_githook(repo, "commit-msg", executable=True)
    _write_settings(repo, [])
    exit_code = wiring_check.main(["--mode", "installed", "--host-root", str(repo)])
    assert exit_code == 0


def test_kit_root_flag_checks_named_root_not_cwd(repo, tmp_path, monkeypatch):
    # Same as above, mirrored for --kit-root / --mode source: point
    # repo_root() at an empty decoy, pass --kit-root at a fixture repo
    # shaped so every source-mode-relevant check is clean, confirm the
    # fixture (not the decoy) is what got audited.
    decoy = tmp_path / "decoy_unrelated_2"
    decoy.mkdir()
    monkeypatch.setattr(wiring_check, "repo_root", lambda: decoy)
    _add_githook(repo, "pre-commit", executable=True)
    _add_githook(repo, "commit-msg", executable=True)
    _write_settings(repo, [])
    exit_code = wiring_check.main(["--mode", "source", "--kit-root", str(repo)])
    assert exit_code == 0


def test_existing_tests_untouched_still_exercise_check_wiring_directly(repo):
    # DoD (e) sanity: the library-level check_wiring()/check_* entry
    # points used by every pre-existing test above are untouched by
    # the CLI-layer --mode/--host-root/--kit-root addition.
    _git(["config", "--local", "core.hooksPath", ".githooks"], repo)
    _add_githook(repo, "pre-commit", executable=True)
    _add_githook(repo, "commit-msg", executable=True)
    _write_settings(repo, [])
    assert wiring_check.check_wiring(repo) == {"ok": True, "issues": []}


# ---------------------------------------------------------------------
# Ignored-flag warnings (critic fix #3): --kit-root in --mode installed
# and --host-root in --mode source are accepted (not an argparse
# error) but produce one ASCII warning line naming the ignored flag,
# so a caller never silently believes an unused flag took effect.
# ---------------------------------------------------------------------


def test_kit_root_ignored_in_installed_mode_warns(repo, capsys):
    (repo / ".claude").mkdir()  # avoid also tripping the wrong-root diagnostic
    wiring_check.main(
        ["--mode", "installed", "--host-root", str(repo), "--kit-root", str(repo)]
    )
    captured = capsys.readouterr()
    assert "warning: --kit-root is ignored in --mode installed" in captured.out


def test_host_root_ignored_in_source_mode_warns(repo, capsys):
    _add_githook(repo, "pre-commit", executable=True)
    _add_githook(repo, "commit-msg", executable=True)
    _write_settings(repo, [])
    wiring_check.main(
        ["--mode", "source", "--kit-root", str(repo), "--host-root", str(repo)]
    )
    captured = capsys.readouterr()
    assert "warning: --host-root is ignored in --mode source" in captured.out


def test_no_ignored_flag_warning_when_only_relevant_flag_given(repo, capsys):
    # Mirror boundary: passing ONLY the mode-relevant flag prints no
    # ignored-flag warning at all.
    (repo / ".claude").mkdir()
    wiring_check.main(["--mode", "installed", "--host-root", str(repo)])
    captured = capsys.readouterr()
    assert "is ignored" not in captured.out


# ---------------------------------------------------------------------
# argparse contract (critic fix #4): the old hand-rolled arg handling
# ignored argv entirely; real parsing now means a genuinely
# unrecognized flag is an argparse error (usage + exit 2), not a
# silent no-op -- --check itself keeps working unchanged.
# ---------------------------------------------------------------------


def test_unrecognized_flag_now_exits_2_argparse_contract(capsys):
    with pytest.raises(SystemExit) as exc_info:
        wiring_check.main(["--this-flag-does-not-exist"])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "usage" in captured.err.lower()


def test_check_flag_still_accepted_as_a_no_op(repo, monkeypatch):
    _git(["config", "--local", "core.hooksPath", ".githooks"], repo)
    _add_githook(repo, "pre-commit", executable=True)
    _add_githook(repo, "commit-msg", executable=True)
    _write_settings(repo, [])
    monkeypatch.setattr(wiring_check, "repo_root", lambda: repo)
    assert wiring_check.main(["--check"]) == 0
    assert wiring_check.main([]) == 0


# ---------------------------------------------------------------------
# Console-codepage survival probes (critic fix #1): the actual bug the
# critic proved -- under PYTHONIOENCODING=cp437, neither new
# diagnostic string's Cyrillic bytes could be encoded, so --mode
# source crashed with UnicodeEncodeError before a single check result
# printed; under PYTHONIOENCODING=cp866, the Cyrillic encoded fine but
# the em-dash (U+2014) in the wrong-root diagnostic did not, crashing
# that path instead. Both new strings are now plain ASCII; these are
# LIVE subprocess runs under the exact two codepages that killed the
# previous version, covering both new print sites under both
# codepages (fix the class, not the instance) so a future non-ASCII
# regression in either string is caught immediately, not just the one
# combination that happened to be proven first.
# ---------------------------------------------------------------------


def _narrow_codepage_env(codepage: str) -> dict:
    return dict(os.environ, PYTHONIOENCODING=codepage)


@pytest.mark.parametrize("codepage", ["cp437", "cp866"])
def test_mode_source_survives_narrow_console_codepage(codepage):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "source"],
        capture_output=True,
        text=True,
        timeout=15,
        env=_narrow_codepage_env(codepage),
    )
    assert "Traceback" not in result.stderr
    assert "UnicodeEncodeError" not in result.stderr
    assert result.returncode in (0, 1)
    assert "source mode" in result.stdout


@pytest.mark.parametrize("codepage", ["cp437", "cp866"])
def test_mode_installed_wrong_root_diagnostic_survives_narrow_console_codepage(codepage, tmp_path):
    bad_root = tmp_path / f"not_a_host_{codepage}"
    bad_root.mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "installed", "--host-root", str(bad_root)],
        capture_output=True,
        text=True,
        timeout=15,
        env=_narrow_codepage_env(codepage),
    )
    assert "Traceback" not in result.stderr
    assert "UnicodeEncodeError" not in result.stderr
    assert result.returncode == 1
    assert "does not look like an installed host" in result.stdout
