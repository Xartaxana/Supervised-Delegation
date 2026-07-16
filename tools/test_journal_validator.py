"""Tests for tools/journal_validator.py. Style mirrors
tools/test_mechanism_gate.py: decide() is a pure function, tested
directly with synthetic staged/head text -- no git needed for most
cases. One integration test at the bottom exercises the real git
wiring (is_journal_staged / get_staged_text / get_head_text) against a
real tmp_path git repo, and one exercises main()'s exit-2 crash path.

Run from the repo root: python -m pytest tools/test_journal_validator.py
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import journal_validator as jv

NOW = jv.datetime.datetime(2026, 7, 10, 12, 0, 0)


def _line(event="delegated", ts="2026-07-10T08:00:00", agent="builder",
          category="implementation", notes="note",
          worker_ref="cli:2026-07-10T08:00:00", **kw) -> str:
    obj = {"ts": ts, "event": event, "agent": agent, "category": category, "notes": notes,
           "worker_ref": worker_ref}
    obj.update(kw)
    return json.dumps(obj, ensure_ascii=False)


HEAD_LINE = _line(event="delegated", task_id="t-001", model="sonnet", ts="2026-07-10T08:00:00")
HEAD_TEXT = HEAD_LINE + "\n"


def _staged(*new_lines: str) -> str:
    return HEAD_TEXT + "".join(l + "\n" for l in new_lines)


# ---- not staged at all -> main() must exit 0 silently (tested separately below) ----

# ---- positive case: valid new lines pass clean ----

def test_positive_case_valid_new_lines_pass(tmp_path):
    staged = _staged(
        _line(event="delegated", ts="2026-07-10T08:10:00", task_id="t-002", model="sonnet",
              notes="delegating t-002"),
        _line(event="accepted", ts="2026-07-10T08:20:00", agent="builder", task_id="t-002",
              model="sonnet", witness="pytest ... 1 passed", by="opus",
              notes="accepted t-002"),
    )
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0
    assert violations == []


# ---- 1. append-only ----

def test_append_only_violation_when_existing_line_modified():
    tampered_head = json.loads(HEAD_LINE)
    tampered_head["notes"] = "rewritten"
    staged = json.dumps(tampered_head, ensure_ascii=False) + "\n"
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("append-only" in v for v in violations)


def test_append_only_violation_when_lines_removed():
    code, violations = jv.decide("", HEAD_TEXT, NOW)
    assert code == 1
    assert any("append-only" in v for v in violations)


# ---- 2. required fields ----

def test_missing_required_field_notes_fails():
    obj = json.loads(_line(event="dispatch_skipped", ts="2026-07-10T08:10:00",
                            agent="scout", category="recon", notes="x"))
    del obj["notes"]
    staged = _staged(json.dumps(obj, ensure_ascii=False))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("notes" in v for v in violations)


def test_invalid_json_line_fails():
    staged = HEAD_TEXT + "{not valid json\n"
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("invalid JSON" in v for v in violations)


# ---- 3. event enum ----

def test_unknown_event_fails():
    staged = _staged(_line(event="reticulated", ts="2026-07-10T08:10:00", agent="lead",
                            category="x", notes="x"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("enum" in v for v in violations)


# ---- 4. model required ----

def test_model_missing_for_delegated_fails():
    staged = _staged(_line(event="delegated", ts="2026-07-10T08:10:00", task_id="t-002",
                            notes="no model"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("'model'" in v for v in violations)


# ---- 5. task_id required + format ----

def test_task_id_missing_for_delegated_fails():
    staged = _staged(_line(event="delegated", ts="2026-07-10T08:10:00", model="sonnet",
                            notes="no task_id"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("'task_id'" in v for v in violations)


def test_task_id_bad_format_fails():
    staged = _staged(_line(event="delegated", ts="2026-07-10T08:10:00", model="sonnet",
                            task_id="t-2", notes="bad format"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("t-NNN format" in v for v in violations)


# ---- 5b. worker_ref required for delegated ----

def test_delegated_missing_worker_ref_fails():
    obj = json.loads(_line(event="delegated", ts="2026-07-10T08:10:00", model="sonnet",
                            task_id="t-002", notes="no worker_ref"))
    del obj["worker_ref"]
    staged = _staged(json.dumps(obj, ensure_ascii=False))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("worker_ref" in v for v in violations)


def test_delegated_empty_worker_ref_fails():
    staged = _staged(_line(event="delegated", ts="2026-07-10T08:10:00", model="sonnet",
                            task_id="t-002", worker_ref="", notes="empty worker_ref"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("worker_ref" in v for v in violations)


def test_delegated_whitespace_worker_ref_fails():
    staged = _staged(_line(event="delegated", ts="2026-07-10T08:10:00", model="sonnet",
                            task_id="t-002", worker_ref="   ", notes="whitespace worker_ref"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("worker_ref" in v for v in violations)


def test_delegated_nonstring_worker_ref_fails():
    staged = _staged(_line(event="delegated", ts="2026-07-10T08:10:00", model="sonnet",
                            task_id="t-002", worker_ref=123, notes="nonstring worker_ref"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("worker_ref" in v for v in violations)


def test_delegated_valid_worker_ref_passes():
    staged = _staged(_line(event="delegated", ts="2026-07-10T08:10:00", model="sonnet",
                            task_id="t-002", worker_ref="cli:2026-07-10T08:10:00",
                            notes="valid worker_ref"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


def test_escalated_needs_no_worker_ref():
    obj = json.loads(_line(event="escalated", ts="2026-07-10T08:10:00", agent="builder",
                            model="sonnet", task_id="t-001", notes="escalated, no worker_ref"))
    del obj["worker_ref"]
    staged = _staged(json.dumps(obj, ensure_ascii=False))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


# ---- 6. rejected: attempt / failure_class ----

def test_rejected_invalid_attempt_and_failure_class_fail():
    staged = _staged(_line(event="rejected", ts="2026-07-10T08:10:00", agent="builder",
                            model="sonnet", task_id="t-001", attempt=0,
                            failure_class="mystery", by="opus", notes="bad rejected"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("'attempt'" in v for v in violations)
    assert any("'failure_class'" in v for v in violations)


# ---- 7. accepted + agent=builder: witness ----

def test_accepted_builder_missing_witness_fails():
    staged = _staged(_line(event="accepted", ts="2026-07-10T08:10:00", agent="builder",
                            model="sonnet", task_id="t-001", by="opus",
                            notes="no witness"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("'witness'" in v for v in violations)


# ---- 8. defect_found: ref ----

def test_defect_found_missing_ref_fails():
    staged = _staged(_line(event="defect_found", ts="2026-07-10T08:10:00", agent="builder",
                            task_id="t-001", notes="late defect"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("'ref'" in v for v in violations)


# ---- 9. task_id novelty / reference ----

def test_delegated_novelty_violation_wrong_number():
    staged = _staged(_line(event="delegated", ts="2026-07-10T08:10:00", model="sonnet",
                            task_id="t-005", notes="skipped ahead"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("task_id novelty" in v for v in violations)


def test_delegated_novelty_correct_max_plus_one_passes():
    staged = _staged(_line(event="delegated", ts="2026-07-10T08:10:00", model="sonnet",
                            task_id="t-002", notes="correct next id"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


def test_accepted_references_nonexistent_task_id_fails():
    staged = _staged(_line(event="accepted", ts="2026-07-10T08:10:00", agent="builder",
                            model="sonnet", task_id="t-099", witness="w", by="opus",
                            notes="dangling ref"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("does not reference" in v for v in violations)


def test_accepted_can_reference_task_id_delegated_earlier_in_same_commit():
    # t-002 delegated and then accepted in the SAME staged batch -- rule 9
    # allows referencing task_ids introduced earlier in this very commit,
    # not only ones already in HEAD.
    staged = _staged(
        _line(event="delegated", ts="2026-07-10T08:10:00", model="sonnet", task_id="t-002",
              notes="new task"),
        _line(event="accepted", ts="2026-07-10T08:20:00", agent="builder", model="sonnet",
              task_id="t-002", witness="w", by="opus", notes="accept same-commit task"),
    )
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


# ---- 9 (Lead correction, live precedent): re-delegated task_id --
# a/b/v legal, two g negatives (a real duplicate-delegation defect once
# found in a production journal; delegated after accepted) ----

def test_9a_new_task_max_plus_one_passes():
    # (a) restated for clarity alongside b/v/g below: a brand-new task_id
    # equal to max+1 is legal regardless of any b/v/g machinery.
    staged = _staged(_line(event="delegated", ts="2026-07-10T08:10:00", model="sonnet",
                            task_id="t-002", notes="new task, case a"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


def test_9b_continuation_dispatch_different_agent_passes():
    # (b) t-001 delegated to builder in HEAD; task is still open (no
    # accepted yet); a NEW delegated on the SAME task_id but a DIFFERENT
    # agent (critic acceptance-gate entry) is legal with no attempt/
    # rejected needed -- exactly the pattern a real critic-gate
    # continuation dispatch needs (builder then critic).
    staged = _staged(_line(event="delegated", ts="2026-07-10T08:10:00", agent="critic",
                            model="opus", task_id="t-001",
                            notes="critic-gate continuation dispatch, case b"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


def test_9v_retry_after_rejected_with_attempt_passes():
    # (v) t-001 rejected, then re-delegated to the SAME agent (builder)
    # WITH attempt>=2 -- legal retry.
    staged = _staged(
        _line(event="rejected", ts="2026-07-10T08:10:00", agent="builder", model="sonnet",
              task_id="t-001", attempt=1, failure_class="spec", by="opus", notes="first attempt rejected"),
        _line(event="delegated", ts="2026-07-10T08:20:00", agent="builder", model="sonnet",
              task_id="t-001", attempt=2, notes="retry after rejection, case v"),
    )
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


def test_9g_duplicate_pattern_same_agent_no_attempt_no_rejected_fails():
    # (g) negative #1: an actual defect once found in a production
    # journal -- same agent re-delegated
    # on an open task_id, no attempt field, no rejected above. Must FAIL.
    staged = _staged(_line(event="delegated", ts="2026-07-10T08:10:00", agent="builder",
                            model="sonnet", task_id="t-001", notes="duplicate delegation, no attempt/rejected"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("forbidden duplicate" in v for v in violations)


# ---- 9c2. dead-worker replacement (replaces_worker marker) ----


def test_9c2_replaces_worker_matching_prior_ref_passes():
    # t-001 delegated to builder in HEAD with worker_ref
    # "cli:2026-07-10T08:00:00" (see HEAD_LINE's default worker_ref).
    # A NEW delegated by the SAME agent, no attempt, no rejected above
    # -- but notes carry a replaces_worker marker whose handle matches
    # that exact worker_ref -- legal (a dead-worker replacement, not a
    # rule-6 retry).
    staged = _staged(
        _line(event="delegated", ts="2026-07-10T08:10:00", agent="builder", model="sonnet",
              task_id="t-001", worker_ref="cli:2026-07-10T08:10:00",
              notes="replaces_worker:cli:2026-07-10T08:00:00 (worker died, no verdict)")
    )
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


def test_9c2_replaces_worker_fabricated_handle_fails():
    # The claimed handle does not match ANY earlier delegated
    # worker_ref for this task_id -- a fabricated replacement, FAIL.
    staged = _staged(
        _line(event="delegated", ts="2026-07-10T08:10:00", agent="builder", model="sonnet",
              task_id="t-001", worker_ref="cli:2026-07-10T08:10:00",
              notes="replaces_worker:cli:2026-07-10T07:00:00 (never happened)")
    )
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("fabricated replacement" in v for v in violations)


def test_9c2_replaces_worker_matches_ref_from_a_different_agents_delegated_line():
    # Rule 9(c2) searches worker_ref across delegated lines of ANY
    # agent for this task_id, not only lines by the same agent as the
    # new one: a critic-entry's worker_ref can legitimately be claimed
    # as replaced by a later builder retry.
    staged = _staged(
        _line(event="delegated", ts="2026-07-10T08:10:00", agent="critic", model="opus",
              task_id="t-001", worker_ref="agent:critic-1",
              notes="critic-gate continuation dispatch, case b"),
        _line(event="delegated", ts="2026-07-10T08:20:00", agent="critic", model="opus",
              task_id="t-001", worker_ref="agent:critic-2",
              notes="replaces_worker:agent:critic-1 (critic-1 died mid-review)"),
    )
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


def test_9c2_replaces_worker_handle_from_unrelated_task_id_does_not_count():
    # A handle that is a real worker_ref, but for a DIFFERENT task_id,
    # must not satisfy rule 9(c2) for this one -- the prior-refs set
    # is scoped per task_id.
    staged = _staged(
        _line(event="delegated", ts="2026-07-10T08:10:00", model="sonnet", task_id="t-002",
              worker_ref="cli:2026-07-10T08:10:00", notes="unrelated task"),
        _line(event="delegated", ts="2026-07-10T08:20:00", agent="builder", model="sonnet",
              task_id="t-001", worker_ref="cli:2026-07-10T08:20:00",
              notes="replaces_worker:cli:2026-07-10T08:10:00 (wrong task's ref)"),
    )
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("fabricated replacement" in v for v in violations)


def test_9c2_replaces_worker_does_not_require_attempt_field():
    # rule 9(c2) explicitly does not require attempt to grow -- a bare
    # replaces_worker marker with no attempt field at all is legal.
    obj = json.loads(
        _line(event="delegated", ts="2026-07-10T08:10:00", agent="builder", model="sonnet",
              task_id="t-001", worker_ref="cli:2026-07-10T08:10:00",
              notes="replaces_worker:cli:2026-07-10T08:00:00")
    )
    assert "attempt" not in obj
    staged = _staged(json.dumps(obj, ensure_ascii=False))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


def test_9c2_replaces_worker_takes_priority_over_plain_duplicate_fail():
    # Sanity: without the marker, the same shape of line fails as a
    # plain duplicate (case d) -- proves the c2 test above is actually
    # exercising the marker branch, not some other path to a pass.
    staged = _staged(
        _line(event="delegated", ts="2026-07-10T08:10:00", agent="builder", model="sonnet",
              task_id="t-001", notes="no replaces_worker marker here at all")
    )
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("forbidden duplicate" in v for v in violations)


def test_9g_delegated_after_accepted_fails_reopen_forbidden():
    # (g) negative #2: task_id already closed (accepted above) -- a new
    # delegated on it is a forbidden reopen (D-0060: treat as two tasks),
    # regardless of which agent issues it.
    head_with_accept = HEAD_TEXT + _line(
        event="accepted", ts="2026-07-10T08:05:00", agent="builder", model="sonnet",
        task_id="t-001", witness="pytest ok", by="opus", notes="t-001 already accepted",
    ) + "\n"
    staged = head_with_accept + _line(event="delegated", ts="2026-07-10T08:10:00", agent="critic",
                                       model="opus", task_id="t-001", notes="reopen attempt") + "\n"
    code, violations = jv.decide(staged, head_with_accept, NOW)
    assert code == 1
    assert any("reopen forbidden" in v for v in violations)


# ---- 10. ts monotonicity / no narrative future ----

def test_ts_not_monotonic_relative_to_previous_new_line_fails():
    staged = _staged(
        _line(event="delegated", ts="2026-07-10T08:20:00", model="sonnet", task_id="t-002",
              notes="later"),
        _line(event="delegated", ts="2026-07-10T08:10:00", model="sonnet", task_id="t-003",
              notes="earlier than previous new line"),
    )
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("not monotonic" in v for v in violations)


def test_ts_earlier_than_last_head_line_fails():
    staged = _staged(_line(event="delegated", ts="2026-07-10T07:00:00", model="sonnet",
                            task_id="t-002", notes="before HEAD's last ts"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("not monotonic" in v for v in violations)


def test_ts_narrative_future_beyond_now_plus_10min_fails():
    staged = _staged(_line(event="delegated", ts="2026-07-11T00:00:00", model="sonnet",
                            task_id="t-002", notes="far future (narrative-future timestamp)"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("narrative-future" in v for v in violations)


def test_ts_within_10min_future_grace_passes():
    staged = _staged(_line(event="delegated", ts="2026-07-10T12:05:00", model="sonnet",
                            task_id="t-002", notes="clock skew grace"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


# ---- 11. role-vs-tier acceptance matrix ----

def test_matrix_missing_by_fails():
    staged = _staged(_line(event="accepted", ts="2026-07-10T08:10:00", agent="scout",
                            model="haiku", task_id="t-001", witness="w",
                            notes="no by field"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("'by'" in v for v in violations)


def test_matrix_scout_accepted_by_same_tier_without_basis_fails():
    staged = _staged(_line(event="accepted", ts="2026-07-10T08:10:00", agent="scout",
                            model="haiku", task_id="t-001", by="haiku",
                            notes="peer accepting peer, no basis"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("role-vs-tier" in v for v in violations)


def test_matrix_scout_accepted_by_higher_tier_passes():
    staged = _staged(_line(event="accepted", ts="2026-07-10T08:10:00", agent="scout",
                            model="haiku", task_id="t-001", by="opus",
                            notes="opus accepts scout"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


def test_matrix_scout_accepted_same_tier_with_basis_passes():
    staged = _staged(_line(event="accepted", ts="2026-07-10T08:10:00", agent="scout",
                            model="haiku", task_id="t-001", by="haiku", basis="queued-to-lead",
                            notes="basis fallback"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


def test_matrix_non_claude_by_requires_basis():
    staged = _staged(_line(event="accepted", ts="2026-07-10T08:10:00", agent="builder",
                            model="sonnet", task_id="t-001", witness="w", by="gemini-2.5-flash",
                            notes="non-Claude by, no basis"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 1
    assert any("role-vs-tier" in v for v in violations)


def test_matrix_non_claude_by_with_basis_critic_passes():
    staged = _staged(_line(event="accepted", ts="2026-07-10T08:10:00", agent="builder",
                            model="sonnet", task_id="t-001", witness="w", by="gemini-2.5-flash",
                            basis="critic", notes="non-Claude by, critic basis"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


def test_matrix_agent_lead_needs_only_presence_of_by():
    staged = _staged(_line(event="accepted", ts="2026-07-10T08:10:00", agent="lead",
                            model="fable", task_id="t-001", by="haiku",
                            notes="lead-tier accept, matrix not applied"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


def test_matrix_rejected_only_needs_by_present_no_tier_check():
    # literal reading of the spec: tier/basis check text only names
    # "accepted"; rejected carries 'by' without a further tier/basis gate.
    staged = _staged(_line(event="rejected", ts="2026-07-10T08:10:00", agent="scout",
                            model="haiku", task_id="t-001", attempt=1, failure_class="recon",
                            by="haiku", notes="rejected, same-tier by, no basis"))
    code, violations = jv.decide(staged, HEAD_TEXT, NOW)
    assert code == 0


# ---- HEAD empty (first-ever commit / fresh deploy) ----

def test_empty_head_first_delegated_must_be_t001():
    staged = _line(event="delegated", ts="2026-07-10T08:00:00", model="sonnet", task_id="t-001",
                   notes="very first task") + "\n"
    code, violations = jv.decide(staged, "", NOW)
    assert code == 0


def test_empty_head_no_lower_ts_bound():
    staged = _line(event="delegated", ts="2020-01-01T00:00:00", model="sonnet", task_id="t-001",
                   notes="old ts, no HEAD to compare against") + "\n"
    code, violations = jv.decide(staged, "", NOW)
    assert code == 0


# ---- crash path: main() fail-closed with exit 2 on unexpected exception ----

def test_main_crashes_exit_2_with_traceback(monkeypatch, capsys):
    def _boom():
        raise RuntimeError("simulated crash, not a validation FAIL")

    monkeypatch.setattr(jv, "is_journal_staged", _boom)
    code = jv.main([])
    assert code == 2
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "simulated crash" in err


# ---- real git integration: not-staged -> exit 0 silently; staged violation -> exit 1 ----

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")


def _init_repo(root: Path):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")


def test_main_exits_zero_when_journal_not_staged(tmp_path, capsys, monkeypatch):
    root = tmp_path
    _init_repo(root)
    (root / "logs").mkdir()
    (root / "logs" / "routing-log.jsonl").write_text(HEAD_TEXT, encoding="utf-8")
    _git(root, "add", "logs/routing-log.jsonl")
    _git(root, "commit", "-q", "-m", "seed journal")
    # nothing staged now (working tree clean)
    monkeypatch.chdir(root)
    code = jv.main([])
    assert code == 0
    assert capsys.readouterr().out == ""


def test_main_exits_one_on_real_staged_violation(tmp_path, capsys, monkeypatch):
    root = tmp_path
    _init_repo(root)
    (root / "logs").mkdir()
    (root / "logs" / "routing-log.jsonl").write_text(HEAD_TEXT, encoding="utf-8")
    _git(root, "add", "logs/routing-log.jsonl")
    _git(root, "commit", "-q", "-m", "seed journal")
    bad_line = _line(event="delegated", ts="2026-07-10T08:10:00", task_id="t-999", model="sonnet",
                      notes="wrong novelty")
    (root / "logs" / "routing-log.jsonl").write_text(_staged(bad_line), encoding="utf-8")
    _git(root, "add", "logs/routing-log.jsonl")
    monkeypatch.chdir(root)
    code = jv.main([])
    assert code == 1
    err = capsys.readouterr().err
    assert "FAILED validation" in err
    assert "task_id novelty" in err
