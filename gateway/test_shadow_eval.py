"""Tests for Shadow Evaluation. No live model/proxy required:
litellm mock_response short-circuits replay() (same trick as test_analyst.py).

Run: python -m pytest gateway/test_shadow_eval.py
"""

import datetime
import json

import pytest

from shadow_eval import (
    _extract_cost,
    aggregate_by_category,
    append_evidence_log,
    calibrate,
    decide_status,
    evaluate,
    format_report,
    judge_pair,
    parse_verdict,
    record_evidence,
    replay,
    sample_requests,
    similarity,
)


@pytest.fixture()
def conn(tmp_path):
    import sqlite3

    from sqlite_logger import SCHEMA

    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.execute(SCHEMA)
    return conn


def seed(conn, model, prompt_messages, response, cost=0.01, status="success", ts=None,
         traffic_kind=None):
    if traffic_kind is None:
        conn.execute(
            "INSERT INTO requests (ts, model, status, cost_usd, prompt, response)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                ts or datetime.datetime.now().isoformat(),
                model,
                status,
                cost,
                json.dumps(prompt_messages),
                response,
            ),
        )
    else:
        conn.execute(
            "INSERT INTO requests (ts, model, status, cost_usd, prompt, response, traffic_kind)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ts or datetime.datetime.now().isoformat(),
                model,
                status,
                cost,
                json.dumps(prompt_messages),
                response,
                traffic_kind,
            ),
        )
    conn.commit()


def test_similarity_identical_and_different():
    assert similarity("same text", "same text") == 1.0
    assert similarity("abc", "xyz") == 0.0
    assert 0 < similarity("summarize this please", "summarize this now") < 1


def test_sample_requests_filters_model_and_status(conn):
    seed(conn, "lead", [{"role": "user", "content": "hi"}], "hello")
    seed(conn, "intern", [{"role": "user", "content": "hi"}], "hello")
    seed(conn, "lead", [{"role": "user", "content": "fail"}], None, status="failure")

    rows = sample_requests(conn, "lead", days=7, limit=10)
    assert len(rows) == 1
    assert rows[0]["response"] == "hello"


def test_sample_requests_excludes_judge_calls(conn):
    from shadow_eval import JUDGE_SYSTEM_PROMPT

    seed(conn, "lead", [{"role": "user", "content": "real task"}], "real answer")
    seed(
        conn, "lead",
        [{"role": "system", "content": JUDGE_SYSTEM_PROMPT},
         {"role": "user", "content": "Task:\nx\n\nAnswer A:\na\n\nAnswer B:\nb\n\nVerdict:"}],
        "EQUIVALENT",
    )

    rows = sample_requests(conn, "lead", days=7, limit=10)
    assert len(rows) == 1
    assert rows[0]["response"] == "real answer"


def test_sample_requests_excludes_replay_and_judge_traffic_kind(conn):
    seed(conn, "lead", [{"role": "user", "content": "real task"}], "real answer",
         traffic_kind="real")
    seed(conn, "lead", [{"role": "user", "content": "replayed task"}], "replayed answer",
         traffic_kind="replay")
    seed(conn, "lead", [{"role": "user", "content": "judge task"}], "judge answer",
         traffic_kind="judge")
    seed(conn, "lead", [{"role": "user", "content": "synthetic task"}], "synthetic answer",
         traffic_kind="synthetic")

    rows = sample_requests(conn, "lead", days=7, limit=10)
    responses = {r["response"] for r in rows}
    assert responses == {"real answer", "synthetic answer"}


def test_replay_tags_traffic_kind_as_replay(monkeypatch):
    import shadow_eval

    real_completion = shadow_eval.litellm.completion
    captured = {}

    def fake_completion(**kwargs):
        captured["extra_body"] = dict(kwargs.get("extra_body") or {})
        kwargs.setdefault("mock_response", "ok")
        return real_completion(**kwargs)

    monkeypatch.setattr(shadow_eval.litellm, "completion", fake_completion)
    replay([{"role": "user", "content": "hi"}], "intern", "http://localhost:4000")
    assert captured["extra_body"] == {"metadata": {"traffic_kind": "replay"}}


