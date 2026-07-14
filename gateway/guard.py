"""Guard: deterministic budget enforcement in the request path.

ARCHITECTURE.md, "Guard"; D-0027. No LLM involved.

Why not LiteLLM's native budgets (D-0030 evaluation, 2026-07-03):
they require Postgres (and Redis for cross-worker counters), both
explicitly deferred by ARCHITECTURE.md until the MVP stack fails,
and they have no per-model 80%-warning semantics. This hook reuses
the SQLite request log the gateway already writes.

Budgets are configuration, not code: budgets.yaml next to this file,
overridable via GATEWAY_BUDGETS_PATH.

Semantics (per gateway alias, per local calendar day):
- spend >= warn_ratio * budget: a 'warn' row in budget_events
  (once per model per day) and a proxy log line;
- spend >= budget: request refused with HTTP 429, a 'block' row
  in budget_events.

Sliding-window TOKEN quotas: provider free-tier limits are often
rolling windows rather than calendar-day resets (verify whether your
provider's daily limit is a rolling 24h window or a calendar day
before relying on either assumption -- a wrong assumption here fails
silently until the provider's own count disagrees with yours), so
they cannot reuse the calendar-day $ budget logic above. `quota_windows`
in the same config file lists, per gateway alias, one or more
{window_seconds, limit_tokens} walls; each is checked independently
against prompt_tokens+completion_tokens summed over `requests` rows
with ts within window_seconds of now (a true sliding window: a
request only ages out once its own timestamp is older than
window_seconds, not at a fixed clock boundary). Same warn/block
semantics as the $ budgets, recorded in a separate quota_events
table (different unit: tokens, not USD). D-0030 native-first check
(2026-07-09): litellm 1.90.2 ships router_strategy/lowest_tpm_rpm_v2.py,
a per-deployment RPM/TPM limiter that works Redis-less (falls back to
an in-memory DualCache) -- but its window is a fixed calendar-minute
bucket (keyed by strftime("%H-%M")), not sliding, and it has no TPD
(24h) primitive at all; it is also a Router/multi-deployment
mechanism (`routing_strategy: usage-based-routing-v2`), not a
single-deployment pre-call gate. It does not cover the TPD wall this
task needs, so building on it here would mean running native TPM
alongside a hand-rolled TPD -- two mechanisms for one wall class.
Left as an open question for Lead (see task report), not adopted.
"""

import datetime
import os
import sqlite3
from pathlib import Path

import yaml
from litellm.integrations.custom_logger import CustomLogger

from sqlite_logger import db_path

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    model TEXT NOT NULL,
    level TEXT NOT NULL,
    spent_usd REAL NOT NULL,
    budget_usd REAL NOT NULL
);
"""

QUOTA_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    model TEXT NOT NULL,
    window_seconds INTEGER NOT NULL,
    level TEXT NOT NULL,
    spent_tokens INTEGER NOT NULL,
    limit_tokens INTEGER NOT NULL
);
"""


def budgets_path() -> Path:
    return Path(os.environ.get("GATEWAY_BUDGETS_PATH", Path(__file__).parent / "budgets.yaml"))


def load_budgets() -> dict:
    path = budgets_path()
    if not path.exists():
        return {"warn_ratio": 0.8, "daily_usd": {}, "quota_windows": {}}
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    config.setdefault("warn_ratio", 0.8)
    config.setdefault("daily_usd", {})
    config.setdefault("quota_windows", {})
    return config


def daily_budget(config: dict, model: str):
    budgets = config["daily_usd"]
    return budgets.get(model, budgets.get("default"))


def quota_windows_for(config: dict, model: str) -> list:
    """List of {window_seconds, limit_tokens} walls configured for model.

    No "default" fallback (unlike daily_budget): a model absent from
    quota_windows has no token wall, matching the "missing entry means
    no limit" convention the $ budgets already use.
    """
    return config["quota_windows"].get(model, [])


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.execute(EVENTS_SCHEMA)
    conn.execute(QUOTA_EVENTS_SCHEMA)
    return conn


def spent_today(conn: sqlite3.Connection, model: str, today: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM requests"
        " WHERE model = ? AND substr(ts, 1, 10) = ?",
        (model, today),
    ).fetchone()
    return row[0]


def _record_event(conn, model, level, spent, budget, now):
    conn.execute(
        "INSERT INTO budget_events (ts, model, level, spent_usd, budget_usd)"
        " VALUES (?, ?, ?, ?, ?)",
        (now, model, level, spent, budget),
    )
    conn.commit()


def _warned_today(conn, model, today) -> bool:
    row = conn.execute(
        "SELECT 1 FROM budget_events"
        " WHERE model = ? AND level = 'warn' AND substr(ts, 1, 10) = ? LIMIT 1",
        (model, today),
    ).fetchone()
    return row is not None


