"""Tests for the Ledger digest. Run: python -m pytest gateway/test_metrics.py"""

import datetime
import json
import sqlite3

import pytest

from metrics import (
    categorize,
    common_prefix_len,
    daily_digest,
    format_digest,
    format_phase2_line,
    parse_shadow_eval_log,
    phase2_readiness,
    repetition_by_model,
)
from sqlite_logger import SCHEMA
from guard import EVENTS_SCHEMA, QUOTA_EVENTS_SCHEMA

# Minimal mirror of tools/usage_report.py's cc_usage CREATE TABLE (Delegated
# Task 5): only the columns phase2_readiness's G1/C2 queries touch are
# populated by seed_cc_usage() below, but every NOT NULL column from the real
# schema is present so INSERTs behave like the real table.
CC_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cc_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    project TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_creation_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    accounted_cost_usd REAL,
    traffic_kind TEXT NOT NULL DEFAULT 'real',
    is_sidechain INTEGER NOT NULL DEFAULT 0,
    agent_id TEXT,
    agent_type TEXT,
    dedupe_key TEXT NOT NULL UNIQUE
);
"""


@pytest.fixture()
def conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.execute(SCHEMA)
    conn.execute(EVENTS_SCHEMA)
    return conn


def seed_cc_usage(conn, project, session_id, turn_index, model="sonnet",
                   traffic_kind="real", is_sidechain=0, ts=None):
    conn.execute(CC_USAGE_SCHEMA)
    conn.execute(
        "INSERT INTO cc_usage (ts, project, session_id, turn_index, model,"
        " input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,"
        " traffic_kind, is_sidechain, dedupe_key) VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?)",
        (
            ts or datetime.datetime.now().isoformat(), project, session_id,
            turn_index, model, traffic_kind, is_sidechain,
            f"{session_id}:{turn_index}",
        ),
    )
    conn.commit()


def seed_quota_event(conn, model, window_seconds, level, spent_tokens, limit_tokens, ts=None):
    conn.execute(QUOTA_EVENTS_SCHEMA)
    conn.execute(
        "INSERT INTO quota_events (ts, model, window_seconds, level, spent_tokens, limit_tokens)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            ts or datetime.datetime.now().isoformat(),
            model, window_seconds, level, spent_tokens, limit_tokens,
        ),
    )
    conn.commit()


def seed(conn, model, prompt, cost=0.01, tokens=(100, 20), status="success", ts=None):
    conn.execute(
        "INSERT INTO requests (ts, model, status, prompt_tokens, completion_tokens,"
        " cost_usd, latency_ms, prompt, response) VALUES (?, ?, ?, ?, ?, ?, 100, ?, 'ok')",
        (
            ts or datetime.datetime.now().isoformat(),
            model, status, tokens[0], tokens[1], cost, prompt,
        ),
    )
    conn.commit()


def test_common_prefix_len():
    assert common_prefix_len("abcd", "abXY") == 2
    assert common_prefix_len("", "abc") == 0
    assert common_prefix_len("same", "same") == 4


def test_repetition_by_model():
    rows = [
        ("lead", "AB"),
        ("lead", "ABCD"),   # 2 of 4 chars repeated
        ("other", "XY"),    # different model, separate chain
        ("lead", "ABCDEF"), # 4 of 6 chars repeated
    ]
    ratios = repetition_by_model(rows)
    assert ratios["lead"] == pytest.approx(6 / 10)
    assert "other" not in ratios  # single request, no consecutive pair


def test_categorize():
    assert categorize("Please summarize this article") == "summarization"
    assert categorize("def main(): ...") == "coding"
    assert categorize("Convert this to JSON") == "extraction"
    assert categorize("hello there") == "other"


def test_daily_digest_aggregates(conn):
    seed(conn, "lead", "AB", cost=0.01)
    seed(conn, "lead", "ABCD", cost=0.02)
    seed(conn, "lead", "fail", cost=0.0, status="failure")
    conn.execute(
        "INSERT INTO budget_events (ts, model, level, spent_usd, budget_usd)"
        " VALUES (?, 'lead', 'warn', 0.8, 1.0)",
        (datetime.datetime.now().isoformat(),),
    )
    conn.commit()

    digest = daily_digest(conn, days=1)

    (day_row,) = digest["per_day"]
    assert day_row["model"] == "lead"
    assert day_row["requests"] == 3
    assert day_row["failures"] == 1
    assert day_row["cost_usd"] == pytest.approx(0.03)
    assert day_row["prompt_tokens"] == 300

    assert digest["context_repetition_ratio"]["lead"] > 0
    assert digest["categories_heuristic"]["other"]["requests"] == 3
    assert digest["budget_events"][0]["level"] == "warn"


def test_old_rows_excluded(conn):
    old = (datetime.datetime.now() - datetime.timedelta(days=10)).isoformat()
    seed(conn, "lead", "old prompt", ts=old)
    digest = daily_digest(conn, days=1)
    assert digest["per_day"] == []


# --- quota_events digest (sibling of budget_events; SIBLING_MAP.md) -------


def test_daily_digest_survives_missing_quota_events_table(conn):
    # The base `conn` fixture (SCHEMA + EVENTS_SCHEMA only) never creates
    # quota_events -- the fail-safe case for a DB from before guard.py ever
    # ran, mirroring budget_events' OperationalError handling.
    seed(conn, "lead", "AB", cost=0.01)
    digest = daily_digest(conn, days=1)
    assert digest["quota_events"] == []
    text = format_digest(digest)
    assert "Token quota events (sliding windows):\n  none" in text


def test_daily_digest_survives_empty_quota_events_table(conn):
    # Table exists (guard.py ran at least once) but has no rows yet.
    conn.execute(QUOTA_EVENTS_SCHEMA)
    conn.commit()
    digest = daily_digest(conn, days=1)
    assert digest["quota_events"] == []
    assert "Token quota events (sliding windows):\n  none" in format_digest(digest)


def test_daily_digest_quota_events_warn_and_block(conn):
    seed_quota_event(conn, "groq-70b", 86400, "warn", 8000, 10000)
    seed_quota_event(conn, "groq-70b", 60, "block", 6000, 6000)

    digest = daily_digest(conn, days=1)

    levels = {e["level"] for e in digest["quota_events"]}
    assert levels == {"warn", "block"}
    warn = next(e for e in digest["quota_events"] if e["level"] == "warn")
    assert warn["model"] == "groq-70b"
    assert warn["window_seconds"] == 86400
    assert warn["spent_tokens"] == 8000
    assert warn["limit_tokens"] == 10000
    block = next(e for e in digest["quota_events"] if e["level"] == "block")
    assert block["window_seconds"] == 60
    assert block["spent_tokens"] == 6000
    assert block["limit_tokens"] == 6000

    text = format_digest(digest)
    assert "groq-70b window=86400s WARN: 8000 of 10000 tok" in text
    assert "groq-70b window=60s BLOCK: 6000 of 6000 tok" in text

    payload = json.dumps(digest)
    reloaded = json.loads(payload)
    reloaded_levels = {e["level"] for e in reloaded["quota_events"]}
    assert reloaded_levels == {"warn", "block"}


def test_daily_digest_quota_events_excludes_old_rows(conn):
    old = (datetime.datetime.now() - datetime.timedelta(days=10)).isoformat()
    seed_quota_event(conn, "groq-70b", 60, "warn", 100, 200, ts=old)
    digest = daily_digest(conn, days=1)
    assert digest["quota_events"] == []


# --- Phase 2 readiness (Delegated Task 3) ---------------------------------

# Mirrors the ACTUAL docs/SHADOW_EVALUATION_LOG.md layout: an H1
# "# Shadow Evaluation Log" at the top of its own file, not a subsection of
# a bigger document.
SHADOW_EVAL_LOG_FIXTURE = """# Shadow Evaluation Log

