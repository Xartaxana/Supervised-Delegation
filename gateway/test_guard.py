"""Tests for the Guard budget enforcement. No API keys required:
the hook is exercised directly against a seeded SQLite log.

Run: python -m pytest gateway/test_guard.py
"""

import asyncio
import datetime
import sqlite3

import litellm
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


def run_hook_data(data):
    """Sibling of run_hook above that lets a test control the FULL
    request payload (messages/max_tokens), needed to exercise the
    projected-spend wall through the real hook path."""
    from guard import guard_instance

    return asyncio.run(guard_instance.async_pre_call_hook(None, None, data, "completion"))


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


# --- Sliding-window token quotas -----------------------------

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


# --- F1 (carried forward from review): three properties of tokens_in_window,
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


# --- Projected-spend wall (external review, P0) --------------------------
#
# Convention shared by every test below that needs a deterministic
# prompt_estimate: monkeypatch litellm.token_counter to return a fixed
# number so the boundary math is exact and independent of tokenizer
# version drift. The one exception is the fallback-heuristic battery,
# which deliberately forces an EXCEPTION instead.


@pytest.fixture()
def zero_prompt_estimate(monkeypatch):
    """prompt_estimate is always 0 for every model under this fixture,
    isolating the boundary math to output_allowance alone."""
    monkeypatch.setattr(litellm, "token_counter", lambda **kwargs: 0)


# --- spent + projected_tokens boundary, alias quota_windows -------------

@pytest.fixture()
def bound_env(tmp_path, monkeypatch):
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_windows:\n"
        "  bound-model:\n"
        "    - window_seconds: 60\n"
        "      limit_tokens: 200\n"
        "projection:\n"
        "  assumed_max_output_tokens: 150\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    return db


def test_projected_tokens_passes_at_limit_minus_one(bound_env, zero_prompt_estimate):
    seed_tokens(bound_env, "bound-model", 49, 0)  # spent=49; +150 allowance = 199 = limit-1
    data = run_hook_data({"model": "bound-model", "messages": []})
    assert data == {"model": "bound-model", "messages": []}
    assert quota_events(bound_env) == []


def test_projected_tokens_blocks_at_limit(bound_env, zero_prompt_estimate):
    from fastapi import HTTPException

    seed_tokens(bound_env, "bound-model", 50, 0)  # spent=50; +150 allowance = 200 = limit
    with pytest.raises(HTTPException) as exc:
        run_hook_data({"model": "bound-model", "messages": []})
    assert exc.value.status_code == 429
    blocks = [e for e in quota_events(bound_env) if e[2] == "block_projected"]
    assert blocks == [("bound-model", 60, "block_projected", 50, 200)]


def test_post_fact_block_wins_over_projection_when_already_over(quota_env):
    """Regression: spent already >= limit must still raise the OLD
    plain 'block' (not 'block_projected'), even when a real payload with
    its own max_tokens is present -- the post-fact branch stays first."""
    from fastapi import HTTPException

    seed_tokens(quota_env, "tpm-model", 70, 50)  # 120 >= 100 already
    with pytest.raises(HTTPException):
        run_hook_data(
            {
                "model": "tpm-model",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 500,
            }
        )
    matches = [e for e in quota_events(quota_env) if e[0] == "tpm-model"]
    assert matches[-1][2] == "block"


# --- quota_pools aggregate across aliases -------------------------------

@pytest.fixture()
def pool_env(tmp_path, monkeypatch):
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_pools:\n"
        "  shared-groq:\n"
        "    aliases: [alias-a, alias-b]\n"
        "    windows:\n"
        "      - window_seconds: 60\n"
        "        limit_tokens: 100\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    return db


def test_quota_pool_under_limit_passes(pool_env):
    seed_tokens(pool_env, "alias-a", 30, 0)
    seed_tokens(pool_env, "alias-b", 30, 0)  # combined 60 < 100
    data = run_hook("alias-a")
    assert data == {"model": "alias-a"}
    assert quota_events(pool_env) == []