def test_replay_passes_max_tokens_to_completion_when_set(monkeypatch):
    # replay() must forward max_tokens to litellm.completion so the replay
    # target doesn't fall back to its provider's own default cap.
    import shadow_eval

    real_completion = shadow_eval.litellm.completion
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        kwargs.setdefault("mock_response", "ok")
        return real_completion(**kwargs)

    monkeypatch.setattr(shadow_eval.litellm, "completion", fake_completion)
    replay([{"role": "user", "content": "hi"}], "intern", "http://localhost:4000", max_tokens=500)
    assert captured["max_tokens"] == 500


def test_replay_omits_max_tokens_when_none(monkeypatch):
    # None preserves the historical not-passed behavior: no max_tokens
    # kwarg at all (e.g. judge_pair's short verdict calls don't need one).
    import shadow_eval

    real_completion = shadow_eval.litellm.completion
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        kwargs.setdefault("mock_response", "ok")
        return real_completion(**kwargs)

    monkeypatch.setattr(shadow_eval.litellm, "completion", fake_completion)
    replay([{"role": "user", "content": "hi"}], "intern", "http://localhost:4000", max_tokens=None)
    assert "max_tokens" not in captured


def test_replay_returns_finish_reason(monkeypatch):
    text, cost, finish_reason = replay(
        [{"role": "user", "content": "hi"}], "intern", "http://localhost:4000",
        mock_response="ok",
    )
    assert finish_reason == "stop"


def test_judge_pair_tags_traffic_kind_as_judge(monkeypatch):
    import shadow_eval

    real_completion = shadow_eval.litellm.completion
    captured = {}

    def fake_completion(**kwargs):
        captured["extra_body"] = dict(kwargs.get("extra_body") or {})
        kwargs.setdefault("mock_response", "EQUIVALENT")
        return real_completion(**kwargs)

    monkeypatch.setattr(shadow_eval.litellm, "completion", fake_completion)
    judge_pair("task", "a", "b", "judge-alias", "http://localhost:4000")
    assert captured["extra_body"] == {"metadata": {"traffic_kind": "judge"}}


def test_evaluate_uses_mock_response(conn):
    seed(conn, "lead", [{"role": "user", "content": "summarize this article"}], "a short summary")

    results = evaluate(
        conn, "lead", "intern", "http://localhost:4000", days=7, sample_n=10,
        mock_response="a short summary",
    )
    assert len(results) == 1
    assert results[0]["category"] == "summarization"
    assert results[0]["similarity"] == 1.0
    assert results[0]["error"] is None


def test_evaluate_categories_whitelist(conn):
    seed(conn, "lead", [{"role": "user", "content": "summarize this article"}], "a summary")
    seed(conn, "lead", [{"role": "user", "content": "write a python function"}], "def f(): pass")

    results = evaluate(
        conn, "lead", "intern", "http://localhost:4000", days=7, sample_n=10,
        categories={"coding"}, mock_response="def f(): pass",
    )
    assert len(results) == 1
    assert results[0]["category"] == "coding"


def test_evaluate_records_replay_errors(conn):
    seed(conn, "lead", [{"role": "user", "content": "hi"}], "hello")

    results = evaluate(
        conn, "lead", "nonexistent-model-xyz", "http://localhost:4000", days=7, sample_n=10,
    )
    assert results[0]["error"] is not None
    assert results[0]["similarity"] == 0.0


def test_auto_max_tokens_scales_and_floors():
    from shadow_eval import _auto_max_tokens

    assert _auto_max_tokens(10000) == int(10000 * 1.3)
    assert _auto_max_tokens(1000) == 8192  # 1.3x would be below the floor
    assert _auto_max_tokens(0) == 8192
    assert _auto_max_tokens(None) == 8192


