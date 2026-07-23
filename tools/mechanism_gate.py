"""Gate for rule 10(b): a commit touching mechanism files must carry an
axis block.

Called by the commit-msg hook (.githooks/commit-msg) with the path to
the commit message file. Logic (entirely in a pure decide(), testable
without git):

1. Staged paths don't touch any mechanism prefix -> the gate is silent.
2. A merge commit (MERGE_HEAD exists) -> the gate is silent: merged
   commits already passed this gate individually; blocking the merge's
   auto-message would be a false positive that trains toward
   --no-verify.
3. Skip line "axes: not a mechanism (<reason>)" (this template's
   English phrasing, CLAUDE.md rule 10) works ONLY from the commit
   message -- a written statement by the committer, the same pattern
   as `dispatch_skipped`. Not looked up in the diff: decision text that
   quotes the skip syntax would otherwise bypass the gate. Inside the
   message itself the line counts ONLY as its OWN separate line
   (^...$ anchor with MULTILINE, indentation by spaces allowed) --
   otherwise an inline quote of the skip syntax in the middle of the
   message's prose would silence the whole gate (source deployment's
   Dog range finding, D-0093); the same anchor already used by
   TIER_LINE_RE below.
4. Axis block -- lines "axis N: <verdict>" for EVERY axis of the
   current docs/SIBLING_MAP.md -- looked up in the commit message PLUS
   in the staged diff of ONE file, DECISIONS.md (this template ships a
   single decisions file; the source deployment's two-file split
   (DECISIONS.md summary + docs/DECISIONS_FULL.md detail) collapses to
   just DECISIONS.md here -- template dependency, toolkit transfer).
   The whole diff is not scanned: unrelated staged content with a
   literal "axis N:" would close axes fictitiously. Axis count and
   numbers are read from the map on every run -- the map grows and
   changes, the gate follows it.
5. The map can't be read / has zero axes -> fail-closed (a silent skip
   of the check is indistinguishable from passing it).
6. Net: known homes of mechanisms in this template (CLAUDE.md,
   DECISIONS.md, docs/SIBLING_MAP.md, PROCESS/, .claude/agents/,
   .claude/skills/, BOOT.md) plus self-protection of the enforcement
   chain itself (this file, tools/session_context.py -- the
   SessionStart hook, .githooks/, .claude/settings.json -- editing the
   gate or the hook registration must not bypass the gate). Wide
   directories (tools/, gateway/) are deliberately outside the net --
   false positives there train toward --no-verify (same tradeoff as
   the source deployment's D-0055).
7. Tier declaration (ported from the source deployment's D-0072,
   mechanism 5): on the "mechanism" branch (axis block already
   satisfied), the commit message must carry a SEPARATE line
   "tier: <value>" -- a self-declaration of the committer's actual
   tier, the same pattern as `dispatch_skipped`. Expected value: the
   model bound to roles.lead in this template's own
   delegation.config.yaml, resolved relative to REPO (this template's
   install root, next to this gate) -- not the caller's cwd. No file,
   or no roles.lead key -> default to the "fable" family (the
   subscription-contour default for Lead). A declaration is accepted
   on an exact match with the bound model, OR by containing its tier
   family (fable/opus/sonnet/haiku, matched as a substring) -- a
   non-Claude binding has no family, so only an exact model-id match
   qualifies for it. The skip branch ("not a mechanism") and merge
   commits do not require a tier line (same exemption net as the axis
   block). The gate does NOT verify the declaration is true --
   two-layer enforcement: code guarantees the line's presence and
   shape, truth is judged by calibration against transcripts, a tier
   above.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
MAP_PATH = REPO / "docs" / "SIBLING_MAP.md"
# Template dependency (toolkit transfer): this template has ONE decisions
# file, DECISIONS.md -- not the source deployment's docs/DECISIONS_FULL.md.
DECISIONS_FULL = "DECISIONS.md"
# This template's install root, next to this gate -- not the caller's cwd.
CONFIG_PATH = REPO / "delegation.config.yaml"

LEAD_FAMILIES = ("fable", "opus", "sonnet", "haiku")
TIER_LINE_RE = re.compile(r"^\s*tier\s*:\s*(\S.*?)\s*$", re.IGNORECASE | re.MULTILINE)

MECHANISM_PREFIXES = (
    "CLAUDE.md",
    "DECISIONS.md",
    "docs/SIBLING_MAP.md",
    "PROCESS/",
    ".claude/agents/",
    ".claude/skills/",
    "BOOT.md",
    "tools/mechanism_gate.py",
    ".githooks/",
    # SessionStart hook duties are future-session obligations too.
    "tools/session_context.py",
    ".claude/settings.json",
)

# Template dependency (toolkit transfer, empirically verified against
# toolkit/docs/SIBLING_MAP.md and toolkit/CLAUDE.md rule 10): this
# template's map headings and its axis-answer/skip-line vocabulary are
# English ("## Axis N", "axis N: covered", "axes: not a mechanism
# (<reason>)") -- the source deployment's original regexes matched on
# its own non-English heading text and silently matched zero axes
# against this template's own map (fail-closed on every mechanism
# commit); ported as English to match the artifact these regexes
# actually run against.
AXIS_HEADING_RE = re.compile(r"^##\s+Axis\s+(\d+)", re.MULTILINE)
# Line anchor (D-0093, source deployment's Dog range): without ^...$/
# MULTILINE the phrase matched via .search() ANYWHERE in the message --
# an inline quote of the skip syntax in the middle of prose ("...the
# line \"axes: not a mechanism (example)\" would bypass...") silenced
# the whole gate. Symmetric with the already-anchored TIER_LINE_RE.
SKIP_RE = re.compile(r"^\s*axes\s*:\s*not\s+a\s+mechanism\s*\(", re.IGNORECASE | re.MULTILINE)


def parse_axes(map_text: str) -> list[int]:
    """Axis numbers from the map's headings; order and gaps in the
    numbering don't matter."""
    return [int(n) for n in AXIS_HEADING_RE.findall(map_text)]