def test_quota_pool_aggregates_across_aliases_and_blocks(pool_env):
    from fastapi import HTTPException

    seed_tokens(pool_env, "alias-a", 60, 0)
    seed_tokens(pool_env, "alias-b", 60, 0)  # combined 120 >= 100
    with pytest.raises(HTTPException) as exc:
        run_hook("alias-a")
    assert exc.value.status_code == 429
    blocks = [e for e in quota_events(pool_env) if e[2] == "block"]
    assert blocks == [("pool:shared-groq", 60, "block", 120, 100)]


def test_quota_pool_data_none_still_blocks_post_fact(pool_env):
    """quota_pools has no pre-projection legacy contract of its own,
    but a direct call with data=None still applies its post-fact wall
    (only the projection add-on is skipped)."""
    from fastapi import HTTPException

    from guard import check_quota_pools

    seed_tokens(pool_env, "alias-a", 60, 0)
    seed_tokens(pool_env, "alias-b", 60, 0)
    with pytest.raises(HTTPException):
        check_quota_pools("alias-a", data=None)


def test_quota_pool_empty_aliases_is_noop_not_broken_sql(tmp_path, monkeypatch):
    """A pool with an empty `aliases` list must not build a malformed
    'IN ()' SQL clause -- tokens_in_window_for_aliases short-circuits to
    (0, None), so the wall is simply a no-op for that pool."""
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_pools:\n"
        "  empty-pool:\n"
        "    aliases: []\n"
        "    windows:\n"
        "      - window_seconds: 60\n"
        "        limit_tokens: 10\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    seed_tokens(db, "alias-a", 999, 999)

    from guard import check_quota_pools

    check_quota_pools("alias-a", data=None)  # must not raise
    assert quota_events(db) == []


def test_quota_pool_with_no_windows_is_noop(tmp_path, monkeypatch):
    """A pool an alias belongs to but with no `windows` entries must be
    a no-op, not an error."""
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_pools:\n"
        "  no-window-pool:\n"
        "    aliases: [alias-a]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    seed_tokens(db, "alias-a", 999, 999)

    from guard import check_quota_pools

    check_quota_pools("alias-a", data=None)  # must not raise
    assert quota_events(db) == []


def test_model_in_no_pool_is_noop(pool_env):
    """A model that belongs to no configured pool at all is a no-op,
    same convention as check_quota_windows for an unwalled alias."""
    data = run_hook("completely-unlisted-model")
    assert data == {"model": "completely-unlisted-model"}
    assert quota_events(pool_env) == []


# --- token_counter exception -> heuristic fallback, request lives -------