def test_evaluate_computes_max_tokens_from_source_completion_tokens(conn, monkeypatch):
    # The pair's max_tokens must be derived from the SOURCE row's own
    # completion_tokens (max(source * 1.3, 8192)), not left unset.
    import shadow_eval

    conn.execute(
        "INSERT INTO requests (ts, model, status, cost_usd, prompt, response, completion_tokens)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.datetime.now().isoformat(), "lead", "success", 0.01,
            json.dumps([{"role": "user", "content": "hi"}]), "hello", 10000,
        ),
    )
    conn.commit()

    captured = {}

    def fake_replay(messages, target_model, gateway, db_path=None, max_tokens=None, **kwargs):
        captured["max_tokens"] = max_tokens
        return "hello", 0.001, "stop"

    monkeypatch.setattr(shadow_eval, "replay", fake_replay)
    evaluate(conn, "lead", "intern", "http://localhost:4000", days=7, sample_n=10)
    assert captured["max_tokens"] == max(int(10000 * 1.3), 8192)


def test_evaluate_floors_max_tokens_when_source_completion_tokens_null(conn, monkeypatch):
    import shadow_eval

    seed(conn, "lead", [{"role": "user", "content": "hi"}], "hello")  # completion_tokens NULL

    captured = {}

    def fake_replay(messages, target_model, gateway, db_path=None, max_tokens=None, **kwargs):
        captured["max_tokens"] = max_tokens
        return "hello", 0.001, "stop"

    monkeypatch.setattr(shadow_eval, "replay", fake_replay)
    evaluate(conn, "lead", "intern", "http://localhost:4000", days=7, sample_n=10)
    assert captured["max_tokens"] == 8192


def test_evaluate_max_tokens_override_bypasses_auto_calc(conn, monkeypatch):
    import shadow_eval

    conn.execute(
        "INSERT INTO requests (ts, model, status, cost_usd, prompt, response, completion_tokens)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.datetime.now().isoformat(), "lead", "success", 0.01,
            json.dumps([{"role": "user", "content": "hi"}]), "hello", 10000,
        ),
    )
    conn.commit()

    captured = {}

    def fake_replay(messages, target_model, gateway, db_path=None, max_tokens=None, **kwargs):
        captured["max_tokens"] = max_tokens
        return "hello", 0.001, "stop"

    monkeypatch.setattr(shadow_eval, "replay", fake_replay)
    evaluate(conn, "lead", "intern", "http://localhost:4000", days=7, sample_n=10,
             max_tokens_override=2000)
    assert captured["max_tokens"] == 2000


def test_evaluate_counts_truncated_on_finish_reason_length(conn, monkeypatch):
    import shadow_eval

    seed(conn, "lead", [{"role": "user", "content": "hi"}], "hello")

    def fake_replay(messages, target_model, gateway, db_path=None, max_tokens=None, **kwargs):
        return "partial answer", 0.001, "length"

    monkeypatch.setattr(shadow_eval, "replay", fake_replay)
    results = evaluate(conn, "lead", "intern", "http://localhost:4000", days=7, sample_n=10)
    assert results[0]["truncated"] is True


def test_evaluate_truncated_false_when_finish_reason_unavailable(conn, monkeypatch):
    # mock/provider not returning finish_reason must not be counted and
    # must not raise.
    import shadow_eval

    seed(conn, "lead", [{"role": "user", "content": "hi"}], "hello")

    def fake_replay(messages, target_model, gateway, db_path=None, max_tokens=None, **kwargs):
        return "answer", 0.001, None

    monkeypatch.setattr(shadow_eval, "replay", fake_replay)
    results = evaluate(conn, "lead", "intern", "http://localhost:4000", days=7, sample_n=10)
    assert results[0]["truncated"] is False


def test_aggregate_by_category():
    results = [
        {"category": "coding", "source_cost_usd": 0.02, "target_cost_usd": 0.001, "similarity": 0.8, "error": None},
        {"category": "coding", "source_cost_usd": 0.03, "target_cost_usd": 0.002, "similarity": 0.6, "error": None},
        {"category": "other", "source_cost_usd": 0.01, "target_cost_usd": 0.0, "similarity": 0.0, "error": "boom"},
    ]
    agg = aggregate_by_category(results)
    assert agg["coding"]["n"] == 2
    assert agg["coding"]["mean_similarity"] == pytest.approx(0.7)
    assert agg["other"]["errors"] == 1