def _matches(path: str, pref: str) -> bool:
    # Prefix boundary: directories match by startswith, files match
    # exactly (CLAUDE.md.bak is not a mechanism path).
    if pref.endswith("/"):
        return path.startswith(pref)
    return path == pref


def mechanism_paths(staged: list[str]) -> list[str]:
    return [p for p in staged
            if any(_matches(p, pref) for pref in MECHANISM_PREFIXES)]


def find_missing(text: str, axes: list[int]) -> list[int]:
    return [n for n in axes
            if not re.search(rf"axis\s+{n}\s*:", text, re.IGNORECASE)]


def resolve_lead_binding(config_text: str | None) -> str:
    """The model bound to roles.lead in delegation.config.yaml. No file,
    no roles.lead key, or unparsable YAML -> default to the "fable"
    family (the subscription-contour default for Lead) -- a
    conservative (fail-closed) choice: it requires an explicit
    declaration from anyone below the top tier. The declaration itself
    is NOT checked for truth here (see tier_declared_ok) -- two-layer
    enforcement: code guarantees the shape, calibration against
    transcripts judges the truth, a tier above."""
    if not config_text:
        return "fable"
    try:
        data = yaml.safe_load(config_text) or {}
    except yaml.YAMLError:
        return "fable"
    lead = (data.get("roles") or {}).get("lead") or {}
    model = ((lead.get("subscription") or {}).get("model")
             or (lead.get("api") or {}).get("model"))
    return model or "fable"


def lead_family(binding: str) -> str | None:
    """The bound model's tier family by substring (fable/opus/sonnet/
    haiku); None -- no family recognized (a non-Claude binding), in
    which case only an exact model-id match qualifies."""
    low = binding.lower()
    for fam in LEAD_FAMILIES:
        if fam in low:
            return fam
    return None


def find_tier_declarations(msg: str) -> list[str]:
    """ALL "tier: <value>" line values in the commit message (not the
    diff) -- not just the first. Several mechanisms in one commit may
    each carry their own tier line. SAFE (fail-closed) semantics,
    chosen deliberately: every found line MUST pass tier_declared_ok
    (see decide_full below) -- reject if even ONE found line fails to
    match the binding, even when another (e.g. a real, later) line
    does match. The alternative ("passes if ANY line matches") was
    rejected: with MULTIPLE tier lines in one message, it would let one
    spoofed/quoted matching line mask a REAL mismatched value elsewhere
    in the same message.

    GUARANTEE-SCOPE CLARIFICATION (source deployment critic t-278(a) --
    docstring corrected, no code change): "ALL must pass" defends
    exactly that MULTI-LINE case (a real mismatched line plus a
    spoofing matching line next to it) -- a SINGLE-LINE spoofer (one
    fake tier line, no real one present) passes BOTH semantics THE
    SAME: this function never checks that the declared tier is TRUE,
    only that its declared FORM matches the binding (truthfulness of
    the declaration is calibration's job, reconciled against
    transcripts -- code guarantees the form, a tier above judges the
    meaning). The actual effect of the chosen semantics is fail-closed
    on quotes/multiple lines (a false reject is safer than a false
    accept), not a general anti-spoofing guarantee."""
    return [m.strip() for m in TIER_LINE_RE.findall(msg)]


