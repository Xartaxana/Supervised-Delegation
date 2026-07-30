"""wiring_check.py -- generalized host-wiring checker (D-0092/D-0093).

Read-only auditor of whether the kit's enforcement chain is actually
wired into a host repo, independent of what any single mechanism's own
SessionStart output claims. Patterns are drawn from the staff
deployment's wiring channel in tools/session_context.py (its
git_hooks_channel/harness_channel functions) -- this module is a
standalone, more general reimplementation, not an import of that file:
it is meant to run against ANY host repo this kit is installed into,
not just this one, and deliberately carries no dependency on
tools/session_context.py so the two can evolve independently. The
converse dependency direction IS used: tools/session_context.py imports
this module for its single summary WIRING line (see that file's
`wiring_summary_line`).

Division of labor with tools/session_context.py (avoids double-reporting
the same fact two different ways): this module is READ-ONLY -- it never
writes to git config or anywhere else. The one WRITE action in this
area (self-healing an unset core.hooksPath) lives in
tools/session_context.py's `hooks_path_autofix_line`, which runs once
per SessionStart, before this module's checks would otherwise flag the
same gap. Running `python tools/wiring_check.py --check` right after a
SessionStart that just autofixed hooksPath will therefore usually find
it already resolved.

CHECKS (each returns a list of issue strings; empty = that check is
clean):

 (1) check_git_hooks_path -- core.hooksPath resolves to <root>/.githooks.
 (2) check_required_hooks -- pre-commit and commit-msg are both present
     as files AND tracked in the git INDEX with mode 100755 (read via
     `git ls-files -s`, the INDEX, not the filesystem: a hook committed
     as mode 100644 is silently skipped by git on a Linux clone --
     Windows/NTFS carries no meaningful exec bit at all, so this cannot
     be observed via os.stat(); F-53/D-0093). Two independent sub-facts
     per hook, both reportable: untracked (missing from the index
     entirely -- a fresh clone gets NO gate at all, worse than
     non-executable) vs tracked-but-wrong-mode.
 (3) check_harness_hooks -- every "python tools/<file>.py" hook command
     named in .claude/settings.json points to a file that EXISTS.
     Existence only, deliberately no import check (unlike the staff
     deployment's harness_channel): this module runs against arbitrary
     host repos, and importing arbitrary host code as a side effect of
     a read-only auditor is out of scope.
 (4) check_adoption_ledger -- if <root>/ADOPTION_LEDGER.md exists (the
     host's filled-in copy of ADOPTION_LEDGER.template.md, not the
     template itself), every row whose Status cell is exactly "adopt"
     AND whose "Kit mechanism" cell names the git-hooks or
     harness-hooks machinery checks (1)-(3) above already cover is
     cross-checked against the issues those checks found (D-0092: an
     "adopt" row with an open issue on the wiring it claims is a WARN).
     Deliberately NARROW: reconciling an arbitrary ledger row ("Skills",
     "PROCESS docs", ...) against a concrete live fact is not
     well-defined for most rows in the template -- only rows naming
     git-hooks/harness-hooks machinery are reconciled; every other row
     is left alone rather than guessed at (silence there is not a false
     claim). No ADOPTION_LEDGER.md at all is not an error -- most hosts
     running this tool will not carry one yet. A ledger that exists but
     fails to read/parse degrades to ONE WARN naming the failure
     (fail-open: a corrupt ledger must not blank out the git/harness
     issues the checks above it already found).
 (5) check_untracked_enforcement_files -- a file present on disk under
     .githooks/ that git does not track AT ALL (beyond the two required,
     named hooks check (2) already covers by name) is a WARN: it was
     never `git add`-ed, so it is invisible to a fresh clone and does
     nothing there regardless of its content or permissions.
 (6) check_skills_casing -- every file tracked under .claude/skills/
     (via `git ls-files`) whose basename matches "skill.md"
     case-insensitively but is not spelled exactly "SKILL.md" is a WARN:
     on a case-insensitive filesystem, `git add` on a case-only rename
     silently no-ops, so a skill can be authored with the wrong casing
     and never actually land in the index under its real name. A
     CONTENT check (unlike check_git_hooks_path): it runs in BOTH
     --mode installed and --mode source, since it reads the kit's own
     committed tree, not live installation state. An untracked skill
     file is invisible to this check by construction (git ls-files only
     ever sees tracked paths) -- a documented limit, not a bug.

Every check function is self-contained and fails OPEN: a subprocess
call that cannot even run (git missing, timeout) or a file that cannot
be read becomes ONE issue string describing the failure, never an
uncaught exception -- check_wiring() itself has no try/except of its
own because every check it aggregates has already turned its own
failure modes into strings.

FORM (spec-required): a CLI (`python tools/wiring_check.py --check`,
exit 0 when clean / 1 when any issue was found, a human-readable
report on stdout) plus the importable `check_wiring(root) -> dict`
function -- the CLI is a thin wrapper around exactly that function,
so the two forms can never disagree.

CLI ROOT/MODE FLAGS (the installed-vs-source root-confusion finding): the CLI grew
--host-root/--kit-root/--mode on top of the single implicit
`repo_root()` guess, because that guess collapses two different
things a caller might mean by "the root to check":

  - an INSTALLED host repo (has its own configured core.hooksPath,
    its own .githooks/.claude) -- the only case check_git_hooks_path
    was ever meaningful for;
  - the KIT'S OWN SOURCE TREE (e.g. this repo's toolkit/ directory,
    reviewed from the staff repo one level up) -- which is NOT a
    self-contained git repo with its own hooksPath wiring; its git
    config is whatever the ENCLOSING repo happens to have configured,
    which has no reason to point at <kit-root>/.githooks. Running the
    old single-root CLI against a kit source tree therefore produced
    a spurious "core.hooksPath does not resolve to ..." finding that
    looks identical to a real wiring defect (the reported P2 case).

--mode installed (default, byte-identical to the pre-existing
behavior on a real installed host -- DoD (a)) checks --host-root
(default: the old `repo_root()` guess) with every check above.
--mode source checks --kit-root (default: same guess) but SKIPS
check_git_hooks_path -- the one check above that is genuinely about
live installation state rather than the kit tree's own committed
content -- printing which check(s) it skipped instead of silently
omitting the finding. Every other check (hook files present +
git-INDEX modes, harness settings.json referencing real files,
untracked .githooks/ cruft, adoption-ledger reconciliation) is
unaffected by install-vs-source and keeps running in BOTH modes: they
read the kit's own committed tree, which is exactly what a source
review wants checked. In --mode installed, a --host-root that has
NEITHER .githooks NOR .claude is flagged loudly as a probable wrong
invocation (most likely: someone ran the installed-mode default from
inside a kit source checkout) before the normal report, with a
forced-nonzero exit -- a caller diagnosis error, not a clean/dirty
wiring verdict. --kit-root is meaningless in --mode installed (and
--host-root is meaningless in --mode source); passing the
mode-inapplicable flag is not an error, but prints one warning line
naming which flag was ignored, so a caller doesn't silently believe
an unused flag took effect.

CLI MESSAGE ENCODING (fixed after a review of the root-confusion finding above): every
string this module prints is plain ASCII -- no em-dash (U+2014, only
`--`), no non-ASCII punctuation, no other-language text. A prior
version's two new diagnostic strings used Cyrillic and an em-dash;
under a narrow console codepage (verified: PYTHONIOENCODING=cp437 --
neither Cyrillic byte encodes; PYTHONIOENCODING=cp866 -- the
Cyrillic bytes DO encode there, but the em-dash does not) `print()`
raised UnicodeEncodeError before a single check result could be
shown, crashing the whole run on --mode source under cp437 and on
the wrong-root diagnostic under cp866. This module runs on arbitrary
host machines with unknown console codepages, and it is not a
mechanism this repo's own dogfooding session controls the codepage
of -- so every printed string here follows the ASCII-only invariant
already used by tools/session_context.py's own printed lines, not
this repo's (Russian-language) CLAUDE.md/journal convention.

ARGPARSE CONTRACT CHANGE (also from that review): the previous
hand-rolled arg handling ignored argv entirely (`_ = sys.argv[1:]`),
so an unrecognized flag was silently a no-op. Now that argument
parsing does real work (--host-root/--kit-root/--mode), an
unrecognized flag is an argparse error: prints usage to stderr and
exits 2, same as any other Python CLI built on argparse. No caller of
this script is known to depend on the old silent-ignore behavior for
any flag other than --check (checked at the review that raised this
point) -- --check itself is still accepted and still a no-op.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_GITHOOKS_DIRNAME = ".githooks"
_REQUIRED_GITHOOKS = ("pre-commit", "commit-msg")
_SETTINGS_RELPATH = Path(".claude") / "settings.json"
_ADOPTION_LEDGER_NAME = "ADOPTION_LEDGER.md"

# The one command shape every hook line in .claude/settings.json is
# expected to use: exactly "python tools/<file>.py", no extra flags,
# forward slashes. Anything else is reported as an honest "unparsed
# hook command" issue rather than guessed at. `[^/\\]+` (not `[\w ]+`)
# deliberately allows spaces in the filename so a path-with-spaces
# command is still recognized and checked, not silently misparsed.
_HOOK_COMMAND_RE = re.compile(r"^python tools/([^/\\]+\.py)$")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _run_git(args: list, root: Path):
    """Runs a git subcommand with a short timeout, capturing output as
    text. Returns None (never raises) on ANY failure to even launch the
    process (git missing from PATH, a timeout, a permissions error) --
    callers treat None as "could not determine this fact", distinct
    from a clean non-zero exit (which git itself can produce for benign
    reasons, e.g. an unset config key)."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None