def test_aggregate_by_category_counts_truncated():
    results = [
        {"category": "coding", "source_cost_usd": 0.02, "target_cost_usd": 0.001,
         "similarity": 0.8, "error": None, "truncated": True},
        {"category": "coding", "source_cost_usd": 0.03, "target_cost_usd": 0.002,
         "similarity": 0.6, "error": None, "truncated": False},
    ]
    agg = aggregate_by_category(results)
    assert agg["coding"]["truncated"] == 1


def test_aggregate_by_category_truncated_defaults_to_zero_without_key():
    # Older-shaped result dicts (no "truncated" key) must not crash aggregation.
    results = [
        {"category": "coding", "source_cost_usd": 0.02, "target_cost_usd": 0.001,
         "similarity": 0.8, "error": None},
    ]
    agg = aggregate_by_category(results)
    assert agg["coding"]["truncated"] == 0


class _FakeResponse:
    def __init__(self, hidden_params):
        self._hidden_params = hidden_params


def test_extract_cost_uses_hidden_params_response_cost():
    response = _FakeResponse({"response_cost": 0.0042})
    cost = _extract_cost(response, "builder", db_path=None, call_start=datetime.datetime.now())
    assert cost == 0.0042


def test_extract_cost_falls_back_to_db_when_hidden_params_missing(tmp_path):
    import sqlite3

    from sqlite_logger import SCHEMA

    db_path = tmp_path / "requests.db"
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    call_start = datetime.datetime(2026, 1, 1, 12, 0, 0)
    conn.execute(
        "INSERT INTO requests (ts, model, status, cost_usd) VALUES (?, ?, ?, ?)",
        ((call_start + datetime.timedelta(seconds=1)).isoformat(), "builder", "success", 0.0191),
    )
    conn.commit()

    response = _FakeResponse({"response_cost": None})
    cost = _extract_cost(response, "builder", db_path=str(db_path), call_start=call_start)
    assert cost == 0.0191


def test_extract_cost_returns_none_when_unavailable():
    response = _FakeResponse({"response_cost": None})
    assert _extract_cost(response, "builder", db_path=None, call_start=datetime.datetime.now()) is None


def test_extract_cost_returns_none_when_no_matching_db_row(tmp_path):
    import sqlite3

    from sqlite_logger import SCHEMA

    db_path = tmp_path / "requests.db"
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    conn.commit()

    response = _FakeResponse({})
    cost = _extract_cost(response, "builder", db_path=str(db_path), call_start=datetime.datetime.now())
    assert cost is None


def test_aggregate_by_category_computes_mean_judge_cost():
    results = [
        {"category": "coding", "source_cost_usd": 0.02, "target_cost_usd": 0.001, "similarity": 0.8,
         "verdict": "equivalent", "judge_cost_usd": 0.0004, "error": None},
        {"category": "coding", "source_cost_usd": 0.03, "target_cost_usd": 0.002, "similarity": 0.6,
         "verdict": "equivalent", "judge_cost_usd": 0.0006, "error": None},
    ]
    agg = aggregate_by_category(results)
    assert agg["coding"]["mean_judge_cost_usd"] == pytest.approx(0.0005)


def test_aggregate_by_category_mean_judge_cost_none_when_no_judge_run():
    results = [
        {"category": "coding", "source_cost_usd": 0.02, "target_cost_usd": 0.001, "similarity": 0.8, "error": None},
    ]
    agg = aggregate_by_category(results)
    assert agg["coding"]["mean_judge_cost_usd"] is None


def test_format_report_includes_truncated_count():
    aggregated = {
        "coding": {"n": 2, "mean_similarity": 0.5, "mean_source_cost_usd": 0.02,
                   "mean_target_cost_usd": 0.001, "errors": 0, "truncated": 3},
    }
    statuses = {"coding": "estimated"}
    report = format_report("lead", "intern", aggregated, statuses)
    assert "errors=0 truncated=3" in report


def test_decide_status_provisionally_validated():
    agg = {"n": 3, "mean_similarity": 0.9, "mean_source_cost_usd": 0.02, "mean_target_cost_usd": 0.001, "errors": 0}
    assert decide_status(agg, similarity_threshold=0.5, min_samples=2) == "provisionally_validated"


def test_decide_status_rejected_low_similarity():
    agg = {"n": 3, "mean_similarity": 0.1, "mean_source_cost_usd": 0.02, "mean_target_cost_usd": 0.001, "errors": 0}
    assert decide_status(agg, similarity_threshold=0.5, min_samples=2) == "rejected"