- 2026-01-01  category=coding  source=lead target=builder  n=2  sim=0.10  cost_source=$0.0044 cost_target=$0.0000  -> rejected
- 2026-01-01  category=coding  source=lead target=builder  n=4  sim=0.50  judge=critic pass_rate=1.00  cost_source=$0.0023 cost_target=$0.0000  -> provisionally_validated [RETRACTED]
- 2026-01-01  category=coding  source=lead target=builder  n=2  sim=0.08  judge=critic pass_rate=0.50  cost_source=$0.0044 cost_target=$0.0000  -> rejected [OVERRULED, see below]
- 2026-01-01  category=coding  source=lead target=critic  n=2  sim=0.25  judge=judge pass_rate=1.00  cost_source=$0.0044 cost_target=$0.0000  -> provisionally_validated
- 2026-01-01  category=summarization  source=lead target=builder  n=2  sim=0.46  judge=critic pass_rate=1.00  cost_source=$0.0016 cost_target=$0.0000  -> provisionally_validated
"""


def test_parse_shadow_eval_log_counts_judged_non_retracted_pairs():
    counts = parse_shadow_eval_log(SHADOW_EVAL_LOG_FIXTURE)
    # coding: the difflib-only line (no judge=) is excluded, the [RETRACTED]
    # line is excluded, the [OVERRULED] line IS counted (it was judged), plus
    # the target=critic replay line -> 2 runs, 2+2=4 pairs.
    assert counts["coding"] == {"pairs": 4, "runs": 2}
    assert counts["summarization"] == {"pairs": 2, "runs": 1}
    assert "classification" not in counts


def test_parse_shadow_eval_log_empty_when_no_judged_lines():
    text = "## Shadow Evaluation Log\n\n- 2026-01-01  category=coding  n=2  -> rejected\n"
    assert parse_shadow_eval_log(text) == {}


def test_parse_shadow_eval_log_h1_heading_matches():
    # The ACTUAL docs/SHADOW_EVALUATION_LOG.md heading is an H1 ("#").
    text = "# Shadow Evaluation Log\n\n- 2026-01-01  category=coding  n=2  -> rejected\n"
    assert parse_shadow_eval_log(text) == {}  # no judge=, but header IS found


def test_parse_shadow_eval_log_empty_when_header_missing_entirely():
    # No whole-text fallback: a missing heading means no section at all,
    # even if judged-looking lines exist in the body.
    text = (
        "- 2026-01-01  category=coding  source=lead target=builder"
        "  n=2  sim=0.10  judge=critic pass_rate=1.00"
        "  cost_source=$0.0044 cost_target=$0.0000  -> provisionally_validated\n"
    )
    assert parse_shadow_eval_log(text) == {}


def test_phase2_readiness_has_all_ten_criteria(conn, tmp_path):
    shadow_log = tmp_path / "SHADOW_EVALUATION_LOG.md"
    shadow_log.write_text(SHADOW_EVAL_LOG_FIXTURE, encoding="utf-8")
    readiness = phase2_readiness(conn, days=14, shadow_log_path=shadow_log)
    assert set(readiness.keys()) == {
        "G1", "G2", "R1", "R2", "R3", "R4", "R5", "C1", "C2", "C3",
    }


def test_g2_and_r5_are_manual_check(conn):
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["G2"]["status"] == "manual_check"
    assert "pointer" in readiness["G2"]
    assert readiness["R5"]["status"] == "manual_check"
    assert "pointer" in readiness["R5"]


def test_r3_r4_not_computable_yet_with_needs(conn):
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    for crit in ("R3", "R4"):
        assert readiness[crit]["status"] == "not_computable_yet"
        assert "needs" in readiness[crit]


def test_r1_not_computable_when_shadow_log_missing(conn):
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["R1"]["status"] == "not_computable_yet"
    assert "not found" in readiness["R1"]["needs"]


def test_r1_not_met_below_threshold(conn, tmp_path):
    shadow_log = tmp_path / "SHADOW_EVALUATION_LOG.md"
    shadow_log.write_text(SHADOW_EVAL_LOG_FIXTURE, encoding="utf-8")
    readiness = phase2_readiness(conn, days=14, shadow_log_path=shadow_log)
    assert readiness["R1"]["status"] == "not_met"
    assert "coding" in readiness["R1"]["detail"]


def test_r1_met_when_threshold_reached(conn, tmp_path):
    lines = ["# Shadow Evaluation Log", ""]
    # 16 judged, non-retracted runs of n=2 -> 32 pairs across 16 runs.
    for _ in range(16):
        lines.append(
            "- 2026-01-01  category=coding  source=lead target=builder"
            "  n=2  sim=0.90  judge=judge pass_rate=1.00"
            "  cost_source=$0.0044 cost_target=$0.0000  -> provisionally_validated"
        )
    shadow_log = tmp_path / "SHADOW_EVALUATION_LOG.md"
    shadow_log.write_text("\n".join(lines), encoding="utf-8")
    readiness = phase2_readiness(conn, days=14, shadow_log_path=shadow_log)
    assert readiness["R1"]["status"] == "met"


def test_g1_not_computable_gracefully_when_cc_usage_absent(conn):
    # The base `conn` fixture has no cc_usage table -- G1 must fall back to
    # requests-only and say so explicitly.
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["G1"]["status"] == "not_met"
    assert "cc_usage table absent" in readiness["G1"]["detail"]


def test_g1_met_counts_requests_and_cc_usage_union(conn):
    now = datetime.datetime.now()
    # 10 real days via requests (days 0-9)
    for i in range(10):
        seed(conn, "lead", f"prompt {i}", ts=(now - datetime.timedelta(days=i)).isoformat())
    conn.execute("UPDATE requests SET traffic_kind = 'real'")
    conn.commit()
    # 4 more distinct real days via cc_usage (days 10-13, no overlap with
    # requests, comfortably inside the 14-day window so the test doesn't
    # depend on the exact 'now'-vs-SQLite-date('now') boundary).
    for i in range(10, 14):
        seed_cc_usage(conn, "proj", f"sess-{i}", 0, ts=(now - datetime.timedelta(days=i)).isoformat())
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["G1"]["status"] == "met"
    assert "requests real=10" in readiness["G1"]["detail"]
    assert "cc_usage real=4" in readiness["G1"]["detail"]


def test_g1_not_met_when_distinct_days_enough_but_run_broken_by_gap(conn):
    # 14 distinct real-traffic days total, but split into two runs of 7 by a
    # 2-day gap: distinct-day count alone would say "met" here, but no run
    # reaches 14.
    now = datetime.datetime.now()
    for i in list(range(0, 7)) + list(range(9, 16)):
        seed(conn, "lead", f"prompt {i}", ts=(now - datetime.timedelta(days=i)).isoformat())
    conn.execute("UPDATE requests SET traffic_kind = 'real'")
    conn.commit()
    readiness = phase2_readiness(conn, days=30, shadow_log_path="/does/not/exist.md")
    assert readiness["G1"]["max_consecutive_days"] == 7
    assert readiness["G1"]["status"] == "not_met"
    assert "14 distinct real-traffic day(s)" in readiness["G1"]["detail"]
    assert "longest consecutive run = 7 day(s)" in readiness["G1"]["detail"]


def test_g1_met_when_run_of_14_consecutive_days(conn):
    now = datetime.datetime.now()
    for i in range(14):
        seed(conn, "lead", f"prompt {i}", ts=(now - datetime.timedelta(days=i)).isoformat())
    conn.execute("UPDATE requests SET traffic_kind = 'real'")
    conn.commit()
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["G1"]["max_consecutive_days"] == 14
    assert readiness["G1"]["status"] == "met"


def test_g1_max_consecutive_days_ignores_shorter_run_across_a_gap(conn):
    # A short run (5 days), a gap, then a longer run (20 days) further back
    # in the window; the reported max must be the longer run, not the sum
    # and not the first-seen run.
    now = datetime.datetime.now()
    for i in list(range(0, 5)) + list(range(6, 26)):
        seed(conn, "lead", f"prompt {i}", ts=(now - datetime.timedelta(days=i)).isoformat())
    conn.execute("UPDATE requests SET traffic_kind = 'real'")
    conn.commit()
    readiness = phase2_readiness(conn, days=30, shadow_log_path="/does/not/exist.md")
    assert readiness["G1"]["max_consecutive_days"] == 20
    assert readiness["G1"]["status"] == "met"


def test_g1_single_day_of_traffic(conn):
    now = datetime.datetime.now()
    seed(conn, "lead", "prompt", ts=now.isoformat())
    conn.execute("UPDATE requests SET traffic_kind = 'real'")
    conn.commit()
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["G1"]["max_consecutive_days"] == 1
    assert readiness["G1"]["status"] == "not_met"
    assert "1 distinct real-traffic day(s)" in readiness["G1"]["detail"]


def test_c2_not_computable_when_cc_usage_absent(conn):
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["C2"]["status"] == "not_computable_yet"
    assert "needs" in readiness["C2"]


def test_c2_met_when_enough_real_sessions(conn):
    for s in range(20):
        for turn in range(5):
            seed_cc_usage(conn, "proj", f"sess-{s}", turn)
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["C2"]["status"] == "met"
    assert "20 real session" in readiness["C2"]["detail"]


def test_c2_not_met_when_too_few_sessions(conn):
    for s in range(5):
        for turn in range(5):
            seed_cc_usage(conn, "proj", f"sess-{s}", turn)
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["C2"]["status"] == "not_met"


def test_c2_excludes_sidechain_turns(conn):
    # A session with only sidechain (subagent) turns should not count.
    for turn in range(5):
        seed_cc_usage(conn, "proj", "sess-sidechain", turn, is_sidechain=1)
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["C2"]["status"] == "not_met"


def test_c1_not_computable_when_no_real_traffic(conn):
    # NOTE: this fixture's SCHEMA (sqlite_logger.SCHEMA) defaults
    # traffic_kind to 'real'; a live gateway/requests.db column default can
    # instead be 'synthetic' depending on migration history. Tag explicitly
    # as 'synthetic' so this test reflects the "no real traffic" case
    # regardless of which default is active.
    seed(conn, "lead", "AB", cost=0.01)
    seed(conn, "lead", "ABCD", cost=0.01)
    conn.execute("UPDATE requests SET traffic_kind = 'synthetic'")
    conn.commit()
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["C1"]["status"] == "not_computable_yet"
    assert "needs" in readiness["C1"]


def test_c1_met_on_real_traffic_above_threshold(conn):
    seed(conn, "lead", "AAAAAAAAAA", cost=0.01)   # 10 chars
    seed(conn, "lead", "AAAAAAAAAAAAAAAAAAAA", cost=0.01)  # 20 chars, 10 repeated
    conn.execute("UPDATE requests SET traffic_kind = 'real'")
    conn.commit()
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["C1"]["status"] == "met"  # 10/20 = 50% >= 40%


def test_c1_not_met_below_threshold(conn):
    seed(conn, "lead", "AAAAAAAAAAAAAAAAAAAA", cost=0.01)  # 20 chars
    seed(conn, "lead", "AAAXXXXXXXXXXXXXXXXX", cost=0.01)  # 3/20 = 15% repeated
    conn.execute("UPDATE requests SET traffic_kind = 'real'")
    conn.commit()
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["C1"]["status"] == "not_met"


def seed_categorized(conn, category, cost, prompt="irrelevant prompt text", ts=None):
    """Insert a real-traffic row with an explicit stored `category` and
    cost_usd, for R2 readiness tests. category=None leaves the column NULL
    so categorize() must fall back on the prompt text."""
    conn.execute(
        "INSERT INTO requests (ts, model, status, prompt_tokens, completion_tokens,"
        " cost_usd, latency_ms, prompt, response, category, traffic_kind)"
        " VALUES (?, 'lead', 'success', 100, 20, ?, 50, ?, 'ok', ?, 'real')",
        (ts or datetime.datetime.now().isoformat(), cost, prompt, category),
    )
    conn.commit()


def test_r2_not_met_with_empty_validated_delegable_categories(conn):
    # This deployment's VALIDATED_DELEGABLE_CATEGORIES starts empty
    # (populate it as calibration actually validates a category, Update
    # Rule 1) -- so no amount of real-traffic spend, however concentrated
    # in one category, can ever read "met" until that set is populated.
    seed_categorized(conn, "coding", 0.30)
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["R2"]["status"] == "not_met"
    assert "0.0%" in readiness["R2"]["detail"]
    assert "(none yet)" in readiness["R2"]["detail"]
    assert "coding" in readiness["R2"]["detail"]  # per-category breakdown still visible


def test_r2_falls_back_to_categorize_when_stored_category_null(conn):
    seed_categorized(conn, None, 0.30, prompt="please summarize this document")
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    # Still not_met (empty validated set), but the categorize() fallback
    # must have actually run: the heuristic category shows up in the
    # per-category spend breakdown.
    assert readiness["R2"]["status"] == "not_met"
    assert "summarization" in readiness["R2"]["detail"]


def test_r2_honest_low_data_when_rows_exist_but_all_zero_cost(conn):
    # Rows exist, but there is nothing to compute a spend SHARE from.
    seed_categorized(conn, "coding", 0.0)
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["R2"]["status"] == "not_computable_yet"
    assert "1 real row(s)" in readiness["R2"]["needs"]
    assert "currently 0" not in readiness["R2"]["needs"]


def test_r2_not_computable_when_no_real_rows(conn):
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["R2"]["status"] == "not_computable_yet"
    assert "needs" in readiness["R2"]


def seed_cache_real(conn, prompt_tokens, cache_read, cache_creation, ts=None):
    """Insert a real-traffic row with explicit cache-token columns, for C3
    readiness tests."""
    conn.execute(
        "INSERT INTO requests (ts, model, status, prompt_tokens, completion_tokens,"
        " cost_usd, latency_ms, prompt, response,"
        " cache_read_input_tokens, cache_creation_input_tokens, traffic_kind)"
        " VALUES (?, 'opus', 'success', ?, 10, 0.01, 50, 'x', 'ok', ?, ?, 'real')",
        (ts or datetime.datetime.now().isoformat(), prompt_tokens, cache_read, cache_creation),
    )
    conn.commit()


def test_c3_not_met_cache_aware_low_uncached_share(conn):
    # Live-shape row: prompt_tokens is INCLUSIVE of both cache columns, so
    # uncached = prompt - read - creation is tiny relative to the input side.
    seed_cache_real(conn, prompt_tokens=63423, cache_read=61541, cache_creation=1880)
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    uncached = 63423 - 61541 - 1880  # = 2
    ratio = uncached / 63423
    assert ratio < 0.25
    assert readiness["C3"]["status"] == "not_met"
    assert f"{uncached} of 63423" in readiness["C3"]["detail"]


def test_c3_met_when_uncached_share_above_threshold(conn):
    seed_cache_real(conn, prompt_tokens=100, cache_read=10, cache_creation=10)  # 80% uncached
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["C3"]["status"] == "met"


def test_c3_honest_low_data_when_rows_exist_but_zero_prompt_tokens(conn):
    seed_cache_real(conn, prompt_tokens=0, cache_read=0, cache_creation=0)
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["C3"]["status"] == "not_computable_yet"
    assert "1 real row(s)" in readiness["C3"]["needs"]
    assert "currently 0" not in readiness["C3"]["needs"]


def test_c3_regression_pin_matches_gate_report_shape(conn):
    """Regression pin: shape approximating a real gate report -- truly-
    uncached ~0.11% of the input side, at cache_read ~96.1% / cache_creation
    ~3.8%."""
    seed_cache_real(
        conn, prompt_tokens=27_589_350, cache_read=26_510_000, cache_creation=1_050_000,
    )
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    uncached = 27_589_350 - 26_510_000 - 1_050_000  # 29,350
    ratio = uncached / 27_589_350
    assert ratio == pytest.approx(0.0011, abs=0.0001)
    assert readiness["C3"]["status"] == "not_met"


def test_c3_not_computable_when_no_real_rows(conn):
    readiness = phase2_readiness(conn, days=14, shadow_log_path="/does/not/exist.md")
    assert readiness["C3"]["status"] == "not_computable_yet"
    assert "needs" in readiness["C3"]


def test_format_phase2_line_vocabulary():
    assert format_phase2_line("G1", {"status": "met", "detail": "x"}) == "  G1: x -> met"
    assert format_phase2_line("R1", {"status": "not_met", "detail": "x"}) == "  R1: x -> not met"
    assert format_phase2_line(
        "R2", {"status": "not_computable_yet", "needs": "y"}
    ) == "  R2: not computable yet (needs y)"
    assert format_phase2_line(
        "G2", {"status": "manual_check", "pointer": "z"}
    ) == "  G2: manual check (z)"


def test_daily_digest_carries_phase2_readiness(conn, tmp_path):
    shadow_log = tmp_path / "SHADOW_EVALUATION_LOG.md"
    shadow_log.write_text(SHADOW_EVAL_LOG_FIXTURE, encoding="utf-8")
    seed(conn, "lead", "AB", cost=0.01)
    digest = daily_digest(conn, days=14, shadow_log_path=shadow_log)
    assert "phase2_readiness" in digest
    assert digest["phase2_readiness"]["G2"]["status"] == "manual_check"


# --- cache token columns in daily_digest -----------------------------------


def seed_with_cache(conn, model, prompt, prompt_tokens=100,
                    cache_read=0, cache_creation=0, ts=None):
    """Insert a request row with explicit cache token counts."""
    conn.execute(
        "INSERT INTO requests (ts, model, status, prompt_tokens, completion_tokens,"
        " cost_usd, latency_ms, prompt, response,"
        " cache_read_input_tokens, cache_creation_input_tokens)"
        " VALUES (?, ?, 'success', ?, 10, 0.01, 50, ?, 'ok', ?, ?)",
        (
            ts or datetime.datetime.now().isoformat(),
            model, prompt_tokens, prompt, cache_read, cache_creation,
        ),
    )
    conn.commit()


def test_daily_digest_cache_aggregation(conn):
    # Two requests with cache activity, one without (NULL -> treated as 0).
    seed_with_cache(conn, "sonnet", "prompt A", prompt_tokens=200,
                    cache_read=80, cache_creation=40)
    seed_with_cache(conn, "sonnet", "prompt B", prompt_tokens=100,
                    cache_read=60, cache_creation=0)
    seed(conn, "sonnet", "prompt C", tokens=(50, 5))  # no cache columns -> NULL

    digest = daily_digest(conn, days=1)
    (row,) = digest["per_day"]

    assert row["cache_read_tokens"] == 140        # 80 + 60 + 0
    assert row["cache_creation_tokens"] == 40     # 40 + 0 + 0
    # cache_read_share = 140 / 350 = 0.4. Denominator is prompt_tokens
    # ALONE: litellm's prompt_tokens in requests.db is the FULL input side,
    # already including the cache counters (verified on live rows) --
    # summing them on top double-counts.
    total_prompt = row["prompt_tokens"]           # 200 + 100 + 50 = 350
    expected_share = round(140 / total_prompt, 4)
    assert row["cache_read_share"] == pytest.approx(expected_share)


def test_daily_digest_cache_share_zero_when_no_prompt_tokens(conn):
    # Edge: all prompt_tokens are 0 AND no cache reads -> denominator is 0,
    # share must be 0.0, not a ZeroDivisionError.
    conn.execute(
        "INSERT INTO requests (ts, model, status, prompt_tokens, completion_tokens,"
        " cost_usd, latency_ms, prompt, response,"
        " cache_read_input_tokens, cache_creation_input_tokens)"
        " VALUES (datetime('now'), 'sonnet', 'success', 0, 0, 0.0, 10, 'x', 'y', 0, 0)"
    )
    conn.commit()
    digest = daily_digest(conn, days=1)
    (row,) = digest["per_day"]
    assert row["cache_read_share"] == 0.0


def test_daily_digest_cache_text_format(conn):
    seed_with_cache(conn, "sonnet", "prompt X", prompt_tokens=400,
                    cache_read=100, cache_creation=50)
    digest = daily_digest(conn, days=1)
    text = format_digest(digest)
    # Cache sub-line must appear after the main per-day line.
    assert "cache: read=100 creation=50" in text
    # cache_read_share = 100/400 = 25.0% (:.1% format); prompt_tokens is
    # the full input side, so it is the denominator by itself.
    assert "cache_read_share=25.0%" in text


def test_daily_digest_cache_share_live_traffic_shape(conn):
    # Regression pin: a row shaped like live API-window traffic
    # (prompt_tokens is the full input side, cache_read is almost all of
    # it). A double-counting denominator would give ~0.49 here; the true
    # share is ~0.97.
    seed_with_cache(conn, "opus", "live-shape", prompt_tokens=63423,
                    cache_read=61541, cache_creation=1880)
    digest = daily_digest(conn, days=1)
    (row,) = digest["per_day"]
    assert row["cache_read_share"] == pytest.approx(round(61541 / 63423, 4))
    assert row["cache_read_share"] > 0.9  # the double-count bug can't pass this


def test_categories_heuristic_stored_category_preferred(conn):
    """categories_heuristic must use the stored category column when non-NULL,
    falling back to categorize() only for rows where category IS NULL."""
    # Row 1: prompt would fire 'formatting' heuristic, but stored category = 'coding'
    conn.execute(
        "INSERT INTO requests (ts, model, status, prompt_tokens, completion_tokens,"
        " cost_usd, latency_ms, prompt, response, category)"
        " VALUES (datetime('now'), 'lead', 'success', 10, 5, 0.01, 100,"
        " 'format this markdown table', 'ok', 'coding')"
    )
    # Row 2: no stored category (NULL) -> heuristic applies, prompt = 'summarize this'
    conn.execute(
        "INSERT INTO requests (ts, model, status, prompt_tokens, completion_tokens,"
        " cost_usd, latency_ms, prompt, response, category)"
        " VALUES (datetime('now'), 'lead', 'success', 10, 5, 0.01, 100,"
        " 'summarize this article', 'ok', NULL)"
    )
    conn.commit()

    digest = daily_digest(conn, days=1)
    cats = digest["categories_heuristic"]
    # stored 'coding' beats the 'formatting' needle in row 1
    assert "coding" in cats
    assert cats["coding"]["requests"] == 1
    # 'formatting' must NOT appear (no row ended up there)
    assert "formatting" not in cats
    # row 2 has no stored category, heuristic fires 'summarization'
    assert "summarization" in cats
    assert cats["summarization"]["requests"] == 1


def test_daily_digest_cache_null_rows_treated_as_zero(conn):
    # seed() does not set cache columns -> they are NULL in the DB.
    seed(conn, "lead", "prompt", tokens=(200, 30))
    digest = daily_digest(conn, days=1)
    (row,) = digest["per_day"]
    assert row["cache_read_tokens"] == 0
    assert row["cache_creation_tokens"] == 0
    assert row["cache_read_share"] == 0.0