def check_git_hooks_path(root: Path) -> list:
    """(1) core.hooksPath resolves to <root>/.githooks. READ-ONLY -- no
    autofix here (see module docstring: the one write action lives in
    tools/session_context.py, run once at SessionStart, before this
    check would otherwise flag the same gap)."""
    expected = (root / _GITHOOKS_DIRNAME).resolve()
    result = _run_git(["config", "core.hooksPath"], root)
    if result is None:
        return ["git config core.hooksPath failed to run (git unavailable?)"]

    raw = (result.stdout or "").strip()
    if result.returncode != 0 or not raw:
        return ["core.hooksPath not set"]

    configured = Path(raw)
    if not configured.is_absolute():
        configured = root / configured
    try:
        configured_resolved = configured.resolve()
    except OSError:
        configured_resolved = configured
    if configured_resolved != expected:
        return [f"core.hooksPath={raw!r} does not resolve to {expected}"]
    return []


def check_required_hooks(root: Path) -> list:
    """(2) pre-commit/commit-msg present as files AND tracked in the git
    INDEX with mode 100755 -- see the module docstring for why the
    index (not the filesystem) is the source of truth here."""
    issues = []
    for name in _REQUIRED_GITHOOKS:
        if not (root / _GITHOOKS_DIRNAME / name).is_file():
            issues.append(f"hook file missing: {_GITHOOKS_DIRNAME}/{name}")

    result = _run_git(["ls-files", "-s", "--", _GITHOOKS_DIRNAME], root)
    if result is None or result.returncode != 0:
        issues.append("git ls-files -s failed -- cannot verify hook exec bits")
        return issues

    modes = {}
    for line in (result.stdout or "").splitlines():
        meta, sep, path_part = line.partition("\t")
        if not sep:
            continue
        fields = meta.split()
        if not fields:
            continue
        modes[Path(path_part).name] = fields[0]

    for name in _REQUIRED_GITHOOKS:
        if name not in modes:
            issues.append(f"hook {name} untracked in git index -- clones get no gate")
        elif modes[name] != "100755":
            issues.append(
                f"hook {name} committed non-executable ({modes[name]}) --"
                " Linux clones get a silently dead gate (D-0093)"
            )
    return issues


