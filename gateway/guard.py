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
Left as an open question for Lead, not adopted.

Projected-spend wall (external review, P0): the walls above only ever
looked at ALREADY-LOGGED spend, so one large request could cross a $
budget or a token quota mid-flight (nothing projects the CURRENT
request before the call). This adds a projection layer that runs
ONLY on the hook path, where the request payload (`data`:
messages/max_tokens) is available -- check_budget, check_quota_windows
and check_quota_pools all take an optional `data` parameter;
`data=None` is the pre-existing direct-call contract and reproduces
the old post-fact-only behavior byte-for-byte (see
test_data_none_matches_legacy_*_behavior in test_guard.py). When
`data` is given: `projected_tokens = prompt_estimate + output_allowance`.
`prompt_estimate` is ALWAYS computed (a single huge prompt gets caught
even with a stock config): via litellm.token_counter, falling back to
a crude len(str(msg))//4 heuristic per message on ANY exception
(unknown model, missing tokenizer, malformed messages, ...) -- an
estimation failure must never fail the actual call. `output_allowance`
is the request's own `max_tokens` if a positive int; otherwise
`projection.assumed_max_output_tokens` from budgets.yaml (an int, or a
{default, <alias>: N} map, alias wins over default); otherwise 0 --
NOT a conservative non-zero default. This means the output side of a
request is invisible to the token-quota projection unless the
operator opts in via `projection.assumed_max_output_tokens`; the gap
is intentional (a wrong non-zero default would have silently changed
existing walls' behavior on stock config, breaking byte-for-byte
compatibility) and is a known, documented hole -- set
`assumed_max_output_tokens` if your traffic uses large completions.
Same `data=None` gate for the $ wall: `projection.usd_prices` gives
explicit {prompt_per_1k, completion_per_1k} per alias; a priced alias
blocks at `spent + projected_usd >= budget` (budget_events row,
level="block_projected"); an alias with NO configured `projection`
section at all is completely unaffected (old behavior); once a
`projection` section exists but this alias has no price, an alias
listed in `projection.fail_closed_aliases` gets a 429 (refuses to
guess a price), any other unpriced alias gets a once-per-day
"warn_no_price" print+row instead -- never a silently invented price.
`quota_pools` (a top-level config key) group several aliases behind
one shared token wall (their combined spend, same window semantics)
for a provider key several aliases share; a pool wall check runs
AFTER the per-alias `quota_windows` check, per-alias blocks are
first. All new event levels ("block_projected", "warn_no_price",
"block_no_price", "warn_bad_assumed_output") are new VALUES of the
existing TEXT `level` column -- no schema/migration change
(budget_events, quota_events unchanged).

Known asymmetry: the projection's INPUT is only the request's message
TEXT (estimate_prompt_tokens above), but the wall's already-logged
`spent` sums `requests.prompt_tokens`, which litellm/the provider
report INCLUDING any cache-read/cache-creation input tokens
(sqlite_logger.py). On cache-heavy traffic this makes the projection
systematically UNDER-estimate what a comparable future request would
cost/consume relative to how the SAME request's own spend gets logged
post-fact -- the projection has no visibility into cache hits/writes
at pre-call time (they are only known after the provider responds).
The direction is the safe one: the wall can fire LATER than a
perfectly-informed projection would (under-blocking a corner case),
never earlier (never a false block on a request that would actually
fit) -- consistent with every other estimation shortfall in this
module (module invariant: a projection failure must never be more
aggressive than reality). Left as a documented gap, not fixed here:
closing it would require the provider's cache pricing/token split
before the call is even made, which the request payload does not
carry.
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
        return {"warn_ratio": 0.8, "daily_usd": {}, "quota_windows": {}, "quota_pools": {}}
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    config.setdefault("warn_ratio", 0.8)
    config.setdefault("daily_usd", {})
    config.setdefault("quota_windows", {})
    config.setdefault("quota_pools", {})
    # "projection" is deliberately NOT defaulted to {} here: its
    # ABSENCE vs. presence is itself the byte-for-byte compatibility
    # gate for the $ projection wall (see check_budget below) -- a
    # config that never mentions "projection" must never engage any
    # of the new $ price-lookup/fail-closed/warn-no-price machinery,
    # even though the request payload (data) is always available on
    # the hook path. config.get("projection") is None for such a
    # config; code below checks `is not None`, not truthiness, on
    # purpose.
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


