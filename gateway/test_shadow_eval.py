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
    judge_pair,
    parse_verdict,
    replay,
    sample_requests,
    similarity,
    update_delegation_table,
    update_table_status,
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


class _FakeResponse:
    def __init__(self, hidden_params):
        self._hidden_params = hidden_params


def test_extract_cost_uses_hidden_params_response_cost():
    response = _FakeResponse({"response_cost": 0.0042})
    cost = _extract_cost(response, "middle-groq", db_path=None, call_start=datetime.datetime.now())
    assert cost == 0.0042


def test_extract_cost_falls_back_to_db_when_hidden_params_missing(tmp_path):
    import sqlite3

    from sqlite_logger import SCHEMA

    db_path = tmp_path / "requests.db"
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    call_start = datetime.datetime(2026, 7, 4, 12, 0, 0)
    conn.execute(
        "INSERT INTO requests (ts, model, status, cost_usd) VALUES (?, ?, ?, ?)",
        ((call_start + datetime.timedelta(seconds=1)).isoformat(), "middle-groq", "success", 0.0191),
    )
    conn.commit()

    response = _FakeResponse({"response_cost": None})
    cost = _extract_cost(response, "middle-groq", db_path=str(db_path), call_start=call_start)
    assert cost == 0.0191


def test_extract_cost_returns_none_when_unavailable():
    response = _FakeResponse({"response_cost": None})
    assert _extract_cost(response, "middle-groq", db_path=None, call_start=datetime.datetime.now()) is None


def test_extract_cost_returns_none_when_no_matching_db_row(tmp_path):
    import sqlite3

    from sqlite_logger import SCHEMA

    db_path = tmp_path / "requests.db"
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    conn.commit()

    response = _FakeResponse({})
    cost = _extract_cost(response, "middle-groq", db_path=str(db_path), call_start=datetime.datetime.now())
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


def test_decide_status_validated():
    agg = {"n": 3, "mean_similarity": 0.9, "mean_source_cost_usd": 0.02, "mean_target_cost_usd": 0.001, "errors": 0}
    assert decide_status(agg, similarity_threshold=0.5, min_samples=2) == "validated"


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
    # low difflib sim but judge says equivalent -> validated
    agg = {"n": 2, "mean_similarity": 0.1, "mean_source_cost_usd": 0.02,
           "mean_target_cost_usd": 0.001, "errors": 0, "pass_rate": 1.0}
    assert decide_status(agg, similarity_threshold=0.5, min_samples=2) == "validated"
    # high sim but judge says worse -> rejected
    agg["pass_rate"] = 0.0
    agg["mean_similarity"] = 0.9
    assert decide_status(agg, similarity_threshold=0.5, min_samples=2) == "rejected"


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


def test_update_delegation_table_evidence_line_includes_judge_cost(tmp_path):
    table_path = tmp_path / "DELEGATION_TABLE.md"
    table_path.write_text(
        "| Task type | Cost (Lead) | Value of Lead | Delegate to | Status |\n"
        "|---|---|---|---|---|\n"
        "| Summarization | Medium | Medium | Junior | estimated |\n",
        encoding="utf-8",
    )
    aggregated = {
        "summarization": {
            "n": 2, "mean_similarity": 0.9, "mean_source_cost_usd": 0.002,
            "mean_target_cost_usd": 0.0001, "pass_rate": 1.0,
            "mean_judge_cost_usd": 0.0004, "errors": 0,
        }
    }
    statuses = {"summarization": "validated"}
    update_delegation_table(
        table_path, "2026-07-04", "lead-gemini", "middle-groq",
        aggregated, statuses, judge_model="judge-groq",
    )
    text = table_path.read_text(encoding="utf-8")
    assert "judge=judge-groq pass_rate=1.00 judge_cost=$0.0004" in text


def test_update_delegation_table_evidence_line_judge_cost_unknown(tmp_path):
    # Rule #1 decoupling (2026-07-07 Lead review finding #2): when the
    # judge ran (pass_rate present) but cost extraction failed, the
    # evidence line must still carry judge= and pass_rate=, with an
    # explicit judge_cost=unknown instead of dropping the segment.
    table_path = tmp_path / "DELEGATION_TABLE.md"
    table_path.write_text(
        "| Task type | Cost (Lead) | Value of Lead | Delegate to | Status |\n"
        "|---|---|---|---|---|\n"
        "| Summarization | Medium | Medium | Junior | estimated |\n",
        encoding="utf-8",
    )
    aggregated = {
        "summarization": {
            "n": 2, "mean_similarity": 0.9, "mean_source_cost_usd": 0.002,
            "mean_target_cost_usd": 0.0001, "pass_rate": 1.0,
            "mean_judge_cost_usd": None, "errors": 0,
        }
    }
    statuses = {"summarization": "validated"}
    update_delegation_table(
        table_path, "2026-07-04", "lead-gemini", "middle-groq",
        aggregated, statuses, judge_model="judge-groq",
    )
    text = table_path.read_text(encoding="utf-8")
    assert "judge=judge-groq pass_rate=1.00 judge_cost=unknown" in text


def test_update_table_status_replaces_only_matching_row():
    text = (
        "| Task type | Cost (Lead) | Value of Lead | Delegate to | Status |\n"
        "|---|---|---|---|---|\n"
        "| Summarization | Medium | Medium | Junior | estimated |\n"
        "| Classification, tagging | Low | Low | Junior | estimated |\n"
    )
    updated = update_table_status(text, "Summarization", "validated")
    assert "| Summarization | Medium | Medium | Junior | validated |" in updated
    assert "| Classification, tagging | Low | Low | Junior | estimated |" in updated


def test_append_evidence_log_creates_section_once():
    text = "# Delegation Table\n\nsome content\n"
    once = append_evidence_log(text, ["entry one"])
    assert "## Shadow Evaluation Log" in once
    assert "- entry one" in once

    twice = append_evidence_log(once, ["entry two"])
    assert twice.count("## Shadow Evaluation Log") == 1
    assert "- entry one" in twice
    assert "- entry two" in twice