def _parse_hook_commands(settings) -> list:
    """Walks every hooks section of a parsed .claude/settings.json,
    collecting each hook's raw command string. Tolerant of any
    malformed shape -- a piece that isn't a dict/list where expected is
    simply skipped, never raised on."""
    commands = []
    hooks_root = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks_root, dict):
        return commands
    for matchers in hooks_root.values():
        if not isinstance(matchers, list):
            continue
        for matcher in matchers:
            if not isinstance(matcher, dict):
                continue
            entries = matcher.get("hooks")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                command = entry.get("command")
                if isinstance(command, str) and command:
                    commands.append(command)
    return commands


def check_harness_hooks(root: Path) -> list:
    """(3) every "python tools/<file>.py" hook command in
    .claude/settings.json names a file that exists. Existence-only, see
    module docstring for why no import check is done here."""
    settings_path = root / _SETTINGS_RELPATH
    try:
        text = settings_path.read_text(encoding="utf-8")
    except Exception as e:
        # Broad catch, deliberately not just OSError: a settings.json
        # saved with invalid UTF-8 bytes raises UnicodeDecodeError (a
        # ValueError subclass, not an OSError) -- must fail open the
        # same as a permissions/missing-file error, not escape uncaught.
        return [f"{settings_path} not readable ({type(e).__name__})"]

    try:
        settings = json.loads(text)
    except Exception as e:
        return [f"{settings_path} not valid JSON ({type(e).__name__})"]

    issues = []
    seen = set()
    for command in _parse_hook_commands(settings):
        m = _HOOK_COMMAND_RE.match(command.strip())
        if not m:
            issues.append(f"unparsed hook command: {command.strip()}")
            continue
        filename = m.group(1)
        if filename in seen:
            continue
        seen.add(filename)
        if not (root / "tools" / filename).is_file():
            issues.append(f"hook file not found: tools/{filename}")
    return issues


