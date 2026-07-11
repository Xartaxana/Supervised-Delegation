"""Tests for the Guard budget enforcement. No API keys required:
the hook is exercised directly against a seeded SQLite log.

Run: python -m pytest gateway/test_guard.py
"""

import asyncio
import datetime
import sqlite3

import pytest


def seed_request(db, model, cost_usd, ts=None):
    conn = sqlite3.connect(db)
    from sqlite_logger import SCHEMA

    conn.execute(SCHEMA)
    conn.execute(
        "INSERT INTO requests (ts, model, status, cost_usd) VALUES (?, ?, 'success', ?)",
        (ts or datetime.datetime.now().isoformat(), model, cost_usd),
    )
    conn.commit()
    conn.close()


def seed_tokens(db, model, prompt_tokens, completion_tokens, ts=None, traffic_kind=None):
    """Seed a request row with token usage but no cost_usd (0), so quota
    tests don't accidentally trip the unrelated $ budget wall."""
    conn = sqlite3.connect(db)
    from sqlite_logger import SCHEMA

    conn.execute(SCHEMA)
    if traffic_kind is None:
        conn.execute(
            "INSERT INTO requests (ts, model, status, cost_usd, prompt_tokens, completion_tokens)"
            " VALUES (?, ?, 'success', 0, ?, ?)",
            (ts or datetime.datetime.now().isoformat(), model, prompt_tokens, completion_tokens),
        )
    else:
        conn.execute(
            "INSERT INTO requests"
            " (ts, model, status, cost_usd, prompt_tokens, completion_tokens, traffic_kind)"
            " VALUES (?, ?, 'success', 0, ?, ?, ?)",
            (
                ts or datetime.datetime.now().isoformat(),
                model,
                prompt_tokens,
                completion_tokens,
                traffic_kind,
            ),
        )
    conn.commit()
    conn.close()


def seed_failure(db, model, ts=None):
    """Seed a failure row: no usage known yet, prompt_tokens/completion_tokens
    are NULL (matches sqlite_logger._failure_row, which never sets them)."""
    conn = sqlite3.connect(db)
    from sqlite_logger import SCHEMA

    conn.execute(SCHEMA)
    conn.execute(
        "INSERT INTO requests (ts, model, status, error) VALUES (?, ?, 'failure', 'boom')",
        (ts or datetime.datetime.now().isoformat(), model),
    )
    conn.commit()
    conn.close()


