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


# --- quota_events digest (t-019, sibling of budget_events, D-0043 class,
# SIBLING_MAP.md axis 2: "enforcement-events Guard <-> Ledger-digest") -----


def test_daily_digest_survives_missing_quota_events_table(conn):
    # The base `conn` fixture (SCHEMA + EVENTS_SCHEMA only) never creates
    # quota_events -- the fail-safe case for a DB from before t-018's guard.py
    # ever ran, mirroring budget_events' OperationalError handling.
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

SHADOW_EVAL_LOG_FIXTURE = """# Delegation Table

## Shadow Evaluation Log

- 2026-07-03  category=coding  source=lead-gemini target=intern  n=2  sim=0.10  cost_source=$0.0044 cost_target=$0.0000  -> rejected
- 2026-07-03  category=coding  source=lead-gemini target=intern  n=4  sim=0.51  judge=middle-groq pass_rate=1.00  cost_source=$0.0023 cost_target=$0.0000  -> validated [RETRACTED]
- 2026-07-03  category=coding  source=lead-gemini target=intern  n=2  sim=0.08  judge=middle-groq pass_rate=0.50  cost_source=$0.0044 cost_target=$0.0000  -> rejected [OVERRULED, see below]
- 2026-07-03  category=coding  source=lead-gemini target=middle-groq  n=2  sim=0.25  judge=judge-groq pass_rate=1.00  cost_source=$0.0044 cost_target=$0.0000  -> validated
- 2026-07-03  category=summarization  source=lead-gemini target=intern  n=2  sim=0.46  judge=middle-groq pass_rate=1.00  cost_source=$0.0016 cost_target=$0.0000  -> validated
"""


def test_parse_shadow_eval_log_counts_judged_non_retracted_pairs():
    counts = parse_shadow_eval_log(SHADOW_EVAL_LOG_FIXTURE)
    # coding: the difflib-only line (no judge=) is excluded, the [RETRACTED]
    # line is excluded, the [OVERRULED] line IS counted (it was judged), plus
    # the middle-groq replay line -> 2 runs, 2+2=4 pairs.
    assert counts["coding"] == {"pairs": 4, "runs": 2}
    assert counts["summarization"] == {"pairs": 2, "runs": 1}
    assert "classification" not in counts


def test_parse_shadow_eval_log_empty_when_no_judged_lines():
    text = "## Shadow Evaluation Log\n\n- 2026-07-03  category=coding  n=2  -> rejected\n"
    assert parse_shadow_eval_log(text) == {}


def test_phase2_readiness_has_all_ten_criteria(conn, tmp_path):
    dtable = tmp_path / "DELEGATION_TABLE.md"
    dtable.write_text(SHADOW_EVAL_LOG_FIXTURE, encoding="utf-8")
    readiness = phase2_readiness(conn, days=14, delegation_table_path=dtable)
    assert set(readiness.keys()) == {
        "G1", "G2", "R1", "R2", "R3", "R4", "R5", "C1", "C2", "C3",
    }


def test_g2_and_r5_are_manual_check(conn):
    readiness = phase2_readiness(conn, days=14, delegation_table_path="/does/not/exist.md")
    assert readiness["G2"]["status"] == "manual_check"
    assert "pointer" in readiness["G2"]
    assert readiness["R5"]["status"] == "manual_check"
    assert "pointer" in readiness["R5"]


def test_r2_r3_r4_c3_not_computable_yet_with_needs(conn):
    readiness = phase2_readiness(conn, days=14, delegation_table_path="/does/not/exist.md")
    for crit in ("R2", "R3", "R4", "C3"):
        assert readiness[crit]["status"] == "not_computable_yet"
        assert "needs" in readiness[crit]


def test_r1_not_computable_when_delegation_table_missing(conn):
    readiness = phase2_readiness(conn, days=14, delegation_table_path="/does/not/exist.md")
    assert readiness["R1"]["status"] == "not_computable_yet"
    assert "not found" in readiness["R1"]["needs"]


def test_r1_not_met_below_threshold(conn, tmp_path):
    dtable = tmp_path / "DELEGATION_TABLE.md"
    dtable.write_text(SHADOW_EVAL_LOG_FIXTURE, encoding="utf-8")
    readiness = phase2_readiness(conn, days=14, delegation_table_path=dtable)
    assert readiness["R1"]["status"] == "not_met"
    assert "coding" in readiness["R1"]["detail"]


def test_r1_met_when_threshold_reached(conn, tmp_path):
    lines = ["## Shadow Evaluation Log", ""]
    # 16 judged, non-retracted runs of n=2 -> 32 pairs across 16 runs.
    for _ in range(16):
        lines.append(
            "- 2026-07-03  category=coding  source=lead-gemini target=intern"
            "  n=2  sim=0.90  judge=judge-groq pass_rate=1.00"
            "  cost_source=$0.0044 cost_target=$0.0000  -> validated"
        )
    dtable = tmp_path / "DELEGATION_TABLE.md"
    dtable.write_text("\n".join(lines), encoding="utf-8")
    readiness = phase2_readiness(conn, days=14, delegation_table_path=dtable)
    assert readiness["R1"]["status"] == "met"


def test_g1_not_computable_gracefully_when_cc_usage_absent(conn):
    # The base `conn` fixture has no cc_usage table -- G1 must fall back to
    # requests-only and say so explicitly (post-spec note in CURRENT_CONTEXT.md).
    readiness = phase2_readiness(conn, days=14, delegation_table_path="/does/not/exist.md")
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
    readiness = phase2_readiness(conn, days=14, delegation_table_path="/does/not/exist.md")
    assert readiness["G1"]["status"] == "met"
    assert "requests real=10" in readiness["G1"]["detail"]
    assert "cc_usage real=4" in readiness["G1"]["detail"]