_SKILLS_DIRNAME = Path(".claude") / "skills"


def check_skills_casing(root: Path) -> list:
    """(6) every tracked file under .claude/skills/ whose basename
    equals "skill.md" case-INsensitively but is NOT spelled exactly
    "SKILL.md" is one ASCII issue string. Fact and cost this closes: on
    a case-INsensitive filesystem (the common case on Windows/macOS),
    `git add` on a path that differs from an already-tracked path only
    by case is a silent no-op -- a skill authored as "Skill.md" or
    "skill.md" can sit on disk, look correct locally, and never
    actually reach the git index under its real name, while a
    differently-cased entry already tracked (or simply the filesystem
    itself) hides the mismatch. Read via `git ls-files -- .claude/skills/`
    (the same _run_git idiom every other check here uses, timeout=5) --
    an UNTRACKED skill file is invisible to this check by construction
    (documented limit, same class as check_untracked_enforcement_files
    above being scoped to .githooks/ only). Not a git repo / git
    missing / a non-zero exit / a timeout all fail OPEN to exactly ONE
    issue string naming the wiring as unverifiable -- never raises."""
    result = _run_git(["ls-files", "--", str(_SKILLS_DIRNAME).replace("\\", "/")], root)
    if result is None or result.returncode != 0:
        return ["git ls-files failed -- cannot verify .claude/skills/ SKILL.md casing"]

    issues = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        basename = Path(line).name
        if basename.lower() == "skill.md" and basename != "SKILL.md":
            issues.append(
                f"skill file '{line}' has non-canonical casing (expected exactly "
                "'SKILL.md' -- a case-insensitive filesystem silently no-ops "
                "'git add' on a case-only rename)"
            )
    return issues


def check_untracked_enforcement_files(root: Path) -> list:
    """(5) a file present on disk under .githooks/ that git does not
    track at all -- never `git add`-ed, invisible to a fresh clone. No
    .githooks/ directory at all is not itself an issue here (that gap
    is check_required_hooks's/check_git_hooks_path's job to report)."""
    githooks_dir = root / _GITHOOKS_DIRNAME
    if not githooks_dir.is_dir():
        return []

    try:
        on_disk = {p.name for p in githooks_dir.iterdir() if p.is_file()}
    except OSError:
        return []

    result = _run_git(["ls-files", "--", _GITHOOKS_DIRNAME], root)
    if result is None or result.returncode != 0:
        return ["git ls-files failed -- cannot verify untracked files under .githooks"]

    tracked = {Path(line).name for line in (result.stdout or "").splitlines() if line.strip()}
    untracked = sorted(on_disk - tracked)
    return [f"untracked enforcement file: {_GITHOOKS_DIRNAME}/{name}" for name in untracked]


# See module docstring, check (4): deliberately narrow keyword sets --
# only ledger rows naming machinery checks (1)-(3) above already cover
# are reconciled; every other row is left alone.
_GIT_HOOKS_ROW_KEYWORDS = (".githooks", "hookspath", "commit-msg", "pre-commit", "mechanism gate")
_HARNESS_HOOKS_ROW_KEYWORDS = ("settings.json", "sessionstart", "session_context.py")

_LEDGER_ROW_RE = re.compile(r"^\|(.+?)\|(.+?)\|(.+?)\|\s*$")