def test_decide_status_estimated_when_not_enough_samples():
    agg = {"n": 1, "mean_similarity": 0.9, "mean_source_cost_usd": 0.02, "mean_target_cost_usd": 0.001, "errors": 0}
    assert decide_status(agg, similarity_threshold=0.5, min_samples=2) == "estimated"


def test_decide_status_estimated_when_all_errored():
    agg = {"n": 2, "mean_similarity": 0.0, "mean_source_cost_usd": 0.02, "mean_target_cost_usd": 0.0, "errors": 2}
    assert decide_status(agg, similarity_threshold=0.5, min_samples=2) == "estimated"


def test_parse_verdict():
    assert parse_verdict("EQUIVALENT") == "equivalent"
    assert parse_verdict("The answer is WORSE.") == "target_worse"
    assert parse_verdict("<think>worse? no, equal quality</think>EQUIVALENT") == "equivalent"
    assert parse_verdict("no keyword here") is None
    assert parse_verdict("") is None
    # last keyword wins when the judge restates both options first
    assert parse_verdict("Either EQUIVALENT or WORSE... verdict: EQUIVALENT") == "equivalent"


def test_judge_pair_with_mock():
    verdict, cost = judge_pair(
        "Summarize X", "short summary", "verbose but correct summary",
        "judge-alias", "http://localhost:4000", mock_response="EQUIVALENT",
    )
    assert verdict == "equivalent"
    assert cost is None


def test_evaluate_with_judge_records_verdict(conn):
    seed(conn, "lead", [{"role": "user", "content": "summarize this article"}], "a summary")

    results = evaluate(
        conn, "lead", "intern", "http://localhost:4000", days=7, sample_n=10,
        judge_model="judge-alias", mock_response="EQUIVALENT",
    )
    assert results[0]["verdict"] == "equivalent"


def test_aggregate_pass_rate():
    results = [
        {"category": "coding", "source_cost_usd": 0.02, "target_cost_usd": 0.001, "similarity": 0.1, "verdict": "equivalent", "error": None},
        {"category": "coding", "source_cost_usd": 0.03, "target_cost_usd": 0.002, "similarity": 0.1, "verdict": "target_worse", "error": None},
    ]
    agg = aggregate_by_category(results)
    assert agg["coding"]["pass_rate"] == pytest.approx(0.5)


def test_decide_status_judge_overrides_similarity():
    # low difflib sim but judge says equivalent -> provisionally_validated
    agg = {"n": 2, "mean_similarity": 0.1, "mean_source_cost_usd": 0.02,
           "mean_target_cost_usd": 0.001, "errors": 0, "pass_rate": 1.0}
    assert decide_status(agg, similarity_threshold=0.5, min_samples=2) == "provisionally_validated"
    # high sim but judge says worse -> rejected
    agg["pass_rate"] = 0.0
    agg["mean_similarity"] = 0.9
    assert decide_status(agg, similarity_threshold=0.5, min_samples=2) == "rejected"