def find_tier_declaration(msg: str) -> str | None:
    """Backward-compat convenience: the value of the FIRST "tier:
    <value>" line (see find_tier_declarations() for the full
    all-lines semantics used by decide_full())."""
    declarations = find_tier_declarations(msg)
    return declarations[0] if declarations else None


def tier_declared_ok(declared: str, binding: str) -> bool:
    if declared == binding:
        return True
    fam = lead_family(binding)
    if fam is None:
        return False
    return fam in declared.lower()


def _tier_queue_note() -> str:
    return ("a mechanism commit is Lead-tier work: a session below the "
            "lead binding's tier does NOT commit the mechanism itself -- "
            "it goes into the Lead queue in CURRENT_CONTEXT.md; a "
            "lead-tier session adds the line \"tier: <its own model>\".")


def decide(msg: str, block_extra: str, staged: list[str],
           map_text: str | None, merging: bool = False) -> tuple[int, str]:
    """Pure gate decision. block_extra -- the diff of DECISIONS.md."""
    hits = mechanism_paths(staged)
    if not hits:
        return 0, ""
    if merging:
        return 0, ""
    if SKIP_RE.search(msg):  # message only -- not looked up in the diff + own separate line only (anchor, D-0093)
        return 0, ""
    if map_text is None:
        return 1, (f"axis map not found ({MAP_PATH}) -- fail-closed, "
                   "commit rejected (rule 10(b))")
    axes = parse_axes(map_text)
    if not axes:
        return 1, ("no axis found in the map (## Axis N) -- "
                   "fail-closed (rule 10(b))")
    missing = find_missing(msg + "\n" + block_extra, axes)
    if missing:
        return 1, ("commit touches mechanism files:\n  " + "\n  ".join(hits)
                   + "\nRule 10(b)'s axis block is incomplete -- no verdict for axes: "
                   + ", ".join(str(n) for n in missing)
                   + "\nAdd \"axis N: covered / queued / n/a <why>\" for "
                   "every axis of the map (in the commit message or in the "
                   "decision text, DECISIONS.md), or an explicit skip in the "
                   "COMMIT MESSAGE: \"axes: not a mechanism (<reason>)\" "
                   "(rule 10(b)).")
    return 0, ""


def decide_full(msg: str, block_extra: str, staged: list[str],
                 map_text: str | None, config_text: str | None,
                 merging: bool = False) -> tuple[int, str]:
    """decide() plus the tier-declaration requirement (rule 7): a
    "tier: <value>" line on the "mechanism" branch (axis block already
    satisfied, not skip, not merge). config_text -- the text of
    delegation.config.yaml (or None if the file is absent), the same
    pattern as map_text."""
    code, reason = decide(msg, block_extra, staged, map_text, merging)
    if code:
        return code, reason
    hits = mechanism_paths(staged)
    if not hits or merging or SKIP_RE.search(msg):
        return 0, ""
    binding = resolve_lead_binding(config_text)
    # ALL found tier lines must pass -- reject if even ONE does not
    # match the binding (see find_tier_declarations()'s docstring for
    # the chosen semantics).
    declared_list = find_tier_declarations(msg)
    if not declared_list:
        return 1, ("commit touches mechanism files:\n  " + "\n  ".join(hits)
                    + "\nNo \"tier: <value>\" line (lead binding: "
                    + binding + ") -- " + _tier_queue_note())
    bad = [d for d in declared_list if not tier_declared_ok(d, binding)]
    if bad:
        return 1, ("commit touches mechanism files:\n  " + "\n  ".join(hits)
                    + "\nNot lead tier: \"tier: " + bad[0]
                    + "\" does not match the binding (" + binding
                    + ") -- " + _tier_queue_note())
    return 0, ""


def _git(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.stdout or ""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("mechanism_gate: need a path to the commit message file", file=sys.stderr)
        return 1
    staged = _git("diff", "--cached", "--name-only").splitlines()
    merge_head = _git("rev-parse", "--git-path", "MERGE_HEAD").strip()
    merging = bool(merge_head) and Path(merge_head).exists()
    msg = Path(argv[0]).read_text(encoding="utf-8", errors="replace")
    block_extra = _git("diff", "--cached", "--", DECISIONS_FULL)
    map_text = (MAP_PATH.read_text(encoding="utf-8", errors="replace")
                if MAP_PATH.exists() else None)
    config_text = (CONFIG_PATH.read_text(encoding="utf-8", errors="replace")
                   if CONFIG_PATH.exists() else None)
    code, reason = decide_full(msg, block_extra, staged, map_text,
                               config_text, merging)
    if code:
        print("mechanism_gate: " + reason, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