def _parse_ledger_adopt_rows(text: str) -> list:
    """Parses ADOPTION_LEDGER.md's pipe-table rows, returns the "Kit
    mechanism" cell text of every row whose Status cell is exactly
    "adopt" (case-insensitive, trimmed). The header row's Status cell
    literally reads "Status" and the separator row is all dashes --
    neither matches "adopt", so both are skipped without special-casing
    them. A line that doesn't match the 3-cell pipe pattern (prose,
    section headers, a malformed row) is silently skipped, not raised
    on -- this function has no try/except of its own, callers decide
    whether a total parse failure (e.g. the read itself failing) is
    fail-open."""
    rows = []
    for line in text.splitlines():
        m = _LEDGER_ROW_RE.match(line.strip())
        if not m:
            continue
        mechanism, status, _basis = m.groups()
        if status.strip().lower() == "adopt":
            rows.append(mechanism.strip())
    return rows


def check_adoption_ledger(root: Path, git_issues: list, harness_issues: list) -> list:
    """(4, D-0092): see module docstring for the full rationale and its
    deliberately narrow scope. git_issues/harness_issues are the
    already-computed outputs of the checks above (passed in rather than
    recomputed) so the whole reconciliation reads one consistent
    snapshot of the wiring state, not a second, possibly-different git
    invocation."""
    ledger_path = root / _ADOPTION_LEDGER_NAME
    if not ledger_path.is_file():
        return []

    try:
        text = ledger_path.read_text(encoding="utf-8")
    except Exception as e:
        # Broad catch, deliberately not just OSError -- same rationale
        # as check_harness_hooks: an invalid-UTF-8 ledger file must
        # still fail open to a WARN, not raise UnicodeDecodeError past
        # check_wiring()'s "never raises" contract.
        return [f"{_ADOPTION_LEDGER_NAME} not readable ({type(e).__name__})"]

    try:
        adopt_rows = _parse_ledger_adopt_rows(text)
    except Exception as e:
        return [f"{_ADOPTION_LEDGER_NAME} not parseable ({type(e).__name__})"]

    issues = []
    has_git_issue = bool(git_issues)
    has_harness_issue = bool(harness_issues)
    for mechanism in adopt_rows:
        low = mechanism.lower()
        if has_git_issue and any(k in low for k in _GIT_HOOKS_ROW_KEYWORDS):
            issues.append(
                f"adoption ledger row '{mechanism}' is 'adopt' but git-hooks wiring has an open issue"
            )
        if has_harness_issue and any(k in low for k in _HARNESS_HOOKS_ROW_KEYWORDS):
            issues.append(
                f"adoption ledger row '{mechanism}' is 'adopt' but harness-hooks wiring has an open issue"
            )
    return issues


# Names check_wiring's `skip` parameter recognizes -- exactly the
# check functions defined above, by their own function names, so a
# caller of check_wiring() and someone reading this module's source
# are looking at the same vocabulary. An unrecognized name in `skip`
# is silently a no-op (nothing in check_wiring's own five-line body
# matches an unknown key) by the same "fail open, no raise" contract
# the individual check_* functions already follow -- see D-0058
# discussion in the module docstring's CLI section for why an unknown
# --mode-adjacent name here is a warning at the CLI layer instead of
# an exception at this layer, which is the importable function other
# callers besides the CLI also use directly.
_KNOWN_CHECK_NAMES = frozenset(
    {
        "check_git_hooks_path",
        "check_required_hooks",
        "check_harness_hooks",
        "check_untracked_enforcement_files",
        "check_adoption_ledger",
        "check_skills_casing",
    }
)


def check_wiring(root: Path = None, skip: frozenset = frozenset()) -> dict:
    """Runs every check above and aggregates them into
    {"ok": bool, "issues": [str, ...]}. Never raises: each check
    function already fails open (a subprocess/file/parse error becomes
    an issue string, not an exception) -- this is a thin aggregator
    with no I/O of its own beyond what the checks already perform.

    `skip` -- a set of check function names (see _KNOWN_CHECK_NAMES)
    to OMIT from this run, each contributing an empty issue list in
    place of actually running. Default empty set: the aggregation is
    byte-identical to before this parameter existed (regression pin:
    test_check_wiring_default_skip_is_byte_identical_to_before).
    Exists so a caller auditing a kit's own SOURCE tree (--mode source,
    per the installed-vs-source root-confusion finding) can skip
    exactly the checks that are genuinely about live installation
    state, through the SAME aggregation this
    function already performs -- rather than a second, hand-inlined
    copy of this function's five lines that a future new check would
    silently not know to skip."""
    root = Path(root) if root else repo_root()
    git_hooks_issues = [] if "check_git_hooks_path" in skip else check_git_hooks_path(root)
    required_hooks_issues = [] if "check_required_hooks" in skip else check_required_hooks(root)
    git_issues = git_hooks_issues + required_hooks_issues
    harness_issues = [] if "check_harness_hooks" in skip else check_harness_hooks(root)
    untracked_issues = (
        [] if "check_untracked_enforcement_files" in skip else check_untracked_enforcement_files(root)
    )
    skills_casing_issues = [] if "check_skills_casing" in skip else check_skills_casing(root)
    ledger_issues = (
        []
        if "check_adoption_ledger" in skip
        else check_adoption_ledger(root, git_issues, harness_issues)
    )
    issues = git_issues + harness_issues + untracked_issues + skills_casing_issues + ledger_issues
    return {"ok": not issues, "issues": issues}