def test_decide_status_vocabulary_pin():
    # decide_status must only ever return one of the 4-status
    # DELEGATION_TABLE.md vocabulary's non-production values (D-0035) --
    # "estimated" (inconclusive), "provisionally_validated" (positive
    # shadow-eval result), or "rejected". It must NEVER return the bare
    # legacy word "validated" (not in the 4-status model) nor
    # "production_validated" (a shadow-eval run is one-shot evidence,
    # never sufficient for that status per the module's own docstring).
    representative_aggs = [
        # too few samples -> estimated
        {"n": 1, "mean_similarity": 0.9, "mean_source_cost_usd": 0.02,
         "mean_target_cost_usd": 0.001, "errors": 0},
        # all errored -> estimated
        {"n": 2, "mean_similarity": 0.0, "mean_source_cost_usd": 0.02,
         "mean_target_cost_usd": 0.0, "errors": 2},
        # difflib path, high similarity -> provisionally_validated
        {"n": 3, "mean_similarity": 0.9, "mean_source_cost_usd": 0.02,
         "mean_target_cost_usd": 0.001, "errors": 0},
        # difflib path, low similarity -> rejected
        {"n": 3, "mean_similarity": 0.1, "mean_source_cost_usd": 0.02,
         "mean_target_cost_usd": 0.001, "errors": 0},
        # target costs more than source -> rejected
        {"n": 3, "mean_similarity": 0.9, "mean_source_cost_usd": 0.001,
         "mean_target_cost_usd": 0.02, "errors": 0},
        # judge path, pass_rate above threshold -> provisionally_validated
        {"n": 2, "mean_similarity": 0.1, "mean_source_cost_usd": 0.02,
         "mean_target_cost_usd": 0.001, "errors": 0, "pass_rate": 1.0},
        # judge path, pass_rate below threshold -> rejected
        {"n": 2, "mean_similarity": 0.9, "mean_source_cost_usd": 0.02,
         "mean_target_cost_usd": 0.001, "errors": 0, "pass_rate": 0.0},
    ]
    verdicts = {
        decide_status(agg, similarity_threshold=0.5, min_samples=2)
        for agg in representative_aggs
    }
    assert verdicts <= {"estimated", "provisionally_validated", "rejected"}
    assert verdicts  # sanity: the representative set actually exercised something
    # Literal check: the bare word "validated" must never occur as a
    # standalone verdict -- only as the suffix of "provisionally_validated"
    # (or "production_validated", which decide_status must never return at all).
    for verdict in verdicts:
        assert verdict != "validated"
        if "validated" in verdict:
            assert verdict in ("provisionally_validated", "production_validated")


def test_calibrate_reports_agreement_and_mismatches():
    pairs = [
        {"prompt": "task1", "source_response": "a", "target_response": "b",
         "category": "coding", "verdict": "equivalent"},
        {"prompt": "task2", "source_response": "a", "target_response": "b",
         "category": "classification", "verdict": "target_worse"},
    ]
    report = calibrate(pairs, "judge-alias", "http://localhost:4000",
                       mock_response="EQUIVALENT")
    assert report["n"] == 2
    assert report["agreements"] == 1
    assert report["mismatches"][0]["category"] == "classification"
    assert report["mismatches"][0]["got"] == "equivalent"


def test_record_evidence_writes_evidence_line_includes_judge_cost(tmp_path):
    # Status cells stay in DELEGATION_TABLE.md (moved only by weekly
    # calibration, Update Rule 1), evidence lines go to a SEPARATE
    # docs/SHADOW_EVALUATION_LOG.md file that record_evidence writes
    # exclusively.
    shadow_log_path = tmp_path / "SHADOW_EVALUATION_LOG.md"
    shadow_log_path.write_text(
        "# Shadow Evaluation Log\n\n"
        "Evidence for DELEGATION_TABLE.md Update Rule 1.\n\n",
        encoding="utf-8",
    )
    aggregated = {
        "summarization": {
            "n": 2, "mean_similarity": 0.9, "mean_source_cost_usd": 0.002,
            "mean_target_cost_usd": 0.0001, "pass_rate": 1.0,
            "mean_judge_cost_usd": 0.0004, "errors": 0,
        }
    }
    statuses = {"summarization": "provisionally_validated"}
    record_evidence(
        shadow_log_path, "2026-01-01", "lead", "builder",
        aggregated, statuses, judge_model="judge",
    )
    log_text = shadow_log_path.read_text(encoding="utf-8")
    assert "judge=judge pass_rate=1.00 judge_cost=$0.0004" in log_text
    assert "-> provisionally_validated" in log_text


def test_record_evidence_judge_cost_unknown(tmp_path):
    # Rule #1 decoupling: when the judge ran (pass_rate present) but cost
    # extraction failed, the evidence line must still carry judge= and
    # pass_rate=, with an explicit judge_cost=unknown instead of dropping
    # the segment.
    shadow_log_path = tmp_path / "SHADOW_EVALUATION_LOG.md"
    shadow_log_path.write_text("# Shadow Evaluation Log\n\n", encoding="utf-8")
    aggregated = {
        "summarization": {
            "n": 2, "mean_similarity": 0.9, "mean_source_cost_usd": 0.002,
            "mean_target_cost_usd": 0.0001, "pass_rate": 1.0,
            "mean_judge_cost_usd": None, "errors": 0,
        }
    }
    statuses = {"summarization": "provisionally_validated"}
    record_evidence(
        shadow_log_path, "2026-01-01", "lead", "builder",
        aggregated, statuses, judge_model="judge",
    )
    log_text = shadow_log_path.read_text(encoding="utf-8")
    assert "judge=judge pass_rate=1.00 judge_cost=unknown" in log_text