def test_g1_not_met_when_distinct_days_enough_but_run_broken_by_gap(conn):
    # 14 distinct real-traffic days total, but split into two runs of 7 by a
    # 2-day gap -- the class-of-bug this task fixes (Task 3 critic finding
    # 1): distinct-day count alone said "met" here, but no run reaches 14.
    now = datetime.datetime.now()
    for i in list(range(0, 7)) + list(range(9, 16)):
        seed(conn, "lead", f"prompt {i}", ts=(now - datetime.timedelta(days=i)).isoformat())
    conn.execute("UPDATE requests SET traffic_kind = 'real'")
    conn.commit()
    readiness = phase2_readiness(conn, days=30, delegation_table_path="/does/not/exist.md")
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
    readiness = phase2_readiness(conn, days=14, delegation_table_path="/does/not/exist.md")
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
    readiness = phase2_readiness(conn, days=30, delegation_table_path="/does/not/exist.md")
    assert readiness["G1"]["max_consecutive_days"] == 20
    assert readiness["G1"]["status"] == "met"


def test_g1_single_day_of_traffic(conn):
    now = datetime.datetime.now()
    seed(conn, "lead", "prompt", ts=now.isoformat())
    conn.execute("UPDATE requests SET traffic_kind = 'real'")
    conn.commit()
    readiness = phase2_readiness(conn, days=14, delegation_table_path="/does/not/exist.md")
    assert readiness["G1"]["max_consecutive_days"] == 1
    assert readiness["G1"]["status"] == "not_met"
    assert "1 distinct real-traffic day(s)" in readiness["G1"]["detail"]


def test_c2_not_computable_when_cc_usage_absent(conn):
    readiness = phase2_readiness(conn, days=14, delegation_table_path="/does/not/exist.md")
    assert readiness["C2"]["status"] == "not_computable_yet"
    assert "needs" in readiness["C2"]


def test_c2_met_when_enough_real_sessions(conn):
    for s in range(20):
        for turn in range(5):
            seed_cc_usage(conn, "proj", f"sess-{s}", turn)
    readiness = phase2_readiness(conn, days=14, delegation_table_path="/does/not/exist.md")
    assert readiness["C2"]["status"] == "met"
    assert "20 real session" in readiness["C2"]["detail"]


def test_c2_not_met_when_too_few_sessions(conn):
    for s in range(5):
        for turn in range(5):
            seed_cc_usage(conn, "proj", f"sess-{s}", turn)
    readiness = phase2_readiness(conn, days=14, delegation_table_path="/does/not/exist.md")
    assert readiness["C2"]["status"] == "not_met"


def test_c2_excludes_sidechain_turns(conn):
    # A session with only sidechain (subagent) turns should not count.
    for turn in range(5):
        seed_cc_usage(conn, "proj", "sess-sidechain", turn, is_sidechain=1)
    readiness = phase2_readiness(conn, days=14, delegation_table_path="/does/not/exist.md")
    assert readiness["C2"]["status"] == "not_met"


def test_c1_not_computable_when_no_real_traffic(conn):
    # NOTE: this fixture's SCHEMA (sqlite_logger.SCHEMA) defaults
    # traffic_kind to 'real'; the live gateway/requests.db column default is
    # 'synthetic' (verified via PRAGMA table_info -- see execution report,
    # this is a pre-existing schema/DB drift, not introduced here). Tag
    # explicitly as 'synthetic' so this test reflects the "no real traffic"
    # case regardless of which default is active.
    seed(conn, "lead", "AB", cost=0.01)
    seed(conn, "lead", "ABCD", cost=0.01)
    conn.execute("UPDATE requests SET traffic_kind = 'synthetic'")
    conn.commit()
    readiness = phase2_readiness(conn, days=14, delegation_table_path="/does/not/exist.md")
    assert readiness["C1"]["status"] == "not_computable_yet"
    assert "needs" in readiness["C1"]


def test_c1_met_on_real_traffic_above_threshold(conn):
    seed(conn, "lead", "AAAAAAAAAA", cost=0.01)   # 10 chars
    seed(conn, "lead", "AAAAAAAAAAAAAAAAAAAA", cost=0.01)  # 20 chars, 10 repeated
    conn.execute("UPDATE requests SET traffic_kind = 'real'")
    conn.commit()
    readiness = phase2_readiness(conn, days=14, delegation_table_path="/does/not/exist.md")
    assert readiness["C1"]["status"] == "met"  # 10/20 = 50% >= 40%


def test_c1_not_met_below_threshold(conn):
    seed(conn, "lead", "AAAAAAAAAAAAAAAAAAAA", cost=0.01)  # 20 chars
    seed(conn, "lead", "AAAXXXXXXXXXXXXXXXXX", cost=0.01)  # 3/20 = 15% repeated
    conn.execute("UPDATE requests SET traffic_kind = 'real'")
    conn.commit()
    readiness = phase2_readiness(conn, days=14, delegation_table_path="/does/not/exist.md")
    assert readiness["C1"]["status"] == "not_met"


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
    dtable = tmp_path / "DELEGATION_TABLE.md"
    dtable.write_text(SHADOW_EVAL_LOG_FIXTURE, encoding="utf-8")
    seed(conn, "lead", "AB", cost=0.01)
    digest = daily_digest(conn, days=14, delegation_table_path=dtable)
    assert "phase2_readiness" in digest
    assert digest["phase2_readiness"]["G2"]["status"] == "manual_check"