def tokens_in_window(conn: sqlite3.Connection, model: str, since_iso: str):
    """(total_tokens, earliest_ts) for `model` requests with ts >= since_iso.

    One SQL aggregate (sibling of spent_today above), not a fetchall +
    Python sum: avoids pulling the whole window's rows into Python on
    every proxy request. total_tokens sums prompt_tokens+completion_tokens
    (NULLs treated as 0: a request logged before usage is known, e.g. a
    failure row, must not make the sum NULL and silently disable the
    wall -- and rows are counted regardless of traffic_kind, since
    synthetic/judge/replay traffic burns the same physical Groq quota as
    real traffic). earliest_ts is MIN(ts) of the window, used to
    estimate when the oldest row ages out (None when the window is
    empty).
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)), 0),"
        " MIN(ts) FROM requests WHERE model = ? AND ts >= ?",
        (model, since_iso),
    ).fetchone()
    return row[0], row[1]


def _record_quota_event(conn, model, window_seconds, level, spent, limit, now):
    conn.execute(
        "INSERT INTO quota_events"
        " (ts, model, window_seconds, level, spent_tokens, limit_tokens)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (now, model, window_seconds, level, spent, limit),
    )
    conn.commit()


def _quota_warned_recently(conn, model, window_seconds, now: datetime.datetime) -> bool:
    """True if a 'warn' for this (model, window) fired within the last
    window_seconds -- the sliding-window analog of _warned_today's
    once-per-calendar-day debounce (there is no calendar boundary to key
    off here, so the debounce period is the window itself)."""
    since = (now - datetime.timedelta(seconds=window_seconds)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM quota_events"
        " WHERE model = ? AND window_seconds = ? AND level = 'warn' AND ts >= ? LIMIT 1",
        (model, window_seconds, since),
    ).fetchone()
    return row is not None


def _estimated_wait_seconds(now: datetime.datetime, earliest_ts, window_seconds: int) -> int:
    """Approximate seconds until the oldest counted request ages out of
    the window, i.e. the earliest moment spend can drop. Approximation,
    not exact time-to-under-limit (that depends on how many of the
    oldest requests must age out): good enough for the DoD's
    "approximate wait time" requirement."""
    if earliest_ts is None:
        return window_seconds
    earliest = datetime.datetime.fromisoformat(earliest_ts)
    age = (now - earliest).total_seconds()
    return max(0, round(window_seconds - age))


def check_budget(model: str) -> None:
    """Raise fastapi.HTTPException(429) when the daily budget is exhausted."""
    config = load_budgets()
    budget = daily_budget(config, model)
    if budget is None:
        return

    now = datetime.datetime.now()
    today = now.date().isoformat()
    conn = _connect()
    try:
        # The requests table may not exist before the first logged request.
        try:
            spent = spent_today(conn, model, today)
        except sqlite3.OperationalError:
            return

        if spent >= budget:
            _record_event(conn, model, "block", spent, budget, now.isoformat())
            from fastapi import HTTPException

            raise HTTPException(
                status_code=429,
                detail=(
                    f"Guard: daily budget exhausted for model '{model}':"
                    f" spent ${spent:.4f} of ${budget:.2f}. Refusing request."
                ),
            )

        if spent >= config["warn_ratio"] * budget and not _warned_today(conn, model, today):
            _record_event(conn, model, "warn", spent, budget, now.isoformat())
            print(
                f"Guard WARNING: model '{model}' at ${spent:.4f}"
                f" of ${budget:.2f} daily budget"
            )
    finally:
        conn.close()


def check_quota_windows(model: str) -> None:
    """Raise fastapi.HTTPException(429) when a sliding-window token quota
    (quota_windows in budgets.yaml) is exhausted for `model`. A model with
    no configured windows is a no-op, same convention as check_budget."""
    config = load_budgets()
    windows = quota_windows_for(config, model)
    if not windows:
        return

    now = datetime.datetime.now()
    conn = _connect()
    try:
        for window in windows:
            window_seconds = window["window_seconds"]
            limit = window["limit_tokens"]
            since = (now - datetime.timedelta(seconds=window_seconds)).isoformat()

            try:
                spent, earliest_ts = tokens_in_window(conn, model, since)
            except sqlite3.OperationalError:
                # requests table not created yet (no request logged so far).
                continue

            if spent >= limit:
                _record_quota_event(
                    conn, model, window_seconds, "block", spent, limit, now.isoformat()
                )
                wait_s = _estimated_wait_seconds(now, earliest_ts, window_seconds)
                from fastapi import HTTPException

                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Guard: sliding-window quota exhausted for model '{model}'"
                        f" ({window_seconds}s window): used {spent} of {limit} tokens."
                        f" Retry in ~{wait_s}s."
                    ),
                )

            if spent >= config["warn_ratio"] * limit and not _quota_warned_recently(
                conn, model, window_seconds, now
            ):
                _record_quota_event(
                    conn, model, window_seconds, "warn", spent, limit, now.isoformat()
                )
                print(
                    f"Guard WARNING: model '{model}' at {spent} of {limit} tokens"
                    f" in {window_seconds}s window"
                )
    finally:
        conn.close()


class Guard(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        model = data.get("model", "")
        check_budget(model)
        check_quota_windows(model)
        return data


guard_instance = Guard()
