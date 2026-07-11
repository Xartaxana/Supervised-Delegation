"""Tests for tools/mechanism_gate.py -- the axis-block gate of CLAUDE.md
rule 10(b).

Axis heading/answer/skip vocabulary is English here ("## Axis N",
"axis N: ...", "axes: not a mechanism (...)") to match this template's
own docs/SIBLING_MAP.md and CLAUDE.md rule 10 -- verified empirically
against toolkit/docs/SIBLING_MAP.md's real headings before choosing
this vocabulary (the source deployment's Russian regexes matched zero
axes against this template's own map).
"""
from __future__ import annotations

import mechanism_gate as mg

MAP_SAMPLE = """# Sibling Map
## Axis 1 -- Deployments
...
## Axis 2 -- Contours
...
## Axis 6 -- Internal axes
...
## Checking the map itself
"""


def test_parse_axes_follows_the_map_not_a_constant():
    # Axis count and numbers come from the map on every run; a gap in
    # numbering (2 -> 6) doesn't break the parser.
    assert mg.parse_axes(MAP_SAMPLE) == [1, 2, 6]
    assert mg.parse_axes("# empty\n") == []


def test_mechanism_paths_filters_prefixes_with_boundary():
    staged = ["CLAUDE.md", "PROCESS/WEEKLY_CALIBRATION_PROTOCOL.md",
              ".claude/agents/scout.md", "gateway/metrics.py",
              "docs/RELATED_WORK.md", "logs/routing-log.jsonl"]
    assert mg.mechanism_paths(staged) == [
        "CLAUDE.md", "PROCESS/WEEKLY_CALIBRATION_PROTOCOL.md",
        ".claude/agents/scout.md"]
    # Prefix boundary: file prefixes match exactly, not as a substring.
    assert mg.mechanism_paths(["CLAUDE.md.bak", "DECISIONS.md.orig",
                               "gateway/metrics.py"]) == []


def test_mechanism_paths_template_homes_and_self_protection():
    # Template MECHANISM_PREFIXES: known mechanism homes + self-protection
    # of the enforcement chain (this gate, the SessionStart hook it shares
    # a home with, the hooks dir, the hook registration file).
    extra = ["BOOT.md", "tools/mechanism_gate.py", "tools/session_context.py",
             ".githooks/commit-msg", ".claude/settings.json"]
    assert mg.mechanism_paths(extra) == extra
    # Narrowness is deliberate (ported from the source deployment):
    # other tools/ and gateway/ files stay outside the net.
    assert mg.mechanism_paths(["tools/usage_report.py",
                               "tools/test_mechanism_gate.py",
                               "gateway/config.yaml",
                               ".claude/settings.local.json",
                               "BOOT.md.bak"]) == []


def test_find_missing_reports_absent_axes_case_insensitive():
    text = "axis 1: covered -- CLAUDE.md both deployments\nAxis 2: n/a (no money involved)\n"
    assert mg.find_missing(text, [1, 2, 6]) == [6]
    assert mg.find_missing(text + "axis 6: queued (next touch)\n", [1, 2, 6]) == []
    # Digit boundary: "axis 15:" does not close axis 1.
    assert mg.find_missing("axis 15: covered\n", [1]) == [1]


def test_prose_answer_is_not_an_answer():
    # Recall-prose "axes are covered" does not satisfy the enumeration format.
    assert mg.find_missing("all axes are covered, checked", [1, 2]) == [1, 2]


def test_decide_skip_only_from_commit_message():
    # A skip line quoted in the DIFF (decision text) does NOT bypass the
    # gate; only the commit message counts.
    code, reason = mg.decide(
        msg="feat: mechanism X",
        block_extra="+ ... legal via the line \"axes: not a mechanism (<reason>)\" ...",
        staged=["CLAUDE.md"], map_text="## Axis 1 -- Deployments\n")
    assert code == 1 and "1" in reason
    code, _ = mg.decide(
        msg="docs: typo fix\n\naxes: not a mechanism (typo in rule 3)",
        block_extra="", staged=["CLAUDE.md"], map_text="## Axis 1 -- Deployments\n")
    assert code == 0


def test_decide_block_counted_from_message_and_decisions_diff_only():
    # Unrelated staged content does not close axes -- decide() receives
    # the diff of ONLY DECISIONS_FULL (here: DECISIONS.md), main() calls
    # it that way.
    code, _ = mg.decide(
        msg="feat: mechanism X\n\naxis 1: covered -- both deployments",
        block_extra="", staged=["CLAUDE.md"], map_text="## Axis 1 --\n")
    assert code == 0
    code, _ = mg.decide(
        msg="feat: mechanism X",
        block_extra="+axis 1: covered -- both deployments (decision text)",
        staged=["CLAUDE.md"], map_text="## Axis 1 --\n")
    assert code == 0


def test_decide_merge_and_non_mechanism_commits_pass():
    # Merge commits are not blocked -- merged commits already passed the
    # gate individually.
    code, _ = mg.decide(msg="Merge branch 'x'", block_extra="",
                        staged=["CLAUDE.md"], map_text="## Axis 1 --\n",
                        merging=True)
    assert code == 0
    code, _ = mg.decide(msg="chore: telemetry", block_extra="",
                        staged=["gateway/metrics.py", "logs/routing-log.jsonl"],
                        map_text="## Axis 1 --\n")
    assert code == 0


def test_decide_fails_closed_without_map_or_axes():
    code, reason = mg.decide(msg="feat: X", block_extra="",
                             staged=["CLAUDE.md"], map_text=None)
    assert code == 1 and "fail-closed" in reason
    code, reason = mg.decide(msg="feat: X", block_extra="",
                             staged=["CLAUDE.md"], map_text="# map without axes\n")
    assert code == 1 and "fail-closed" in reason


def test_explicit_skip_line_matches():
    assert mg.SKIP_RE.search("axes: not a mechanism (typo fix in CLAUDE.md)")
    assert mg.SKIP_RE.search("Axes: not a mechanism (archival reshuffle)")
    assert not mg.SKIP_RE.search("axes are covered by not a mechanism")