# Checks that are genuinely about a LIVE installation's state (a real
# git config value someone had to run `git config core.hooksPath ...`
# to set) rather than about the kit's own committed tree content --
# see the module docstring's "CLI ROOT/MODE FLAGS" section for why
# only this one check qualifies. This is BOTH the skip set passed to
# check_wiring() and (via its keys) the printed skip message -- one
# dict, so the message and the actual skip decision cannot drift
# apart, and so a future check added to check_wiring() that ALSO
# turns out to be host-install-only is skipped here by adding its
# name to this one dict, not by re-deriving a second, separate list.
_SOURCE_MODE_SKIP = {"check_git_hooks_path": "core.hooksPath wiring"}


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wiring_check.py",
        description="Read-only auditor of the kit's enforcement-chain wiring (D-0092/D-0093).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="documented invocation flag; has no effect on behavior (there is only one report form).",
    )
    parser.add_argument(
        "--host-root",
        default=None,
        metavar="PATH",
        help="root of the INSTALLED host repo to audit in --mode installed "
        "(default: this script's own repo_root() guess -- unchanged from "
        "the pre-existing single-root behavior).",
    )
    parser.add_argument(
        "--kit-root",
        default=None,
        metavar="PATH",
        help="root of the kit's SOURCE tree to audit in --mode source "
        "(default: the same repo_root() guess as --host-root's default).",
    )
    parser.add_argument(
        "--mode",
        choices=("installed", "source"),
        default="installed",
        help="'installed' (default): audit an installed host repo -- today's "
        "behavior, unchanged. 'source': audit the kit's own source tree, "
        "skipping the checks that only make sense against a live install.",
    )
    return parser


def main(argv=None) -> int:
    """CLI form: `python tools/wiring_check.py --check`, now with
    --host-root/--kit-root/--mode (see module docstring). Both modes
    still route through check_wiring()/the individual check_* functions
    directly -- no separate reporting logic -- so the CLI and the
    importable functions can never disagree on what counts as an
    issue, only on which root and which subset of checks apply."""
    args = _build_arg_parser().parse_args(sys.argv[1:] if argv is None else argv)
    default_root = repo_root()
    host_root = Path(args.host_root).resolve() if args.host_root else default_root
    kit_root = Path(args.kit_root).resolve() if args.kit_root else default_root

    bad_root = False
    if args.mode == "source":
        if args.host_root is not None:
            print("warning: --host-root is ignored in --mode source (only --kit-root is used)")
        root = kit_root
        skip_names = sorted(_SOURCE_MODE_SKIP)
        skip_label = ", ".join(f"{name} ({_SOURCE_MODE_SKIP[name]})" for name in skip_names)
        print(f"source mode: skipping host-only checks: {skip_label}")
        result = check_wiring(root, skip=frozenset(_SOURCE_MODE_SKIP))
    else:
        if args.kit_root is not None:
            print("warning: --kit-root is ignored in --mode installed (only --host-root is used)")
        root = host_root
        if not (root / _GITHOOKS_DIRNAME).is_dir() and not (root / ".claude").is_dir():
            bad_root = True
            print(
                f"host-root {root} does not look like an installed host "
                "(no .githooks and no .claude) -- likely run from the wrong "
                "root; use --mode source to check kit source instead"
            )
        result = check_wiring(root)

    if result["ok"]:
        print("WIRING: OK")
        exit_code = 0
    else:
        print(f"WIRING: {len(result['issues'])} issue(s)")
        for issue in result["issues"]:
            print(f"  - {issue}")
        exit_code = 1

    if bad_root:
        # Caller-diagnosis error, not a clean/dirty wiring verdict: never
        # let a coincidentally-clean check_wiring() result mask it.
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
