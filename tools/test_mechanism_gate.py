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

# Ported from the source deployment (D-0072): a config with a Claude
# binding (matching this template's own delegation.config.yaml) and one
# with a non-Claude binding.
CONFIG_SAMPLE = """
roles:
  lead:
    subscription:
      model: claude-fable-5
    api:
      provider:
      model:
      api_key_env:
"""

CONFIG_SAMPLE_NON_CLAUDE = """
roles:
  lead:
    subscription:
      model:
    api:
      provider: groq
      model: llama-3.3-70b-versatile
      api_key_env: GROQ_API_KEY
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


# --- Tier declaration on the "mechanism" branch (ported from D-0072) ----

def test_resolve_lead_binding_defaults_to_fable_without_config():
    assert mg.resolve_lead_binding(None) == "fable"
    assert mg.resolve_lead_binding("roles: {}\n") == "fable"
    assert mg.resolve_lead_binding("not: yaml: [broken\n") == "fable"


def test_resolve_lead_binding_reads_subscription_model():
    assert mg.resolve_lead_binding(CONFIG_SAMPLE) == "claude-fable-5"


def test_resolve_lead_binding_falls_back_to_api_for_non_claude():
    assert (mg.resolve_lead_binding(CONFIG_SAMPLE_NON_CLAUDE)
            == "llama-3.3-70b-versatile")


def test_tier_declared_ok_exact_and_family_vs_non_claude():
    assert mg.tier_declared_ok("claude-fable-5", "claude-fable-5")
    assert mg.tier_declared_ok("fable", "claude-fable-5")
    assert not mg.tier_declared_ok("sonnet", "claude-fable-5")
    # Non-Claude binding: no family, only an exact match qualifies.
    assert mg.tier_declared_ok("llama-3.3-70b-versatile",
                               "llama-3.3-70b-versatile")
    assert not mg.tier_declared_ok("fable", "llama-3.3-70b-versatile")


def test_decide_full_missing_tier_line_fails():
    code, reason = mg.decide_full(
        msg="feat: mechanism X\n\naxis 1: covered -- both deployments",
        block_extra="", staged=["CLAUDE.md"], map_text="## Axis 1 --\n",
        config_text=None)
    assert code == 1
    assert "tier" in reason.lower()
    assert "Lead queue" in reason


def test_decide_full_tier_mismatch_fails_with_distinct_text():
    # Default lead binding (no config file) is "fable"; sonnet doesn't fit.
    code, reason = mg.decide_full(
        msg="feat: mechanism X\n\naxis 1: covered\ntier: sonnet",
        block_extra="", staged=["CLAUDE.md"], map_text="## Axis 1 --\n",
        config_text=None)
    assert code == 1
    assert "Not lead tier" in reason
    # Distinct from the "missing line" text.
    assert "No \"tier:" not in reason


def test_decide_full_tier_fable_default_passes():
    code, _ = mg.decide_full(
        msg="feat: mechanism X\n\naxis 1: covered\ntier: fable",
        block_extra="", staged=["CLAUDE.md"], map_text="## Axis 1 --\n",
        config_text=None)
    assert code == 0


def test_decide_full_tier_exact_model_id_passes():
    code, _ = mg.decide_full(
        msg="feat: mechanism X\n\naxis 1: covered\ntier: claude-fable-5",
        block_extra="", staged=["CLAUDE.md"], map_text="## Axis 1 --\n",
        config_text=CONFIG_SAMPLE)
    assert code == 0


def test_decide_full_skip_line_without_tier_passes():
    code, _ = mg.decide_full(
        msg="docs: typo fix\n\naxes: not a mechanism (typo in rule 3)",
        block_extra="", staged=["CLAUDE.md"], map_text="## Axis 1 --\n",
        config_text=None)
    assert code == 0


def test_decide_full_merge_commit_without_tier_passes():
    code, _ = mg.decide_full(
        msg="Merge branch 'x'", block_extra="", staged=["CLAUDE.md"],
        map_text="## Axis 1 --\n", config_text=None, merging=True)
    assert code == 0


def test_decide_full_non_claude_lead_requires_exact_match():
    code, _ = mg.decide_full(
        msg="feat: mechanism X\n\naxis 1: covered\ntier: llama-3.3-70b-versatile",
        block_extra="", staged=["CLAUDE.md"], map_text="## Axis 1 --\n",
        config_text=CONFIG_SAMPLE_NON_CLAUDE)
    assert code == 0
    code, reason = mg.decide_full(
        msg="feat: mechanism X\n\naxis 1: covered\ntier: fable",
        block_extra="", staged=["CLAUDE.md"], map_text="## Axis 1 --\n",
        config_text=CONFIG_SAMPLE_NON_CLAUDE)
    assert code == 1
    assert "Not lead tier" in reason
