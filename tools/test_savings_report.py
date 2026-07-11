# -*- coding: utf-8 -*-
"""Tests for tools/savings_report.py (the savings/trend calibration
check): counterfactual math, window splitting, the API-contour slice --
against a tmp database covering both schemas."""
import sqlite3

import pytest

from savings_report import (
    api_contour_summary,
    counterfactual_summary,
    fable_counterfactual,
    window_summary,
)
from usage_report import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER, PRICES_PER_TOKEN_USD


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute(
        "CREATE TABLE cc_usage (ts TEXT, project TEXT, session_id TEXT,"
        " model TEXT, input_tokens INT, output_tokens INT,"
        " cache_creation_tokens INT, cache_read_tokens INT,"
        " accounted_cost_usd REAL, is_sidechain INT, agent_type TEXT)")
    conn.execute(
        "CREATE TABLE requests (ts TEXT, model TEXT, cost_usd REAL,"
        " traffic_kind TEXT)")
    return conn


def _cc(conn, ts, model, side, i=100, o=50, cw=0, cr=0, cost=1.0,
        agent=None, sess="s1"):
    conn.execute(
        "INSERT INTO cc_usage VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, "p", sess, model, i, o, cw, cr, cost, side, agent))


def test_fable_counterfactual_math():
    fp = PRICES_PER_TOKEN_USD["claude-fable-5"]
    got = fable_counterfactual(1000, 200, 400, 8000)
    expected = (1000 * fp[0] + 200 * fp[1]
                + 400 * fp[0] * CACHE_WRITE_MULTIPLIER
                + 8000 * fp[0] * CACHE_READ_MULTIPLIER)
    assert got == pytest.approx(expected)


def test_window_split_pre_vs_routed(db):
    _cc(db, "2026-07-05T10:00:00", "claude-sonnet-5", 0, cost=2.0)
    _cc(db, "2026-07-09T10:00:00", "claude-fable-5", 0, cost=5.0)
    pre = window_summary(db, "ts < ?", ("2026-07-08",))
    routed = window_summary(db, "ts >= ?", ("2026-07-08",))
    assert pre["total_cost"] == pytest.approx(2.0)
    assert routed["total_cost"] == pytest.approx(5.0)
    assert pre["days"] == 1 and routed["days"] == 1


def test_counterfactual_only_sidechains_in_window(db):
    # a sidechain inside the window counts; main and a pre-window sidechain don't
    _cc(db, "2026-07-09T10:00:00", "claude-haiku-4-5-20251001", 1,
        i=1000, o=100, cost=0.002, agent="scout")
    _cc(db, "2026-07-09T11:00:00", "claude-fable-5", 0, cost=9.0)
    _cc(db, "2026-07-01T10:00:00", "claude-sonnet-5", 1, cost=1.0, agent="builder")
    c = counterfactual_summary(db, "ts >= ?", ("2026-07-08",))
    assert len(c["detail"]) == 1
    assert c["detail"][0]["agent_type"] == "scout"
    assert c["actual"] == pytest.approx(0.002)
    assert c["as_fable"] == pytest.approx(fable_counterfactual(1000, 100, 0, 0))
    assert c["gross_savings"] == pytest.approx(c["as_fable"] - 0.002)


def test_api_contour_summary_groups_by_kind(db):
    db.execute("INSERT INTO requests VALUES ('2026-07-09','judge-groq',0.01,'judge')")
    db.execute("INSERT INTO requests VALUES ('2026-07-09','lead-sonnet',0.02,'synthetic')")
    db.execute("INSERT INTO requests VALUES ('2026-07-10','lead-sonnet',0.03,'synthetic')")
    a = api_contour_summary(db)
    assert a["total_n"] == 3
    assert a["total_cost"] == pytest.approx(0.06)
    kinds = {k: (n, c) for k, n, c in a["kinds"]}
    assert kinds["synthetic"][0] == 2