def test_token_counter_exception_falls_back_and_blocks_at_heuristic_boundary(
    tmp_path, monkeypatch
):
    messages = [{"role": "user", "content": "x" * 400}]
    expected_heuristic = sum(len(str(m)) // 4 for m in messages)

    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_windows:\n"
        "  fallback-model:\n"
        "    - window_seconds: 60\n"
        f"      limit_tokens: {expected_heuristic}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))

    def boom(**kwargs):
        raise RuntimeError("no tokenizer for this model")

    monkeypatch.setattr(litellm, "token_counter", boom)
    seed_tokens(db, "fallback-model", 0, 0)  # establishes the requests table, spent=0

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        run_hook_data({"model": "fallback-model", "messages": messages})
    blocks = [e for e in quota_events(db) if e[2] == "block_projected"]
    assert blocks == [("fallback-model", 60, "block_projected", 0, expected_heuristic)]


def test_token_counter_exception_falls_back_and_request_survives_under_limit(
    tmp_path, monkeypatch
):
    """An exception inside the tokenizer must never propagate and never
    kill the request -- only the fallback heuristic value changes the
    projection math."""
    messages = [{"role": "user", "content": "x" * 400}]
    expected_heuristic = sum(len(str(m)) // 4 for m in messages)

    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_windows:\n"
        "  fallback-model:\n"
        "    - window_seconds: 60\n"
        f"      limit_tokens: {expected_heuristic + 1}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))

    def boom(**kwargs):
        raise RuntimeError("no tokenizer for this model")

    monkeypatch.setattr(litellm, "token_counter", boom)
    seed_tokens(db, "fallback-model", 0, 0)  # establishes the requests table, spent=0

    data = run_hook_data({"model": "fallback-model", "messages": messages})
    assert data == {"model": "fallback-model", "messages": messages}
    assert quota_events(db) == []


# --- explicit max_tokens drives the boundary exactly --------------------

@pytest.fixture()
def mt_env(tmp_path, monkeypatch):
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_windows:\n"
        "  mt-model:\n"
        "    - window_seconds: 60\n"
        "      limit_tokens: 50\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    return db


def test_explicit_max_tokens_passes_just_under_boundary(mt_env, zero_prompt_estimate):
    seed_tokens(mt_env, "mt-model", 0, 0)  # establishes the requests table, spent=0
    data = run_hook_data({"model": "mt-model", "messages": [], "max_tokens": 49})
    assert data == {"model": "mt-model", "messages": [], "max_tokens": 49}
    assert quota_events(mt_env) == []


def test_explicit_max_tokens_blocks_exactly_at_boundary(mt_env, zero_prompt_estimate):
    from fastapi import HTTPException

    seed_tokens(mt_env, "mt-model", 0, 0)  # establishes the requests table, spent=0
    with pytest.raises(HTTPException):
        run_hook_data({"model": "mt-model", "messages": [], "max_tokens": 50})
    blocks = [e for e in quota_events(mt_env) if e[2] == "block_projected"]
    assert blocks == [("mt-model", 60, "block_projected", 0, 50)]


def test_max_tokens_zero_is_not_used_falls_back_to_assumed(tmp_path, monkeypatch, zero_prompt_estimate):
    """max_tokens=0 is not a positive int -- output_allowance falls
    through to projection.assumed_max_output_tokens instead of treating
    0 as the request's own allowance."""
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_windows:\n"
        "  zero-mt-model:\n"
        "    - window_seconds: 60\n"
        "      limit_tokens: 20\n"
        "projection:\n"
        "  assumed_max_output_tokens: 25\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    seed_tokens(db, "zero-mt-model", 0, 0)

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        run_hook_data({"model": "zero-mt-model", "messages": [], "max_tokens": 0})
    blocks = [e for e in quota_events(db) if e[2] == "block_projected"]
    assert blocks == [("zero-mt-model", 60, "block_projected", 0, 20)]


def test_max_tokens_negative_is_not_used_falls_back_to_assumed(
    tmp_path, monkeypatch, zero_prompt_estimate
):
    """max_tokens=-1 is not a positive int either."""
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_windows:\n"
        "  neg-mt-model:\n"
        "    - window_seconds: 60\n"
        "      limit_tokens: 20\n"
        "projection:\n"
        "  assumed_max_output_tokens: 25\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    seed_tokens(db, "neg-mt-model", 0, 0)

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        run_hook_data({"model": "neg-mt-model", "messages": [], "max_tokens": -1})
    blocks = [e for e in quota_events(db) if e[2] == "block_projected"]
    assert blocks == [("neg-mt-model", 60, "block_projected", 0, 20)]


def test_max_tokens_bool_is_not_used_falls_back_to_assumed(
    tmp_path, monkeypatch, zero_prompt_estimate
):
    """max_tokens=True must not be treated as the int 1 (or as a
    positive allowance at all) -- bool is excluded even though Python's
    isinstance(True, int) is True."""
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_windows:\n"
        "  bool-mt-model:\n"
        "    - window_seconds: 60\n"
        "      limit_tokens: 20\n"
        "projection:\n"
        "  assumed_max_output_tokens: 25\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    seed_tokens(db, "bool-mt-model", 0, 0)

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        run_hook_data({"model": "bool-mt-model", "messages": [], "max_tokens": True})
    blocks = [e for e in quota_events(db) if e[2] == "block_projected"]
    assert blocks == [("bool-mt-model", 60, "block_projected", 0, 20)]


def test_max_tokens_string_is_not_used_falls_back_to_assumed(
    tmp_path, monkeypatch, zero_prompt_estimate
):
    """max_tokens as a string (e.g. "500") is not a native int -- must
    not be silently parsed; falls back to the configured assumed
    allowance instead."""
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_windows:\n"
        "  str-mt-model:\n"
        "    - window_seconds: 60\n"
        "      limit_tokens: 20\n"
        "projection:\n"
        "  assumed_max_output_tokens: 25\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    seed_tokens(db, "str-mt-model", 0, 0)

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        run_hook_data({"model": "str-mt-model", "messages": [], "max_tokens": "500"})
    blocks = [e for e in quota_events(db) if e[2] == "block_projected"]
    assert blocks == [("str-mt-model", 60, "block_projected", 0, 20)]


def test_max_tokens_missing_uses_global_assumed_default_passes_just_under(
    tmp_path, monkeypatch, zero_prompt_estimate
):
    """max_tokens absent -> falls back to projection.assumed_max_output_tokens
    (here a plain global int), exercised at its own boundary: 29 stays
    under the 30-token limit (0 prompt + 29 assumed = 29 < 30)."""
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_windows:\n"
        "  assumed-model:\n"
        "    - window_seconds: 60\n"
        "      limit_tokens: 30\n"
        "projection:\n"
        "  assumed_max_output_tokens: 29\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))

    seed_tokens(db, "assumed-model", 0, 0)  # establishes the requests table, spent=0
    data = run_hook_data({"model": "assumed-model", "messages": []})  # no max_tokens key
    assert data == {"model": "assumed-model", "messages": []}
    assert quota_events(db) == []


def test_max_tokens_missing_uses_global_assumed_default_blocks_at_boundary(
    tmp_path, monkeypatch, zero_prompt_estimate
):
    """Same fixture, assumed bumped to 30: 0 prompt + 30 assumed = 30 >=
    the 30-token limit -> blocks exactly at the boundary."""
    from fastapi import HTTPException

    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_windows:\n"
        "  assumed-model:\n"
        "    - window_seconds: 60\n"
        "      limit_tokens: 30\n"
        "projection:\n"
        "  assumed_max_output_tokens: 30\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))

    seed_tokens(db, "assumed-model", 0, 0)  # establishes the requests table, spent=0
    with pytest.raises(HTTPException):
        run_hook_data({"model": "assumed-model", "messages": []})
    blocks = [e for e in quota_events(db) if e[2] == "block_projected"]
    assert blocks == [("assumed-model", 60, "block_projected", 0, 30)]


# --- assumed_max_output_tokens as a {default, <alias>} map --------------

@pytest.fixture()
def assumed_dict_env(tmp_path, monkeypatch):
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_windows:\n"
        "  special-model:\n"
        "    - window_seconds: 60\n"
        "      limit_tokens: 40\n"
        "  generic-model:\n"
        "    - window_seconds: 60\n"
        "      limit_tokens: 10\n"
        "projection:\n"
        "  assumed_max_output_tokens:\n"
        "    default: 10\n"
        "    special-model: 40\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    return db


def test_assumed_output_tokens_alias_override_used(assumed_dict_env, zero_prompt_estimate):
    """special-model's own override (40) must win over 'default' (10) --
    if the default leaked in instead, 0+10=10 < 40 would wrongly pass."""
    from fastapi import HTTPException

    seed_tokens(assumed_dict_env, "special-model", 0, 0)  # requests table exists, spent=0
    with pytest.raises(HTTPException):
        run_hook_data({"model": "special-model", "messages": []})
    blocks = [e for e in quota_events(assumed_dict_env) if e[2] == "block_projected"]
    assert blocks == [("special-model", 60, "block_projected", 0, 40)]


def test_assumed_output_tokens_default_used_for_unlisted_alias(
    assumed_dict_env, zero_prompt_estimate
):
    """generic-model has no entry of its own -> falls back to 'default'
    (10) -- if the fallback silently returned 0 instead, 0+0=0 < 10
    would wrongly pass."""
    from fastapi import HTTPException

    seed_tokens(assumed_dict_env, "generic-model", 0, 0)  # requests table exists, spent=0

    with pytest.raises(HTTPException):
        run_hook_data({"model": "generic-model", "messages": []})
    blocks = [e for e in quota_events(assumed_dict_env) if e[2] == "block_projected"]
    assert blocks == [("generic-model", 60, "block_projected", 0, 10)]


# --- $ projection -- fail-closed vs. warn-once-per-day ------------------

@pytest.fixture()
def usd_env(tmp_path, monkeypatch):
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd:\n"
        "  no-price-fc: 5.00\n"
        "  no-price-open: 5.00\n"
        "  priced-model: 1.00\n"
        "projection:\n"
        "  usd_prices:\n"
        "    priced-model:\n"
        "      prompt_per_1k: 0.0\n"
        "      completion_per_1k: 1.0\n"
        "  fail_closed_aliases: [no-price-fc]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    return db


def test_fail_closed_alias_without_price_blocks(usd_env):
    """A fail-closed refusal must leave a "block_no_price" row in
    budget_events -- an unpriced fail-closed refusal with NO event row
    at all would be invisible to any level-agnostic digest scan."""
    from fastapi import HTTPException

    seed_request(usd_env, "no-price-fc", 0.0)  # establishes the requests table, spent=0
    with pytest.raises(HTTPException) as exc:
        run_hook("no-price-fc")
    assert exc.value.status_code == 429
    assert "fail-closed" in exc.value.detail
    rows = [e for e in events(usd_env) if e[0] == "no-price-fc"]
    assert rows == [("no-price-fc", "block_no_price", 0.0, 5.00)]  # not fabricated, real spent/budget


def test_fail_closed_block_event_row_exists_in_raw_table(usd_env):
    """Belt-and-braces on top of the assertion above: query budget_events
    directly (not through the events() column projection) to prove the
    row is a real, queryable INSERT, not just a coincidental tuple
    match -- this is exactly what a level-agnostic digest scan would
    find."""
    from fastapi import HTTPException

    seed_request(usd_env, "no-price-fc", 0.0)
    with pytest.raises(HTTPException):
        run_hook("no-price-fc")
    conn = sqlite3.connect(usd_env)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM budget_events WHERE model = 'no-price-fc'"
            " AND level = 'block_no_price'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_open_alias_without_price_warns_once_and_passes(usd_env):
    seed_request(usd_env, "no-price-open", 0.0)  # establishes the requests table, spent=0
    data = run_hook("no-price-open")
    assert data == {"model": "no-price-open"}
    data = run_hook("no-price-open")
    assert data == {"model": "no-price-open"}
    rows = [e for e in events(usd_env) if e[0] == "no-price-open"]
    assert rows == [("no-price-open", "warn_no_price", 0.0, 5.00)]


def test_budget_projected_usd_passes_just_under_boundary(usd_env, zero_prompt_estimate):
    seed_request(usd_env, "priced-model", 0.0)  # establishes the requests table, spent=0
    data = run_hook_data({"model": "priced-model", "messages": [], "max_tokens": 999})
    assert data == {"model": "priced-model", "messages": [], "max_tokens": 999}
    assert events(usd_env) == []


def test_budget_projected_usd_blocks_at_boundary(usd_env, zero_prompt_estimate):
    from fastapi import HTTPException

    seed_request(usd_env, "priced-model", 0.0)  # establishes the requests table, spent=0

    with pytest.raises(HTTPException) as exc:
        run_hook_data({"model": "priced-model", "messages": [], "max_tokens": 1000})
    assert exc.value.status_code == 429
    assert "projected" in exc.value.detail
    blocks = [e for e in events(usd_env) if e[1] == "block_projected"]
    assert blocks == [("priced-model", "block_projected", 0.0, 1.00)]


def test_alias_with_no_projection_section_is_unaffected(env):
    """An alias whose config has NO "projection" section at all must be
    completely unaffected by the projection machinery, even though the
    hook path always passes a real `data` payload with messages/
    max_tokens -- this is the byte-for-byte compatibility gate."""
    seed_request(env, "lead", 0.10)
    data = run_hook_data(
        {"model": "lead", "messages": [{"role": "user", "content": "x" * 100000}], "max_tokens": 999999}
    )
    assert data["model"] == "lead"
    assert events(env) == []


# --- data=None direct-call regression pin: byte-for-byte legacy contract

def test_data_none_matches_legacy_budget_behavior(env):
    from guard import check_budget

    seed_request(env, "lead", 0.10)
    check_budget("lead", data=None)  # must not raise
    assert events(env) == []

    seed_request(env, "lead", 1.20)  # cumulative spend now 1.30 >= 1.00 budget
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        check_budget("lead", data=None)
    assert ("lead", "block") in [e[:2] for e in events(env)]


def test_data_none_matches_legacy_quota_behavior(quota_env):
    from guard import check_quota_windows

    seed_tokens(quota_env, "tpm-model", 30, 20)  # 50 < 100
    check_quota_windows("tpm-model", data=None)  # must not raise
    assert quota_events(quota_env) == []

    seed_tokens(quota_env, "tpm-model", 70, 50)  # cumulative 120 >= 100
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        check_quota_windows("tpm-model", data=None)


def test_data_none_check_quota_pools_skips_projection_branch_only(pool_env):
    """data=None on check_quota_pools skips only the projection add-on
    (per its own docstring) -- the post-fact wall above already proves
    it still blocks; here we prove the UNDER-limit case still passes
    with data=None exactly like the post-fact-only contract."""
    from guard import check_quota_pools

    seed_tokens(pool_env, "alias-a", 30, 0)
    seed_tokens(pool_env, "alias-b", 30, 0)  # combined 60 < 100
    check_quota_pools("alias-a", data=None)  # must not raise
    assert quota_events(pool_env) == []


# --- data={} behaves like data=None for the projection-gated branches ---


def test_data_empty_dict_treated_like_none_for_quota_projection(quota_env):
    """An empty dict is not None but also carries no messages/max_tokens
    -- projected_tokens must reduce to a 0-length messages estimate,
    never raise on a missing "messages" key."""
    seed_tokens(quota_env, "tpm-model", 30, 20)  # 50 < 100
    data = run_hook_data({"model": "tpm-model"})
    assert data == {"model": "tpm-model"}
    assert quota_events(quota_env) == []


# --- assumed-output-tokens malformation and latency fixes ---------------


def test_quoted_assumed_output_tokens_falls_back_and_warns(tmp_path, monkeypatch, zero_prompt_estimate):
    """A quoted scalar in YAML (`assumed_max_output_tokens: {default:
    "2048"}`) must not raise inside the projection path -- the request
    stays alive, treated as if nothing were configured (0), with a
    once-per-day warning making the misconfiguration visible."""
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_windows:\n"
        "  quoted-model:\n"
        "    - window_seconds: 60\n"
        "      limit_tokens: 10\n"
        "projection:\n"
        "  assumed_max_output_tokens:\n"
        '    default: "2048"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    seed_tokens(db, "quoted-model", 0, 0)  # establishes the requests table, spent=0

    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    # Must not raise (0+0 output_allowance < 10 limit -- proves the quoted
    # "2048" was NOT silently coerced into a real 2048, which would block).
    data = run_hook_data({"model": "quoted-model", "messages": []})
    assert data == {"model": "quoted-model", "messages": []}
    assert quota_events(db) == []

    warn_rows = [e for e in events(db) if e[1] == "warn_bad_assumed_output"]
    assert warn_rows == [("quoted-model", "warn_bad_assumed_output", 0.0, 0.0)]
    assert any("not a valid integer" in line for line in printed)


def test_quoted_assumed_output_tokens_warns_only_once_per_day(
    tmp_path, monkeypatch, zero_prompt_estimate
):
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_windows:\n"
        "  quoted-model:\n"
        "    - window_seconds: 60\n"
        "      limit_tokens: 10\n"
        "projection:\n"
        '  assumed_max_output_tokens: "2048"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    seed_tokens(db, "quoted-model", 0, 0)

    run_hook_data({"model": "quoted-model", "messages": []})
    run_hook_data({"model": "quoted-model", "messages": []})
    warn_rows = [e for e in events(db) if e[1] == "warn_bad_assumed_output"]
    assert len(warn_rows) == 1


def test_token_counter_called_once_per_call_across_multiple_windows(quota_env, monkeypatch):
    """multi-model carries TWO quota_windows entries (60s and 86400s);
    projected_tokens must be computed ONCE per check_quota_windows
    call, not once per window -- otherwise litellm.token_counter would
    run N times per pre-call request for no benefit."""
    calls = []

    def counting_token_counter(**kwargs):
        calls.append(1)
        return 0

    monkeypatch.setattr(litellm, "token_counter", counting_token_counter)
    seed_tokens(quota_env, "multi-model", 0, 0)

    run_hook_data({"model": "multi-model", "messages": [{"role": "user", "content": "hi"}]})
    assert len(calls) == 1


def test_token_counter_called_once_per_call_across_pool_windows(tmp_path, monkeypatch):
    """Same latency fix, pool side: a pool with 2 windows across 2
    aliases must not multiply the token_counter calls per window."""
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_pools:\n"
        "  shared-groq:\n"
        "    aliases: [alias-a, alias-b]\n"
        "    windows:\n"
        "      - window_seconds: 60\n"
        "        limit_tokens: 1000\n"
        "      - window_seconds: 86400\n"
        "        limit_tokens: 5000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    seed_tokens(db, "alias-a", 0, 0)

    calls = []

    def counting_token_counter(**kwargs):
        calls.append(1)
        return 0

    monkeypatch.setattr(litellm, "token_counter", counting_token_counter)
    run_hook_data({"model": "alias-a", "messages": [{"role": "user", "content": "hi"}]})
    assert len(calls) == 1


# --- quota_pools get a warn_ratio branch, same debounce -----------------

@pytest.fixture()
def pool_warn_env(tmp_path, monkeypatch):
    db = tmp_path / "requests.db"
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        "warn_ratio: 0.8\n"
        "daily_usd: {}\n"
        "quota_pools:\n"
        "  shared-groq:\n"
        "    aliases: [alias-a, alias-b]\n"
        "    windows:\n"
        "      - window_seconds: 60\n"
        "        limit_tokens: 100\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_BUDGETS_PATH", str(budgets))
    return db


def test_quota_pool_warns_just_under_warn_ratio_boundary(pool_warn_env):
    seed_tokens(pool_warn_env, "alias-a", 79, 0)  # 79 < 0.8*100=80: no warn yet
    data = run_hook("alias-a")
    assert data == {"model": "alias-a"}
    assert quota_events(pool_warn_env) == []


def test_quota_pool_warns_at_warn_ratio_boundary_once(pool_warn_env):
    seed_tokens(pool_warn_env, "alias-a", 80, 0)  # 80 >= 0.8*100=80: warns
    run_hook("alias-a")
    run_hook("alias-a")  # debounced: still only one warn row
    warns = [e for e in quota_events(pool_warn_env) if e[2] == "warn"]
    assert warns == [("pool:shared-groq", 60, "warn", 80, 100)]