def quota_pools_containing(config: dict, model: str) -> list:
    """[(pool_name, pool_dict), ...] for every quota_pools entry whose
    `aliases` list includes `model` (several aliases sharing one
    provider key share one physical quota ceiling). A model in no pool
    is a no-op, same "absent means no wall" convention as
    quota_windows_for."""
    return [
        (name, pool)
        for name, pool in config["quota_pools"].items()
        if model in pool.get("aliases", [])
    ]


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
    synthetic/judge/replay traffic burns the same physical quota as
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


def tokens_in_window_for_aliases(conn: sqlite3.Connection, aliases: list, since_iso: str):
    """Sibling of tokens_in_window above, aggregated over SEVERAL
    aliases at once (quota_pools: several gateway aliases can share one
    provider key's physical quota, so the pool wall must sum their
    combined spend, not check each alias in isolation). Same NULL-safe
    COALESCE and all-traffic-kinds semantics. An empty `aliases` list
    is a no-op (0, None), never a malformed "IN ()" SQL clause."""
    if not aliases:
        return 0, None
    placeholders = ",".join("?" for _ in aliases)
    row = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)), 0),"
        f" MIN(ts) FROM requests WHERE model IN ({placeholders}) AND ts >= ?",
        (*aliases, since_iso),
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
    oldest requests must age out): good enough for an approximate
    wait-time hint."""
    if earliest_ts is None:
        return window_seconds
    earliest = datetime.datetime.fromisoformat(earliest_ts)
    age = (now - earliest).total_seconds()
    return max(0, round(window_seconds - age))


# --- Projected-spend estimation (external review, P0) ---------------------


def estimate_prompt_tokens(model: str, messages) -> int:
    """Best-effort prompt token estimate for the CURRENT request. Tries
    litellm's real tokenizer first; ANY exception (unknown model,
    missing tokenizer dependency, malformed messages, a litellm version
    without this model's tokenizer, ...) falls back to a crude
    4-chars-per-token heuristic over the stringified messages -- coarse,
    but always available, and an estimation failure must never fail the
    actual call. `messages=None` (a request with no "messages" key) is
    treated as an empty list, same convention litellm itself uses."""
    messages = messages or []
    try:
        import litellm

        return litellm.token_counter(model=model, messages=messages)
    except Exception:
        return sum(len(str(m)) // 4 for m in messages)


def _coerce_int(value):
    """Config-supplied numeric value, coerced to int, or None if it is
    not ALREADY a native int/float (a quoted scalar in YAML, e.g.
    `assumed_max_output_tokens: "2048"`, parses as a Python str --
    deliberately rejected here even though Python's own int("2048")
    would happily succeed, because a quoted number in a numeric YAML
    field is almost always an operator typo that deserves a warning,
    not silent acceptance). `bool` is excluded (True/False are
    technically int in Python but never a meaningful token count). The
    caller treats None exactly like "absent" (0), the same fail-safe
    direction as every other estimation failure in this module: a
    malformed config value must never fail the actual call."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _raw_assumed_output_tokens(config: dict, model: str):
    """The UNCOERCED projection.assumed_max_output_tokens value that
    would apply to `model` (a plain scalar, or the alias/`default`
    entry of a {default, <alias>} map) -- None if nothing is
    configured for this alias at all (a real "absent", distinct from a
    present-but-malformed value)."""
    projection = config.get("projection") or {}
    assumed = projection.get("assumed_max_output_tokens")
    if assumed is None:
        return None
    if isinstance(assumed, dict):
        if model in assumed:
            return assumed[model]
        return assumed.get("default")
    return assumed


def assumed_output_tokens_malformed(config: dict, model: str) -> bool:
    """True when `model` has a CONFIGURED assumed_max_output_tokens
    value that fails int coercion (e.g. a quoted "2048") -- used to
    fire a once-per-day warning; an alias with nothing configured at
    all is NOT malformed, just absent (False)."""
    raw = _raw_assumed_output_tokens(config, model)
    if raw is None:
        return False
    return _coerce_int(raw) is None