def test_record_evidence_creates_shadow_log_when_missing(tmp_path):
    # If docs/SHADOW_EVALUATION_LOG.md doesn't exist yet (fresh checkout,
    # or a path typo), append_evidence_log's own no-heading branch creates
    # it -- this must not raise even though shadow_log_path.exists() is False.
    shadow_log_path = tmp_path / "SHADOW_EVALUATION_LOG.md"
    assert not shadow_log_path.exists()
    aggregated = {
        "summarization": {
            "n": 2, "mean_similarity": 0.9, "mean_source_cost_usd": 0.002,
            "mean_target_cost_usd": 0.0001, "pass_rate": None,
            "mean_judge_cost_usd": None, "errors": 0,
        }
    }
    statuses = {"summarization": "provisionally_validated"}
    record_evidence(
        shadow_log_path, "2026-01-01", "lead", "builder",
        aggregated, statuses,
    )
    assert shadow_log_path.exists()
    log_text = shadow_log_path.read_text(encoding="utf-8")
    assert "# Shadow Evaluation Log" in log_text
    assert "category=summarization" in log_text


def test_record_evidence_does_not_touch_delegation_table(tmp_path):
    # The code path that once wrote status cells into DELEGATION_TABLE.md
    # was removed -- record_evidence's signature no longer accepts a
    # table_path at all, and a DELEGATION_TABLE.md placed next to the
    # shadow log in tmp_path must come out byte-for-byte unchanged: table
    # statuses move only via weekly calibration (Update Rule 1).
    table_path = tmp_path / "DELEGATION_TABLE.md"
    original_table_bytes = (
        "| Task type | Cost (Lead) | Value of Lead | Delegate to | Status |\n"
        "|---|---|---|---|---|\n"
        "| Summarization | Medium | Medium | Junior | estimated |\n"
    ).encode("utf-8")
    table_path.write_bytes(original_table_bytes)
    shadow_log_path = tmp_path / "SHADOW_EVALUATION_LOG.md"
    shadow_log_path.write_text("# Shadow Evaluation Log\n\n", encoding="utf-8")
    aggregated = {
        "summarization": {
            "n": 2, "mean_similarity": 0.9, "mean_source_cost_usd": 0.002,
            "mean_target_cost_usd": 0.0001, "pass_rate": None,
            "mean_judge_cost_usd": None, "errors": 0,
        }
    }
    statuses = {"summarization": "provisionally_validated"}

    import inspect

    import shadow_eval as shadow_eval_module

    # negative check: record_evidence no longer takes a table_path param
    assert "table_path" not in inspect.signature(record_evidence).parameters
    # negative check: no code path in the module writes to DELEGATION_TABLE
    source = inspect.getsource(shadow_eval_module)
    write_lines = [
        line for line in source.splitlines()
        if "DELEGATION_TABLE" in line and (".write_text(" in line or ".write_bytes(" in line)
    ]
    assert write_lines == []

    record_evidence(
        shadow_log_path, "2026-01-01", "lead", "builder",
        aggregated, statuses,
    )
    assert table_path.read_bytes() == original_table_bytes


def test_append_evidence_log_creates_section_once():
    # docs/SHADOW_EVALUATION_LOG.md's own heading is H1 --
    # append_evidence_log must create an H1 when no heading is found at
    # all, matching metrics.py's _SHADOW_EVAL_HEADER_RE which accepts any
    # depth "#{1,6}".
    text = "some unrelated content\n"
    once = append_evidence_log(text, ["entry one"])
    assert "# Shadow Evaluation Log" in once
    assert "- entry one" in once

    twice = append_evidence_log(once, ["entry two"])
    assert twice.count("Shadow Evaluation Log") == 1
    assert "- entry one" in twice
    assert "- entry two" in twice


