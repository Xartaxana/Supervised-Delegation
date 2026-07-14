"""Tests for tools/calibration_counts.py. Synthetic journal fixtures on
tmp_path, one case per class from the spec, plus a CLI smoke test."""
import json

from calibration_counts import analyze_journal, main, parse_ts


def write_journal(path, lines):
    """lines: a list of dicts OR raw strings (for unparsable lines /
    the spaced-JSON format)."""
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            if isinstance(line, str):
                fh.write(line + "\n")
            else:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def ev(ts, event, **kw):
    d = {"ts": ts, "event": event}
    d.update(kw)
    return d


# ---------------------------------------------------------------------
# 1. rule-6 pair without escalated -> candidate
# ---------------------------------------------------------------------
def test_rule6_pair_without_escalated_is_candidate(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-08T00:00:00", "journal_created", notes="init"),
        ev("2026-07-08T01:00:00", "delegated", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),
        ev("2026-07-08T01:10:00", "rejected", agent="scout", model="haiku",
           task_id="t-001", attempt=1, failure_class="tooling", category="recon", notes="n"),
        ev("2026-07-08T01:20:00", "delegated", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),
        ev("2026-07-08T01:30:00", "rejected", agent="scout", model="haiku",
           task_id="t-001", attempt=2, failure_class="tooling", category="recon", notes="n"),
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    assert len(report["rule6_candidates"]) == 1
    assert report["rule6_candidates"][0]["task_id"] == "t-001"


# ---------------------------------------------------------------------
# 2. rule-6 pair WITH escalated -> NOT a candidate
# ---------------------------------------------------------------------
def test_rule6_pair_with_escalated_not_candidate(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-08T00:00:00", "journal_created", notes="init"),
        ev("2026-07-08T01:00:00", "delegated", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),
        ev("2026-07-08T01:10:00", "rejected", agent="scout", model="haiku",
           task_id="t-001", attempt=1, failure_class="tooling", category="recon", notes="n"),
        ev("2026-07-08T01:20:00", "delegated", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),
        ev("2026-07-08T01:30:00", "rejected", agent="scout", model="haiku",
           task_id="t-001", attempt=2, failure_class="tooling", category="recon", notes="n"),
        ev("2026-07-08T01:40:00", "escalated", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    assert report["rule6_candidates"] == []


# ---------------------------------------------------------------------
# 3. rejected without failure_class -> violation
# ---------------------------------------------------------------------
def test_rejected_missing_failure_class_is_violation(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-09T00:00:00", "delegated", agent="builder", model="sonnet",
           task_id="t-001", category="implementation", notes="n"),
        ev("2026-07-09T00:10:00", "rejected", agent="builder", model="sonnet",
           task_id="t-001", attempt=1, category="implementation", notes="n"),
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    viol = [v for v in report["field_violations"] if v["event"] == "rejected"]
    assert len(viol) == 1
    assert "failure_class" in viol[0]["missing_fields"]


# ---------------------------------------------------------------------
# 4. accepted(builder) without witness -> violation
# ---------------------------------------------------------------------
def test_accepted_builder_missing_witness_is_violation(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-09T00:00:00", "delegated", agent="builder", model="sonnet",
           task_id="t-001", category="implementation", notes="n"),
        ev("2026-07-09T00:10:00", "accepted", agent="builder", model="sonnet",
           task_id="t-001", category="implementation", notes="n"),
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    viol = [v for v in report["field_violations"] if v["event"] == "accepted"]
    assert len(viol) == 1
    assert "witness" in viol[0]["missing_fields"]


# ---------------------------------------------------------------------
# 5. missing 'by' after the cutoff vs. legal before it
# ---------------------------------------------------------------------
def test_by_missing_after_cutoff_legal_before(tmp_path):
    p = tmp_path / "j.jsonl"
    by_since = "2026-07-10T13:14:00"
    write_journal(p, [
        ev("2026-07-09T00:00:00", "accepted", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),  # before cutoff, no by -> legal
        ev("2026-07-10T14:00:00", "accepted", agent="scout", model="haiku",
           task_id="t-002", category="recon", notes="n"),  # after cutoff, no by -> candidate
        ev("2026-07-10T15:00:00", "accepted", agent="scout", model="haiku",
           task_id="t-003", category="recon", notes="n", by="fable"),  # after, with by -> ok
    ])
    report = analyze_journal(str(p), None, None, parse_ts(by_since))
    assert len(report["by_violations"]) == 1
    assert report["by_violations"][0]["task_id"] == "t-002"


# ---------------------------------------------------------------------
# 6. duplicate task_id: after accepted / critic-entry / continuation / retry
# ---------------------------------------------------------------------
def test_duplicate_delegate_after_accepted_is_candidate(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-08T00:00:00", "delegated", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),
        ev("2026-07-08T00:10:00", "accepted", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),
        ev("2026-07-08T00:20:00", "delegated", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),  # duplicate/reopen, no attempt>=2, not critic
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    dups = report["duplicate_delegates"]
    assert len(dups) == 1
    assert dups[0]["branch"] == "candidate-duplicate"


def test_duplicate_delegate_critic_entry_is_legal_branch(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-08T00:00:00", "delegated", agent="builder", model="sonnet",
           task_id="t-001", category="implementation", notes="n"),
        ev("2026-07-08T00:10:00", "accepted", agent="builder", model="sonnet",
           task_id="t-001", category="implementation", notes="n", witness="ok"),
        ev("2026-07-08T00:20:00", "delegated", agent="critic", model="opus",
           task_id="t-001", category="review", notes="n"),  # critic-entry on an open/closed task
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    dups = report["duplicate_delegates"]
    assert len(dups) == 1
    assert dups[0]["branch"] == "critic-entry"


def test_duplicate_delegate_continuation_after_rejected(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-08T00:00:00", "delegated", agent="builder", model="sonnet",
           task_id="t-001", category="implementation", notes="n"),
        ev("2026-07-08T00:10:00", "rejected", agent="builder", model="sonnet",
           task_id="t-001", attempt=1, failure_class="spec", category="implementation", notes="n"),
        ev("2026-07-08T00:20:00", "delegated", agent="builder", model="sonnet",
           task_id="t-001", category="implementation", notes="n"),  # continuation, same tier
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    dups = report["duplicate_delegates"]
    assert len(dups) == 1
    assert dups[0]["branch"] == "continuation"


def test_duplicate_delegate_retry_attempt_2(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-08T00:00:00", "delegated", agent="builder", model="sonnet",
           task_id="t-001", category="implementation", notes="n"),
        ev("2026-07-08T00:10:00", "rejected", agent="builder", model="sonnet",
           task_id="t-001", attempt=1, failure_class="tooling", category="implementation", notes="n"),
        ev("2026-07-08T00:20:00", "escalated", agent="builder", model="sonnet",
           task_id="t-001", category="implementation", notes="n"),
        ev("2026-07-08T00:30:00", "delegated", agent="builder", model="sonnet",
           task_id="t-001", attempt=2, category="implementation", notes="n"),  # retry, post-escalation
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    dups = report["duplicate_delegates"]
    assert len(dups) == 1
    assert dups[0]["branch"] == "retry"


# ---------------------------------------------------------------------
# 7. ts non-monotonicity
# ---------------------------------------------------------------------
def test_ts_non_monotonic_detected(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-08T10:00:00", "delegated", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),
        ev("2026-07-08T09:00:00", "accepted", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),  # earlier than the previous line
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    assert len(report["ts_anomalies"]) == 1
    assert report["ts_anomalies"][0]["line"] == 2


# ---------------------------------------------------------------------
# 8. unparsable line
# ---------------------------------------------------------------------
def test_unparsable_line_reported(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-08T00:00:00", "journal_created", notes="init"),
        "{ this is not valid json",
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    assert len(report["unparsable"]) == 1
    assert report["unparsable"][0]["line"] == 2


# ---------------------------------------------------------------------
# 9. the spaced-JSON format (spaces after colons)
# ---------------------------------------------------------------------
def test_external_journal_format_with_spaces_parses(tmp_path):
    p = tmp_path / "j.jsonl"
    raw = ('{"ts": "2026-07-08T00:00:00", "event": "delegated", "agent": "builder", '
           '"category": "implementation", "notes": "n", "task_id": "at-bug-001"}')
    write_journal(p, [raw])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    assert report["parsed_lines"] == 1
    assert report["unparsable"] == []
    assert report["counts"]["by_event"]["delegated"] == 1


# ---------------------------------------------------------------------
# 10. window filter
# ---------------------------------------------------------------------
def test_window_filter_excludes_outside_events(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-08T00:00:00", "delegated", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),
        ev("2026-07-09T00:00:00", "accepted", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),
        ev("2026-07-10T00:00:00", "delegated", agent="scout", model="haiku",
           task_id="t-002", category="recon", notes="n"),
    ])
    start = parse_ts("2026-07-09T00:00:00")
    end = parse_ts("2026-07-10T00:00:00")
    report = analyze_journal(str(p), start, end, parse_ts("2026-07-10T13:14:00"))
    assert report["in_window_count"] == 1
    assert report["counts"]["by_event"] == {"accepted": 1}


# ---------------------------------------------------------------------
# 11. legacy section, pre-typed-fields-schema (migration installs only:
# a fresh install keeps both cutovers at the epoch, so the legacy branch
# is exercised here by patching the cutoff the way a migrating
# deployment would set it)
# ---------------------------------------------------------------------
def test_legacy_events_before_cutoff_not_counted_as_violation(tmp_path, monkeypatch):
    import calibration_counts as cc
    monkeypatch.setattr(cc, "LEGACY_CUTOFF", "2026-07-08T20:00:00")
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        # before the (patched) LEGACY_CUTOFF, rejected with no
        # failure_class -- legacy
        ev("2026-07-08T10:00:00", "delegated", agent="builder", model="sonnet",
           task_id="t-001", category="implementation", notes="n"),
        ev("2026-07-08T10:10:00", "rejected", agent="builder", model="sonnet",
           task_id="t-001", attempt=1, category="implementation", notes="n"),
        # after the cutoff, the same defect -- a real violation
        ev("2026-07-09T10:00:00", "delegated", agent="builder", model="sonnet",
           task_id="t-002", category="implementation", notes="n"),
        ev("2026-07-09T10:10:00", "rejected", agent="builder", model="sonnet",
           task_id="t-002", attempt=1, category="implementation", notes="n"),
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    assert len(report["legacy_events"]) == 1
    assert report["legacy_events"][0]["task_id"] == "t-001"
    field_viol_task_ids = [v["task_id"] for v in report["field_violations"]]
    assert "t-002" in field_viol_task_ids
    assert "t-001" not in field_viol_task_ids


# ---------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------
def test_cli_json_smoke(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-08T00:00:00", "journal_created", notes="init"),
        ev("2026-07-08T01:00:00", "delegated", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),
        ev("2026-07-08T01:10:00", "accepted", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),
    ])
    # a direct call to the module's main() is more reliable than subprocess
    # (not tied to cwd/PYTHONPATH)
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(["--journal", str(p), "--json"])
    assert code == 0
    parsed = json.loads(buf.getvalue())
    assert "journals" in parsed
    assert len(parsed["journals"]) == 1
    assert parsed["journals"][0]["counts"]["by_event"]["delegated"] == 1


def test_cli_text_mode_exit_zero(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-08T00:00:00", "journal_created", notes="init"),
    ])
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(["--journal", str(p)])
    assert code == 0
    assert "journal_created" in buf.getvalue()


def test_cli_invalid_window_start_exit_2(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [ev("2026-07-08T00:00:00", "journal_created", notes="init")])
    code = main(["--journal", str(p), "--window-start", "not-a-date"])
    assert code == 2


def test_cli_missing_file_exit_2(tmp_path):
    code = main(["--journal", str(tmp_path / "does-not-exist.jsonl")])
    assert code == 2


# ---------------------------------------------------------------------
# Schema constants stay in sync with journal_validator (a prior review
# finding): both copies encode the SAME typed-fields schema (a gate on
# write, a counter on read); a silent divergence would quietly skew the
# calibration count.
# ---------------------------------------------------------------------
def test_schema_constants_match_journal_validator():
    import journal_validator as jv
    import calibration_counts as cc
    assert cc.MODEL_REQUIRED_EVENTS == jv.MODEL_REQUIRED_EVENTS
    assert cc.TASK_ID_REQUIRED_EVENTS == jv.TASK_ID_REQUIRED_EVENTS
    assert cc.FAILURE_CLASSES == jv.FAILURE_CLASSES


# ---------------------------------------------------------------------
# The "other" branch (a prior review finding): a repeated delegated
# AFTER escalated with no attempt (a live precedent) -> an honest
# catch-all, surfaced in the report with prior_status for a human
# verdict.
# ---------------------------------------------------------------------
def test_duplicate_delegate_after_escalated_without_attempt_is_other(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-09T01:00:00", "delegated", agent="scout", model="m",
           task_id="t-001", category="recon", notes="n"),
        ev("2026-07-09T01:10:00", "rejected", agent="scout", model="m",
           task_id="t-001", attempt=1, failure_class="tooling", category="recon", notes="n"),
        ev("2026-07-09T01:15:00", "rejected", agent="scout", model="m",
           task_id="t-001", attempt=2, failure_class="tooling", category="recon", notes="n"),
        ev("2026-07-09T01:20:00", "escalated", agent="scout", model="m",
           task_id="t-001", category="recon", notes="n"),
        ev("2026-07-09T01:30:00", "delegated", agent="scout", model="m",
           task_id="t-001", category="recon", notes="attempt 3 with no attempt field"),
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    other = [d for d in report["duplicate_delegates"] if d["branch"] == "other"]
    assert len(other) == 1
    assert other[0]["prior_status"] == "escalated"
    assert other[0]["attempt"] is None


# ---------------------------------------------------------------------
# False-accept rate by tier (a prior review finding).
# ---------------------------------------------------------------------
def test_false_accept_rate_per_agent(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-09T01:00:00", "delegated", agent="builder", model="m",
           task_id="t-001", category="i", notes="n"),
        ev("2026-07-09T01:10:00", "accepted", agent="builder", model="m",
           task_id="t-001", witness="w", category="i", notes="n"),
        ev("2026-07-09T01:20:00", "delegated", agent="builder", model="m",
           task_id="t-002", category="i", notes="n"),
        ev("2026-07-09T01:30:00", "accepted", agent="builder", model="m",
           task_id="t-002", witness="w", category="i", notes="n"),
        ev("2026-07-09T02:00:00", "defect_found", agent="builder", model="m",
           task_id="t-003", ref="t-001", category="i", notes="n"),
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    fa = report["false_accept"]["builder"]
    assert fa == {"defect_found": 1, "accepted": 2, "rate": 0.5}


# ---------------------------------------------------------------------
# Degradation pairs (journal side): a closed pair and an unclosed tail.
# ---------------------------------------------------------------------
def test_degradation_pairs_closed_and_open_tail(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-09T01:00:00", "lead_degraded", agent="lead", model="opus",
           category="degradation", notes="switch down"),
        ev("2026-07-09T02:00:00", "lead_restored", agent="lead", model="fable",
           category="degradation", notes="window review: empty"),
        ev("2026-07-09T03:00:00", "lead_degraded", agent="lead", model="sonnet",
           category="degradation", notes="switch down again"),
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    pairs = report["degradation_pairs"]
    assert len(pairs) == 2
    assert pairs[0]["note"] == "closed"
    assert pairs[0]["restored_line"] == 2
    assert pairs[1]["restored_line"] is None
    assert "NOT CLOSED" in pairs[1]["note"]


# ---------------------------------------------------------------------
# rejected distribution by failure_class x agent x model.
# ---------------------------------------------------------------------
def test_rejected_distribution_grouping(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-09T01:00:00", "delegated", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),
        ev("2026-07-09T01:10:00", "rejected", agent="scout", model="haiku",
           task_id="t-001", attempt=1, failure_class="tooling", category="recon", notes="n"),
        ev("2026-07-09T01:20:00", "delegated", agent="builder", model="sonnet",
           task_id="t-002", category="i", notes="n"),
        ev("2026-07-09T01:30:00", "rejected", agent="builder", model="sonnet",
           task_id="t-002", attempt=1, failure_class="spec", category="i", notes="n"),
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    dist = {(d["failure_class"], d["agent"], d["model"]): d["count"]
            for d in report["rejected_distribution"]}
    assert dist == {("tooling", "scout", "haiku"): 1, ("spec", "builder", "sonnet"): 1}


# ---------------------------------------------------------------------
# Unclosed tasks: last lifecycle event is delegated -> listed.
# ---------------------------------------------------------------------
def test_unclosed_tasks_listed(tmp_path):
    p = tmp_path / "j.jsonl"
    write_journal(p, [
        ev("2026-07-09T01:00:00", "delegated", agent="scout", model="haiku",
           task_id="t-001", category="recon", notes="n"),
        ev("2026-07-09T01:10:00", "delegated", agent="builder", model="sonnet",
           task_id="t-002", category="i", notes="n"),
        ev("2026-07-09T01:30:00", "accepted", agent="builder", model="sonnet",
           task_id="t-002", witness="w", category="i", notes="n"),
    ])
    report = analyze_journal(str(p), None, None, parse_ts("2026-07-10T13:14:00"))
    assert report["unclosed_tasks"] == ["t-001"]