def output_allowance(config: dict, model: str, data: dict) -> int:
    """Upper bound assumed for THIS request's completion tokens, used
    only by the projection wall. Priority: the request's own
    `max_tokens` (a positive int) > `projection.assumed_max_output_tokens`
    for this alias specifically > the same key's `default` entry > 0.
    `assumed_max_output_tokens` may be a plain number (applies to every
    alias alike) or a {default: N, <alias>: M} map (alias overrides
    default, same convention as daily_usd). The final fallback is 0, NOT
    a conservative non-zero guess (module docstring: a wrong default
    here would silently change existing walls' behavior on a stock
    config) -- an alias with no max_tokens and no configured allowance
    has its output side invisible to the token projection until the
    operator opts in. A configured value that fails int coercion (a
    quoted scalar in YAML) is likewise treated as absent (0), never a
    raised exception -- see assumed_output_tokens_malformed above for
    the once-per-day warning this triggers at the call sites."""
    data = data or {}
    max_tokens = data.get("max_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
        return max_tokens
    raw = _raw_assumed_output_tokens(config, model)
    if raw is None:
        return 0
    coerced = _coerce_int(raw)
    return coerced if coerced is not None else 0


def projected_tokens(config: dict, model: str, data: dict) -> int:
    """prompt_estimate + output_allowance for the CURRENT request --
    the quantity the token-quota projection wall adds to already-logged
    spend before deciding to block."""
    data = data or {}
    return estimate_prompt_tokens(model, data.get("messages")) + output_allowance(
        config, model, data
    )


def usd_price_for(config: dict, model: str):
    """{prompt_per_1k, completion_per_1k} for `model` from
    projection.usd_prices, or None if unpriced (never a guessed
    default -- module docstring)."""
    projection = config.get("projection") or {}
    prices = projection.get("usd_prices") or {}
    return prices.get(model)


def projected_usd(config: dict, model: str, data: dict, price: dict) -> float:
    """$ estimate for THIS request given an explicit price, using the
    same prompt_estimate/output_allowance projection as the token
    wall."""
    data = data or {}
    prompt_estimate = estimate_prompt_tokens(model, data.get("messages"))
    allowance = output_allowance(config, model, data)
    return (prompt_estimate / 1000.0) * price.get("prompt_per_1k", 0.0) + (
        allowance / 1000.0
    ) * price.get("completion_per_1k", 0.0)


def _no_price_warned_today(conn, model, today) -> bool:
    """Once-per-day debounce for the "warn_no_price" print, sibling of
    _warned_today above (new VALUE of the existing `level` TEXT column,
    no schema change)."""
    row = conn.execute(
        "SELECT 1 FROM budget_events"
        " WHERE model = ? AND level = 'warn_no_price' AND substr(ts, 1, 10) = ? LIMIT 1",
        (model, today),
    ).fetchone()
    return row is not None


def _bad_assumed_output_warned_today(conn, model, today) -> bool:
    """Once-per-day debounce for the "warn_bad_assumed_output" print,
    sibling of _no_price_warned_today above. Reuses budget_events
    (spent_usd/budget_usd recorded as 0.0 -- this warning is not a $
    figure, but the schema's NOT NULL columns need a value and 0.0 is
    an honest sentinel, not a guessed cost)."""
    row = conn.execute(
        "SELECT 1 FROM budget_events"
        " WHERE model = ? AND level = 'warn_bad_assumed_output'"
        " AND substr(ts, 1, 10) = ? LIMIT 1",
        (model, today),
    ).fetchone()
    return row is not None


def warn_if_assumed_output_malformed(conn, model: str, today: str, config: dict) -> None:
    """Fires the once-per-day "warn_bad_assumed_output" print+row when
    `model`'s projection.assumed_max_output_tokens fails int coercion --
    output_allowance() already fails safe (0) on its own, this only
    makes the misconfiguration VISIBLE instead of silently degrading
    the wall."""
    if not assumed_output_tokens_malformed(config, model):
        return
    if _bad_assumed_output_warned_today(conn, model, today):
        return
    _record_event(conn, model, "warn_bad_assumed_output", 0.0, 0.0, datetime.datetime.now().isoformat())
    print(
        f"Guard WARNING: projection.assumed_max_output_tokens for model"
        f" '{model}' is not a valid integer; treating it as unset (0)"
        f" for this request's output-side projection."
    )


def check_budget(model: str, data: dict = None) -> None:
    """Raise fastapi.HTTPException(429) when the daily budget is
    exhausted (post-fact, unchanged) OR when it is PROJECTED to be
    exhausted by the current request (external review, P0).

    `data` (the request payload: messages/max_tokens) is optional and
    defaults to None: `data=None` reproduces the pre-projection
    direct-call contract byte-for-byte -- no projection code below
    ever runs, only the original post-fact wall. Guard.async_pre_call_hook
    always passes the real payload; the projection layer only exists
    on that path, and even then is gated on `projection` actually
    being configured (module docstring)."""
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

        if data is not None:
            # Fire this BEFORE any projection math runs -- a malformed
            # assumed_max_output_tokens must be visible even on a $
            # wall that has no usd_prices entry at all
            # (output_allowance's own fail-safe already prevents a
            # crash; this only makes the misconfiguration audible).
            warn_if_assumed_output_malformed(conn, model, today, config)

            projection = config.get("projection")
            # Absent "projection" section entirely == this alias's $
            # wall never enters the new machinery (byte-for-byte
            # compatibility, see load_budgets/module docstring).
            if projection is not None:
                price = usd_price_for(config, model)
                if price is not None:
                    proj_usd = projected_usd(config, model, data, price)
                    if spent + proj_usd >= budget:
                        _record_event(
                            conn, model, "block_projected", spent, budget, now.isoformat()
                        )
                        from fastapi import HTTPException

                        raise HTTPException(
                            status_code=429,
                            detail=(
                                f"Guard: projected daily budget exhausted for model"
                                f" '{model}': spent ${spent:.4f} + projected"
                                f" ${proj_usd:.4f} of ${budget:.2f}. Refusing request."
                            ),
                        )
                else:
                    fail_closed = set(projection.get("fail_closed_aliases") or [])
                    if model in fail_closed:
                        # A fail-closed refusal must leave a trace in
                        # budget_events (level "block_no_price") --
                        # otherwise this branch raises with NO row at
                        # all, making unpriced fail-closed refusals
                        # invisible to any level-agnostic digest.
                        # spent/budget are real, known figures; no
                        # price is fabricated.
                        _record_event(
                            conn, model, "block_no_price", spent, budget, now.isoformat()
                        )
                        from fastapi import HTTPException

                        raise HTTPException(
                            status_code=429,
                            detail=(
                                f"Guard: $ projection unavailable for fail-closed"
                                f" alias '{model}' (no projection.usd_prices entry)."
                                f" Refusing request rather than guessing a price."
                            ),
                        )
                    elif not _no_price_warned_today(conn, model, today):
                        _record_event(
                            conn, model, "warn_no_price", spent, budget, now.isoformat()
                        )
                        print(
                            f"Guard WARNING: no projection.usd_prices entry for"
                            f" model '{model}'; cannot project $ spend for this"
                            f" request (daily budget ${budget:.2f})."
                        )

        if spent >= config["warn_ratio"] * budget and not _warned_today(conn, model, today):
            _record_event(conn, model, "warn", spent, budget, now.isoformat())
            print(
                f"Guard WARNING: model '{model}' at ${spent:.4f}"
                f" of ${budget:.2f} daily budget"
            )
    finally:
        conn.close()


def check_quota_windows(model: str, data: dict = None) -> None:
    """Raise fastapi.HTTPException(429) when a sliding-window token quota
    (quota_windows in budgets.yaml) is exhausted for `model` -- post-fact
    (unchanged) or PROJECTED to be exhausted by the current request
    (external review, P0). A model with no configured windows is a
    no-op, same convention as check_budget.

    `data` is optional and defaults to None: `data=None` reproduces the
    pre-projection direct-call contract byte-for-byte (post-fact wall
    only, no projection). Guard.async_pre_call_hook always passes the
    real payload; unlike the $ wall, the token projection here does NOT
    require a `projection` config section to engage -- prompt_estimate
    is always computed once `data` is given (the main P0 driver), only
    `output_allowance` defaults to 0 without config (module docstring).

    `projected_tokens` is computed ONCE per call, before the loop
    below -- an alias can carry several windows (e.g. a 60s TPM wall
    and an 86400s TPD wall), and recomputing it per window would run
    litellm.token_counter that many times on every single pre-call
    request for no benefit (the projection value does not depend on
    which window is being checked)."""
    config = load_budgets()
    windows = quota_windows_for(config, model)
    if not windows:
        return

    now = datetime.datetime.now()
    today = now.date().isoformat()
    conn = _connect()
    try:
        projected = None
        if data is not None:
            warn_if_assumed_output_malformed(conn, model, today, config)
            projected = projected_tokens(config, model, data)

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

            if projected is not None:
                if spent + projected >= limit:
                    _record_quota_event(
                        conn,
                        model,
                        window_seconds,
                        "block_projected",
                        spent,
                        limit,
                        now.isoformat(),
                    )
                    wait_s = _estimated_wait_seconds(now, earliest_ts, window_seconds)
                    from fastapi import HTTPException

                    raise HTTPException(
                        status_code=429,
                        detail=(
                            f"Guard: sliding-window quota would be exceeded for"
                            f" model '{model}' ({window_seconds}s window): spent"
                            f" {spent} + projected {projected} tokens (this request)"
                            f" of {limit}. Retry in ~{wait_s}s, or reduce the"
                            f" request size."
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


def check_quota_pools(model: str, data: dict = None) -> None:
    """Raise fastapi.HTTPException(429) when a shared quota_pools wall
    (external review, P0 recommendation: several aliases can share one
    provider key's physical quota ceiling) is exhausted -- post-fact
    (spend already over the wall, summed across ALL aliases in the
    pool) or PROJECTED to be exceeded by the current request. A model
    in no pool is a no-op, same convention as check_quota_windows.
    Runs AFTER the per-alias check_quota_windows in
    Guard.async_pre_call_hook: a per-alias wall blocks first.

    `data=None` (the direct-call contract, same as check_budget /
    check_quota_windows) skips the projection branch only -- the
    post-fact pool wall still applies, since quota_pools has no
    pre-projection legacy behavior to preserve either way.

    `projected_tokens` is computed ONCE per call, before the
    pools/windows loop below, for the same reason as
    check_quota_windows: it does not depend on which pool or window is
    being checked, so recomputing it per iteration would run
    litellm.token_counter redundantly on every pre-call request.

    A pool also gets a warn_ratio warning with the same once-per-window
    debounce as an alias's own quota_windows (Rule #1: cheap, and the
    class should behave the same way)."""
    config = load_budgets()
    pools = quota_pools_containing(config, model)
    if not pools:
        return

    now = datetime.datetime.now()
    today = now.date().isoformat()
    conn = _connect()
    try:
        projected = None
        if data is not None:
            warn_if_assumed_output_malformed(conn, model, today, config)
            projected = projected_tokens(config, model, data)

        for name, pool in pools:
            aliases = pool.get("aliases", [])
            pool_model = f"pool:{name}"
            for window in pool.get("windows", []):
                window_seconds = window["window_seconds"]
                limit = window["limit_tokens"]
                since = (now - datetime.timedelta(seconds=window_seconds)).isoformat()

                try:
                    spent, _earliest_ts = tokens_in_window_for_aliases(conn, aliases, since)
                except sqlite3.OperationalError:
                    continue

                if spent >= limit:
                    _record_quota_event(
                        conn, pool_model, window_seconds, "block", spent, limit, now.isoformat()
                    )
                    from fastapi import HTTPException

                    raise HTTPException(
                        status_code=429,
                        detail=(
                            f"Guard: pool '{name}' token quota exhausted"
                            f" ({window_seconds}s window): used {spent} of {limit}"
                            f" tokens across aliases {aliases}."
                        ),
                    )

                if projected is not None:
                    if spent + projected >= limit:
                        _record_quota_event(
                            conn,
                            pool_model,
                            window_seconds,
                            "block_projected",
                            spent,
                            limit,
                            now.isoformat(),
                        )
                        from fastapi import HTTPException

                        raise HTTPException(
                            status_code=429,
                            detail=(
                                f"Guard: pool '{name}' token quota would be"
                                f" exceeded ({window_seconds}s window): spent"
                                f" {spent} + projected {projected} tokens (this"
                                f" request) of {limit} tokens across aliases"
                                f" {aliases}."
                            ),
                        )

                if spent >= config["warn_ratio"] * limit and not _quota_warned_recently(
                    conn, pool_model, window_seconds, now
                ):
                    _record_quota_event(
                        conn, pool_model, window_seconds, "warn", spent, limit, now.isoformat()
                    )
                    print(
                        f"Guard WARNING: pool '{name}' at {spent} of {limit} tokens"
                        f" in {window_seconds}s window (aliases {aliases})"
                    )
    finally:
        conn.close()


class Guard(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        model = data.get("model", "")
        # Order: $ budget -> per-alias token windows -> shared pools; a
        # per-alias wall blocks before the pool-level one runs. `data`
        # is always passed here (never None) -- this is the ONLY call
        # site where the projection layer engages; direct calls (e.g.
        # from tests or scripts) that omit `data` get the post-fact-only
        # contract.
        check_budget(model, data)
        check_quota_windows(model, data)
        check_quota_pools(model, data)
        return data


guard_instance = Guard()