def test_append_evidence_log_appends_to_tail_of_real_file_structure():
    # Reproduces the actual docs/SHADOW_EVALUATION_LOG.md shape: H1
    # heading, prose caveats, and several historical "- YYYY-MM-DD ..."
    # evidence lines already present. New entries must land at the
    # chronological TAIL of the file (end of document), not spliced in
    # right after the heading ahead of older prose/entries.
    text = (
        "# Shadow Evaluation Log\n\n"
        "Evidence for DELEGATION_TABLE.md Update Rule 1 -- one line per"
        " Shadow Evaluation run.\n\n"
        "Some caveat prose about an earlier accounting bug.\n\n"
        "- 2026-01-01  category=coding  source=lead target=builder"
        "  n=2  sim=0.10  cost_source=$0.0044 cost_target=$0.0000  -> rejected\n"
    )
    updated = append_evidence_log(text, ["2026-01-02  category=coding  n=2  -> validated"])
    lines = updated.splitlines()
    assert lines[0] == "# Shadow Evaluation Log"
    # the new entry is the LAST line, after the pre-existing 2026-01-01 one
    assert lines[-1] == "- 2026-01-02  category=coding  n=2  -> validated"
    assert "2026-01-01" in updated  # old entry preserved, not overwritten


def test_append_evidence_log_heading_matched_at_any_depth():
    # A "##"-depth heading (a shape this can still meet against a stale
    # copy) is recognized too -- no duplicate heading is created.
    text = "## Shadow Evaluation Log\n\nsome content\n"
    updated = append_evidence_log(text, ["entry one"])
    assert updated.count("Shadow Evaluation Log") == 1
    assert "- entry one" in updated


def test_evaluate_stored_category_overrides_heuristic(conn):
    """A row whose text would be categorized as 'formatting' by the heuristic
    but whose stored category column says 'coding' must end up in 'coding'
    -- the ground-truth beats the keyword scan."""
    # Insert a row whose prompt text would fire the formatting heuristic
    # but carry a stored category of 'coding'.
    conn.execute(
        "INSERT INTO requests (ts, model, status, cost_usd, prompt, response, category)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.datetime.now().isoformat(),
            "lead",
            "success",
            0.01,
            json.dumps([{"role": "user", "content": "format this markdown table please"}]),
            "some answer",
            "coding",
        ),
    )
    conn.commit()

    results = evaluate(
        conn, "lead", "intern", "http://localhost:4000", days=7, sample_n=10,
        mock_response="some answer",
    )
    assert len(results) == 1
    assert results[0]["category"] == "coding"


def test_sample_requests_returns_category_column(conn):
    """sample_requests() must return the category field from the DB row."""
    conn.execute(
        "INSERT INTO requests (ts, model, status, cost_usd, prompt, response, category)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.datetime.now().isoformat(),
            "lead",
            "success",
            0.01,
            json.dumps([{"role": "user", "content": "write a sort function"}]),
            "def sort(): pass",
            "coding",
        ),
    )
    conn.commit()
    rows = sample_requests(conn, "lead", days=7, limit=10)
    assert len(rows) == 1
    assert rows[0]["category"] == "coding"


def test_evaluate_pace_sleeps_between_pairs(conn, monkeypatch):
    import time as time_module

    import shadow_eval

    sleep_calls = []
    monkeypatch.setattr(time_module, "sleep", lambda secs: sleep_calls.append(secs))
    monkeypatch.setattr(shadow_eval.time, "sleep", lambda secs: sleep_calls.append(secs))

    seed(conn, "lead", [{"role": "user", "content": "summarize this article"}], "summary one")
    seed(conn, "lead", [{"role": "user", "content": "write a python function"}], "def f(): pass")
    seed(conn, "lead", [{"role": "user", "content": "classify this text"}], "positive")

    results = evaluate(
        conn, "lead", "intern", "http://localhost:4000", days=7, sample_n=10,
        pace=2.5, mock_response="ok",
    )
    # 3 pairs -> sleep called exactly (3 - 1) = 2 times, each with pace value
    assert len(results) == 3
    assert sleep_calls == [2.5, 2.5]