def events(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT model, level, spent_usd, budget_usd FROM budget_events"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def quota_events(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT model, window_seconds, level, spent_tokens, limit_tokens"
            " FROM quota_events"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def run_hook(model):
    from guard import guard_instance

    return asyncio.run(
        guard_instance.async_pre_call_hook(None, None, {"model": model}, "completion")
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\ndaily_usd:\n  lead: 1.00\n", encoding="utf-8"
    )
    # GATEWAY_DB_PATH already points at this db: the autouse fixture in
    # conftest.py sets it to tmp_path / "requests.db" for every test.
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    return db


def test_under_budget_passes(env):
    seed_request(env, "lead", 0.10)
    data = run_hook("lead")
    assert data == {"model": "lead"}
    assert events(env) == []


def test_no_budget_model_passes(env):
    seed_request(env, "other", 999.0)
    run_hook("other")
    assert events(env) == []


def test_warn_at_80_percent_once_per_day(env):
    seed_request(env, "lead", 0.85)
    run_hook("lead")
    run_hook("lead")
    assert [e[:2] for e in events(env)] == [("lead", "warn")]


def test_block_at_100_percent(env):
    from fastapi import HTTPException

    seed_request(env, "lead", 1.20)
    with pytest.raises(HTTPException) as exc:
        run_hook("lead")
    assert exc.value.status_code == 429
    assert "budget exhausted" in exc.value.detail
    assert ("lead", "block") in [e[:2] for e in events(env)]


def test_yesterday_spend_does_not_count(env):
    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat()
    seed_request(env, "lead", 5.00, ts=yesterday)
    run_hook("lead")
    assert events(env) == []


# --- Sliding-window token quotas (t-018) -----------------------------

@pytest.fixture()
def quota_env(tmp_path, monkeypatch):
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_windows:\n"
        "  tpm-model:\n"
        "    - window_seconds: 60\n"
        "      limit_tokens: 100\n"
        "  multi-model:\n"
        "    - window_seconds: 60\n"
        "      limit_tokens: 100\n"
        "    - window_seconds: 86400\n"
        "      limit_tokens: 1000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    return db


def test_quota_under_limit_passes(quota_env):
    seed_tokens(quota_env, "tpm-model", 30, 20)  # 50 < 100
    data = run_hook("tpm-model")
    assert data == {"model": "tpm-model"}
    assert quota_events(quota_env) == []


def test_quota_no_window_model_passes(quota_env):
    seed_tokens(quota_env, "unwalled", 99999, 99999)
    run_hook("unwalled")
    assert quota_events(quota_env) == []


def test_quota_warn_at_80_percent_once(quota_env):
    seed_tokens(quota_env, "tpm-model", 50, 35)  # 85 >= 0.8 * 100
    run_hook("tpm-model")
    run_hook("tpm-model")
    assert [e[:3] for e in quota_events(quota_env)] == [("tpm-model", 60, "warn")]


def test_quota_block_at_limit(quota_env):
    from fastapi import HTTPException

    seed_tokens(quota_env, "tpm-model", 70, 50)  # 120 >= 100
    with pytest.raises(HTTPException) as exc:
        run_hook("tpm-model")
    assert exc.value.status_code == 429
    assert "quota exhausted" in exc.value.detail
    assert "60s window" in exc.value.detail
    assert "120" in exc.value.detail and "100" in exc.value.detail
    blocks = [e for e in quota_events(quota_env) if e[2] == "block"]
    assert blocks == [("tpm-model", 60, "block", 120, 100)]


def test_quota_window_is_sliding_old_tokens_excluded(quota_env):
    """The decisive test: tokens older than window_seconds must NOT
    count, proving this is a rolling window and not a fixed
    clock-aligned bucket (e.g. per-minute-of-clock)."""
    stale = (datetime.datetime.now() - datetime.timedelta(seconds=61)).isoformat()
    seed_tokens(quota_env, "tpm-model", 90, 60, ts=stale)  # 150 tokens, but 61s old
    data = run_hook("tpm-model")
    assert data == {"model": "tpm-model"}
    assert quota_events(quota_env) == []


def test_quota_window_mixes_stale_and_fresh_correctly(quota_env):
    """Old tokens excluded, fresh tokens counted, in the same window
    check -- catches an implementation that either counts everything
    (fixed bucket) or drops everything (off-by-one on the cutoff)."""
    stale = (datetime.datetime.now() - datetime.timedelta(seconds=90)).isoformat()
    seed_tokens(quota_env, "tpm-model", 90, 60, ts=stale)  # excluded: 90s old
    seed_tokens(quota_env, "tpm-model", 10, 10)  # fresh: 20 tokens, counted
    data = run_hook("tpm-model")
    assert data == {"model": "tpm-model"}  # 20 < 100, passes
    assert quota_events(quota_env) == []


def test_quota_windows_are_independent_per_model(quota_env):
    """multi-model carries a tight 60s/100 wall and a loose
    86400s/1000 wall; 110 fresh tokens trips only the tight one."""
    from fastapi import HTTPException

    seed_tokens(quota_env, "multi-model", 60, 50)  # 110 tokens
    with pytest.raises(HTTPException) as exc:
        run_hook("multi-model")
    assert "60s window" in exc.value.detail
    assert "86400s" not in exc.value.detail
    blocks = [e for e in quota_events(quota_env) if e[2] == "block"]
    assert blocks == [("multi-model", 60, "block", 110, 100)]


def test_quota_block_message_has_wait_estimate(quota_env):
    from fastapi import HTTPException

    seed_tokens(quota_env, "tpm-model", 70, 50)
    with pytest.raises(HTTPException) as exc:
        run_hook("tpm-model")
    assert "Retry in ~" in exc.value.detail


# --- F1 (critic, t-018 attempt 2): three properties of tokens_in_window,
# each locked with its own assert on spent_tokens. ------------------------

def test_tokens_in_window_model_isolation(quota_env):
    """Tokens burned by model A inside the window must not count toward
    model B's wall -- each alias has its own quota."""
    from guard import _connect, tokens_in_window

    seed_tokens(quota_env, "multi-model", 500, 500)  # would blow tpm-model's 100 limit
    conn = _connect()
    try:
        since = (datetime.datetime.now() - datetime.timedelta(seconds=60)).isoformat()
        spent, earliest_ts = tokens_in_window(conn, "tpm-model", since)
    finally:
        conn.close()
    assert spent == 0
    assert earliest_ts is None


def test_tokens_in_window_null_usage_counts_as_zero(quota_env):
    """A failure row (prompt_tokens/completion_tokens NULL, matching
    sqlite_logger._failure_row) must not NULL-poison the SQL SUM and
    silently disable the wall -- COALESCE keeps the total a number."""
    from guard import _connect, tokens_in_window

    seed_failure(quota_env, "tpm-model")
    seed_tokens(quota_env, "tpm-model", 30, 20)  # 50 real tokens alongside the NULL row
    conn = _connect()
    try:
        since = (datetime.datetime.now() - datetime.timedelta(seconds=60)).isoformat()
        spent, earliest_ts = tokens_in_window(conn, "tpm-model", since)
    finally:
        conn.close()
    assert spent == 50
    assert earliest_ts is not None


def test_tokens_in_window_counts_every_traffic_kind(quota_env):
    """synthetic/judge/replay traffic burns the same physical Groq quota
    as 'real' traffic (it is the same API key hitting the same free-tier
    ceiling) -- the wall must count all traffic_kind values, not just
    'real'."""
    from guard import _connect, tokens_in_window

    for kind, tokens in (("real", 10), ("synthetic", 20), ("judge", 30), ("replay", 40)):
        seed_tokens(quota_env, "tpm-model", tokens, 0, traffic_kind=kind)
    conn = _connect()
    try:
        since = (datetime.datetime.now() - datetime.timedelta(seconds=60)).isoformat()
        spent, _ = tokens_in_window(conn, "tpm-model", since)
    finally:
        conn.close()
    assert spent == 10 + 20 + 30 + 40
