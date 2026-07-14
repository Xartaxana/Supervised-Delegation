"""Ledger: deterministic analytics over the gateway request log.

ARCHITECTURE.md, "Ledger"; D-0027. Pure Python/SQL, no LLM.

Produces a daily digest: requests, tokens, cost, latency and response
length per model per day; budget events; token-quota (sliding-window)
events; task categories (transparent keyword heuristics, always marked
as such); and the context-repetition ratio — the share of prompt
characters already sent in the previous request of the same model.
External priors to beat: 50-62% of spend is re-sent history
(docs/RELATED_WORK.md).

Usage:
    python metrics.py [--db PATH] [--days N] [--json]
"""

import argparse
import datetime
import json
import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

# Transparent, deterministic heuristics. Categories are estimates for
# the delegation table, not ground truth; the Analyst refines them.
CATEGORY_RULES = [
    ("coding", ("```", "def ", "class ", "function", "traceback", "compile")),
    ("summarization", ("summarize", "summary", "tl;dr", "shorten")),
    ("extraction", ("extract", "to json", "parse", "convert")),
    ("classification", ("classify", "categorize", "label", "tag")),
    ("formatting", ("format", "markdown", "table")),
]


def categorize(prompt_text: str) -> str:
    lowered = (prompt_text or "").lower()
    for category, needles in CATEGORY_RULES:
        if any(needle in lowered for needle in needles):
            return category
    return "other"


# Categories from CATEGORY_RULES/categorize() (and the stored `category`
# column) that DELEGATION_TABLE.md's four-state status vocabulary (D-0035)
# currently marks provisionally_validated or production_validated -- i.e.
# have a delegate whose suitability has actually been checked, as opposed
# to "estimated" (not yet checked) or "rejected" (checked and found
# harmful). Used by R2 readiness (ROADMAP.md "Money on the table"). No
# machine link to DELEGATION_TABLE.md; the drift detector is REGISTERED
# (rule 10(c)): a calibration check diffs this frozenset against the
# table's provisionally/production_validated rows -- statuses move ONLY
# at calibration (Update Rule 1), so any status move must update this set
# in the same commit.
#
# Starts empty: populate as your deployment validates categories at
# calibration (Update Rule 1); the calibration check mirrors this set
# against the delegation table.
VALIDATED_DELEGABLE_CATEGORIES = frozenset()


def common_prefix_len(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


def repetition_totals(rows):
    """rows: (model, prompt) ordered by ts. Returns (repeated, total): raw
    per-model character counts (repeated prefix chars, total prompt chars)
    over consecutive same-model pairs. repetition_by_model() turns this into
    ratios; phase2_readiness's C1 aggregates the raw counts across models
    into a single ratio (ROADMAP.md C1 names one ratio, not per-model)."""
    previous = {}
    repeated = defaultdict(int)
    total = defaultdict(int)
    for model, prompt in rows:
        if not prompt:
            continue
        if model in previous:
            repeated[model] += common_prefix_len(previous[model], prompt)
            total[model] += len(prompt)
        previous[model] = prompt
    return repeated, total


def repetition_by_model(rows) -> dict:
    """rows: (model, prompt) ordered by ts. Returns per-model ratio:
    repeated prompt chars / total prompt chars, over consecutive pairs."""
    repeated, total = repetition_totals(rows)
    return {
        model: round(repeated[model] / total[model], 4)
        for model in total
        if total[model]
    }


# --- Phase 2 readiness (Delegated Task 3, D-0025) ------------------------
#
# ROADMAP.md "Phase 2 -- Routing and Context Management Evaluation" gate
# criteria: G1-G2 (common), R1-R5 (Router), C1-C3 (Context management).
# Deterministic Python/SQL over requests.db (incl. its cc_usage table,
# Delegated Task 5) and docs/SHADOW_EVALUATION_LOG.md only -- no LLM calls
# (spec rule 1). Every entry is one of four vocabularies, never a guessed
# value (spec rule 2/3, Rule #1 spirit):
#   status="met" | "not_met"          -> entry also carries "detail"
#   status="not_computable_yet"       -> entry also carries "needs"
#   status="manual_check"             -> entry also carries "pointer"

_SHADOW_EVAL_LINE_RE = re.compile(
    r"^- (\d{4}-\d{2}-\d{2})\s+category=(\S+)\s+.*?n=(\d+)\s+.*?->\s*(\w+)"
    r"(\s*\[([^\]]+)\])?\s*$"
)

# Matches the section's own heading regardless of heading depth: the H1
# "# Shadow Evaluation Log" atop docs/SHADOW_EVALUATION_LOG.md. No match ->
# no section -> empty counts; see parse_shadow_eval_log's docstring for why
# this is not a whole-text fallback.
_SHADOW_EVAL_HEADER_RE = re.compile(
    r"^#{1,6}\s*Shadow Evaluation Log\s*$", re.MULTILINE
)


def parse_shadow_eval_log(text: str) -> dict:
    """Parses the Shadow Evaluation Log (docs/SHADOW_EVALUATION_LOG.md)
    into per-category {"pairs": n_sum, "runs": line_count}, counting only
    JUDGED pairs (R1's own wording): a line without "judge=" (early
    difflib-only evidence) is not judged evidence. [RETRACTED] lines are
    excluded (contaminated sample, per the log's own retraction note);
    [OVERRULED, ...] lines are NOT excluded -- that pair WAS judged, only
    the verdict was overridden by chief-judge review, which is still
    judged-evidence volume for R1's purpose.

    The heading must actually be found (_SHADOW_EVAL_HEADER_RE) before any
    line is parsed -- no match means no section, and parsing stops with an
    empty result. This is deliberate: a whole-text fallback when the
    heading isn't found would be hidden fragility -- it would happen to
    still work only as long as the log always carried the section
    somewhere in it, masking a real "heading missing/renamed" case."""
    match = _SHADOW_EVAL_HEADER_RE.search(text)
    section = text[match.start():] if match else ""
    counts = defaultdict(lambda: {"pairs": 0, "runs": 0})
    for line in section.splitlines():
        m = _SHADOW_EVAL_LINE_RE.match(line)
        if not m:
            continue
        if "judge=" not in line:
            continue
        _date, category, n, _verdict, _bracket, tag = m.groups()
        if tag and "RETRACT" in tag.upper():
            continue
        counts[category]["pairs"] += int(n)
        counts[category]["runs"] += 1
    return dict(counts)


def _max_consecutive_days(day_strs) -> int:
    """Given an iterable of 'YYYY-MM-DD' strings, returns the length of the
    longest run of calendar-consecutive days present in the set (gaps break
    the run; duplicates are deduped via the set())."""
    days = sorted(datetime.date.fromisoformat(d) for d in set(day_strs))
    if not days:
        return 0
    best = 1
    current = 1
    for prev, curr in zip(days, days[1:]):
        if (curr - prev).days == 1:
            current += 1
            best = max(best, current)
        else:
            current = 1
    return best


def _g1_readiness(conn: sqlite3.Connection, days: int) -> dict:
    since = f"-{days} days"
    req_days = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT substr(ts, 1, 10) FROM requests"
            " WHERE traffic_kind = 'real' AND substr(ts, 1, 10) >= date('now', ?)",
            (since,),
        ).fetchall()
    }
    cc_available = True
    try:
        cc_days = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT substr(ts, 1, 10) FROM cc_usage"
                " WHERE traffic_kind = 'real' AND substr(ts, 1, 10) >= date('now', ?)",
                (since,),
            ).fetchall()
        }
    except sqlite3.OperationalError:
        cc_days = set()
        cc_available = False

    combined = req_days | cc_days
    value = len(combined)
    max_consecutive_days = _max_consecutive_days(combined)
    status = "met" if max_consecutive_days >= 14 else "not_met"
    if cc_available:
        source_note = f"requests real={len(req_days)} + cc_usage real={len(cc_days)}, union"
    else:
        source_note = f"requests real={len(req_days)} only; cc_usage table absent in this DB"
    return {
        "status": status,
        "max_consecutive_days": max_consecutive_days,
        "detail": (
            f"{value} distinct real-traffic day(s) in the last {days} day(s)"
            f" ({source_note}); longest consecutive run ="
            f" {max_consecutive_days} day(s) vs threshold >=14 consecutive days"
        ),
    }


def _c1_readiness(conn: sqlite3.Connection, days: int) -> dict:
    since = f"-{days} days"
    rows = conn.execute(
        "SELECT model, prompt FROM requests WHERE traffic_kind = 'real'"
        " AND substr(ts, 1, 10) >= date('now', ?) ORDER BY ts",
        (since,),
    ).fetchall()
    repeated, total = repetition_totals(rows)
    total_chars = sum(total.values())
    if total_chars == 0:
        return {
            "status": "not_computable_yet",
            "needs": (
                f"real multi-turn traffic in requests (0 traffic_kind='real' rows"
                f" with a same-model predecessor in the last {days} day(s))"
            ),
        }
    ratio = sum(repeated.values()) / total_chars
    status = "met" if ratio >= 0.40 else "not_met"
    return {
        "status": status,
        "detail": (
            f"{ratio:.0%} context-repetition ratio on real traffic"
            " (requests, traffic_kind='real', all models combined)"
            " vs threshold >=40%"
        ),
    }


def _c2_readiness(conn: sqlite3.Connection, days: int) -> dict:
    since = f"-{days} days"
    try:
        (value,) = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT project, session_id, COUNT(*) AS turns
                FROM cc_usage
                WHERE traffic_kind = 'real' AND is_sidechain = 0
                  AND substr(ts, 1, 10) >= date('now', ?)
                GROUP BY project, session_id
                HAVING turns >= 5
            )
            """,
            (since,),
        ).fetchone()
    except sqlite3.OperationalError:
        return {
            "status": "not_computable_yet",
            "needs": (
                "the cc_usage table (tools/usage_report.py, Delegated Task 5,"
                " has not been run against this DB)"
            ),
        }
    status = "met" if value >= 20 else "not_met"
    return {
        "status": status,
        "detail": (
            f"{value} real session(s) with >=5 top-level turns in the last"
            f" {days} day(s) (cc_usage session_id, sidechain excluded)"
            " vs threshold >=20"
        ),
    }


def _r2_readiness(conn: sqlite3.Connection, days: int) -> dict:
    """ROADMAP.md R2 ("Money on the table"): validated-delegable categories'
    share of real-traffic spend, over the G1 window. Category attribution
    prefers the stored `category` column (ground truth); NULL rows fall
    back to categorize() the same way daily_digest's categories_heuristic
    does.

    RECORDED ASSUMPTION: the gate text says "share of the LEAD's accounted
    spend"; this sums ALL real rows without a model filter. Today the two
    are equivalent by construction -- real rows in requests.db are
    coordinator-tier interactive traffic (subagents are accounted in
    cc_usage, not here). If non-Lead 'real' traffic ever flows through the
    gateway (e.g. a built Router), this denominator silently widens: add a
    model filter then. The per-category spend printout keeps the mix
    visible to the calibrating Lead."""
    since = f"-{days} days"
    rows = conn.execute(
        "SELECT category, COALESCE(cost_usd, 0), prompt FROM requests"
        " WHERE traffic_kind = 'real' AND substr(ts, 1, 10) >= date('now', ?)",
        (since,),
    ).fetchall()
    if not rows:
        return {
            "status": "not_computable_yet",
            "needs": (
                f"categorized real traffic (0 traffic_kind='real' row(s)"
                f" in the last {days} day(s))"
            ),
        }
    cost_by_category = defaultdict(float)
    total_cost = 0.0
    for stored_category, cost, prompt in rows:
        category = stored_category or categorize(prompt)
        cost_by_category[category] += cost
        total_cost += cost
    if total_cost <= 0:
        return {
            "status": "not_computable_yet",
            "needs": (
                f"nonzero cost_usd on real rows to compute a spend share"
                f" ({len(rows)} real row(s) in the last {days} day(s),"
                " all zero-cost)"
            ),
        }
    validated_cost = sum(
        cost_by_category[c] for c in VALIDATED_DELEGABLE_CATEGORIES
        if c in cost_by_category
    )
    ratio = validated_cost / total_cost
    status = "met" if ratio >= 0.25 else "not_met"
    breakdown = ", ".join(
        f"{cat}=${cost_by_category[cat]:.4f}" for cat in sorted(cost_by_category)
    )
    validated_label = ", ".join(sorted(VALIDATED_DELEGABLE_CATEGORIES)) or "(none yet)"
    return {
        "status": status,
        "detail": (
            f"{ratio:.1%} of real-traffic spend (${total_cost:.4f} across"
            f" {len(rows)} row(s)) falls in validated-delegable categories"
            f" ({validated_label}, per DELEGATION_TABLE.md"
            " provisionally_validated rows); per-category spend:"
            f" {breakdown} vs threshold >=25%"
        ),
    }


def _c3_readiness(conn: sqlite3.Connection, days: int) -> dict:
    """ROADMAP.md C3: cache-aware truly-uncached share of real-traffic
    input-side tokens. prompt_tokens is INCLUSIVE of the cache columns
    (verified empirically): the truly-uncached paid portion of a row is
    prompt_tokens minus BOTH cache columns, not prompt_tokens alone (the
    old bug) and not prompt_tokens plus the cache columns (double-counts
    the other way -- see daily_digest's cache_read_share comment for the
    disjoint-columns cache-formula sibling in tools/usage_report.py, where
    cc_usage input_tokens is disjoint from the cache counters instead of
    inclusive of them; porting a formula across that pair must translate
    field semantics, not just copy arithmetic)."""
    since = f"-{days} days"
    rows = conn.execute(
        "SELECT COALESCE(prompt_tokens, 0), COALESCE(cache_read_input_tokens, 0),"
        " COALESCE(cache_creation_input_tokens, 0) FROM requests"
        " WHERE traffic_kind = 'real' AND substr(ts, 1, 10) >= date('now', ?)",
        (since,),
    ).fetchall()
    if not rows:
        return {
            "status": "not_computable_yet",
            "needs": (
                f"cache-column data on real traffic (0 traffic_kind='real'"
                f" row(s) in the last {days} day(s))"
            ),
        }
    total_input = sum(r[0] for r in rows)
    if total_input <= 0:
        return {
            "status": "not_computable_yet",
            "needs": (
                f"nonzero prompt_tokens on real rows to compute a cache"
                f" share ({len(rows)} real row(s) in the last {days} day(s),"
                " all zero prompt_tokens)"
            ),
        }
    uncached = sum(max(p - read - creation, 0) for p, read, creation in rows)
    cache_read_total = sum(r[1] for r in rows)
    cache_creation_total = sum(r[2] for r in rows)
    ratio = uncached / total_input
    status = "met" if ratio >= 0.25 else "not_met"
    return {
        "status": status,
        "detail": (
            f"{ratio:.2%} truly-uncached paid input on real traffic"
            f" ({uncached} of {total_input} input-side token(s) across"
            f" {len(rows)} row(s) in the last {days} day(s); cache_read="
            f"{cache_read_total}, cache_creation={cache_creation_total})"
            " vs threshold >=25%"
        ),
    }


def _r1_readiness(shadow_log_path) -> dict:
    try:
        text = Path(shadow_log_path).read_text(encoding="utf-8")
    except OSError:
        return {
            "status": "not_computable_yet",
            "needs": f"docs/SHADOW_EVALUATION_LOG.md (not found at {shadow_log_path})",
        }
    counts = parse_shadow_eval_log(text)
    if not counts:
        return {
            "status": "not_computable_yet",
            "needs": "judged Shadow Evaluation Log lines in docs/SHADOW_EVALUATION_LOG.md (none found)",
        }
    best_category, best = max(
        counts.items(), key=lambda kv: (kv[1]["pairs"], kv[1]["runs"])
    )
    met = best["pairs"] >= 30 and best["runs"] >= 2
    status = "met" if met else "not_met"
    breakdown = ", ".join(
        f"{cat}={c['pairs']}/{c['runs']}" for cat, c in sorted(counts.items())
    )
    return {
        "status": status,
        "detail": (
            f"best candidate {best_category}: {best['pairs']} judged pair(s)"
            f" across {best['runs']} run(s); all categories: {breakdown}"
            " vs threshold >=30 pairs across >=2 independent runs, per category"
        ),
    }


def phase2_readiness(conn: sqlite3.Connection, days: int, shadow_log_path=None) -> dict:
    """Builds the Phase 2 readiness section: one entry per ROADMAP.md gate
    criterion (G1, G2, R1-R5, C1-C3). See the module comment above for the
    four-status vocabulary. shadow_log_path defaults to
    docs/SHADOW_EVALUATION_LOG.md next to this repo's metrics.py (one level
    up from gateway/, then into docs/), so callers (including tests) can
    override it."""
    if shadow_log_path is None:
        shadow_log_path = Path(__file__).parent.parent / "docs" / "SHADOW_EVALUATION_LOG.md"

    return {
        "G1": _g1_readiness(conn, days),
        "G2": {
            "status": "manual_check",
            "pointer": (
                "PROCESS/JUDGE_CALIBRATION_PROTOCOL.md -- last recorded result"
                " (see CURRENT_CONTEXT.md)"
            ),
        },
        "R1": _r1_readiness(shadow_log_path),
        "R2": _r2_readiness(conn, days),
        "R3": {
            "status": "not_computable_yet",
            "needs": (
                "the same categorized real traffic as R2, split into the two"
                " halves of the G1 window"
            ),
        },
        "R4": {
            "status": "not_computable_yet",
            "needs": (
                "R1-R3 satisfied first, plus a projected router operating cost"
                " (no router built yet, D-0029)"
            ),
        },
        "R5": {
            "status": "manual_check",
            "pointer": (
                "ROADMAP.md Router gate R5 / CURRENT_CONTEXT.md Environment"
                " Notes"
            ),
        },
        "C1": _c1_readiness(conn, days),
        "C2": _c2_readiness(conn, days),
        "C3": _c3_readiness(conn, days),
    }


def format_phase2_line(criterion: str, entry: dict) -> str:
    status = entry["status"]
    if status == "met":
        return f"  {criterion}: {entry['detail']} -> met"
    if status == "not_met":
        return f"  {criterion}: {entry['detail']} -> not met"
    if status == "not_computable_yet":
        return f"  {criterion}: not computable yet (needs {entry['needs']})"
    if status == "manual_check":
        return f"  {criterion}: manual check ({entry['pointer']})"
    raise ValueError(f"unknown phase2_readiness status: {status!r}")


def daily_digest(conn: sqlite3.Connection, days: int, shadow_log_path=None) -> dict:
    since = f"-{days} days"
    per_day = conn.execute(
        """
        SELECT substr(ts, 1, 10) AS day, model,
               COUNT(*) AS requests,
               SUM(status = 'failure') AS failures,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               ROUND(AVG(latency_ms), 1) AS avg_latency_ms,
               ROUND(AVG(LENGTH(COALESCE(response, ''))), 1) AS avg_response_chars,
               COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_tokens
        FROM requests
        WHERE day >= date('now', ?)
        GROUP BY day, model ORDER BY day, model
        """,
        (since,),
    ).fetchall()

    # categories_heuristic: source is mixed -- stored category column takes
    # priority when non-NULL (ground-truth from regression set or any
    # tagged caller); categorize() is the fallback for untagged real
    # traffic. The key "categories_heuristic" is kept for downstream
    # compatibility.
    categories = defaultdict(lambda: {"requests": 0, "cost_usd": 0.0})
    prompts = conn.execute(
        "SELECT model, prompt, COALESCE(cost_usd, 0), category FROM requests"
        " WHERE substr(ts, 1, 10) >= date('now', ?) ORDER BY ts",
        (since,),
    ).fetchall()
    for _, prompt, cost, stored_category in prompts:
        bucket = categories[stored_category or categorize(prompt)]
        bucket["requests"] += 1
        bucket["cost_usd"] = round(bucket["cost_usd"] + cost, 6)

    repetition = repetition_by_model((model, prompt) for model, prompt, _, _cat in prompts)

    try:
        events = conn.execute(
            "SELECT substr(ts, 1, 10), model, level, spent_usd, budget_usd"
            " FROM budget_events WHERE substr(ts, 1, 10) >= date('now', ?)"
            " ORDER BY ts",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        events = []

    try:
        quota_events = conn.execute(
            "SELECT substr(ts, 1, 10), model, window_seconds, level,"
            " spent_tokens, limit_tokens FROM quota_events"
            " WHERE substr(ts, 1, 10) >= date('now', ?) ORDER BY ts",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        quota_events = []

    return {
        "days": days,
        "per_day": [
            {
                "day": r[0], "model": r[1], "requests": r[2], "failures": r[3],
                "prompt_tokens": r[4], "completion_tokens": r[5],
                "cost_usd": round(r[6], 6), "avg_latency_ms": r[7],
                "avg_response_chars": r[8],
                # Cache columns (NULL rows treated as 0; only Anthropic traffic populates these)
                "cache_read_tokens": r[9],
                "cache_creation_tokens": r[10],
                # Fraction of input-side tokens served from cache. FIELD
                # SEMANTICS (verified empirically on live rows): litellm
                # Usage.prompt_tokens for anthropic is the FULL input side
                # -- it already INCLUDES cache_read and cache_creation. So
                # the denominator is prompt_tokens ALONE; adding the cache
                # columns on top double-counts and understates the share.
                # This is the same METRIC as tools/usage_report.py
                # cache_read_share_of_input, but THERE cc_usage
                # input_tokens is DISJOINT from the cache counters, so that
                # sibling SUMS the three. Porting a formula across that
                # pair must translate field semantics, not just copy
                # arithmetic (the disjoint-columns cache-formula sibling in
                # tools/usage_report.py).
                "cache_read_share": (
                    round(r[9] / r[4], 4) if r[4] > 0 else 0.0
                ),
            }
            for r in per_day
        ],
        "categories_heuristic": dict(categories),
        "context_repetition_ratio": repetition,
        "budget_events": [
            {"day": e[0], "model": e[1], "level": e[2],
             "spent_usd": e[3], "budget_usd": e[4]}
            for e in events
        ],
        "quota_events": [
            {"day": e[0], "model": e[1], "window_seconds": e[2], "level": e[3],
             "spent_tokens": e[4], "limit_tokens": e[5]}
            for e in quota_events
        ],
        "phase2_readiness": phase2_readiness(conn, days, shadow_log_path),
    }


def format_digest(digest: dict) -> str:
    lines = [f"LEDGER DIGEST (last {digest['days']} day(s))", ""]

    lines.append("Per model per day:")
    if not digest["per_day"]:
        lines.append("  no requests")
    for r in digest["per_day"]:
        lines.append(
            f"  {r['day']}  {r['model']}: {r['requests']} req"
            f" ({r['failures']} failed), {r['prompt_tokens']}+{r['completion_tokens']} tok,"
            f" ${r['cost_usd']:.4f}, {r['avg_latency_ms']} ms avg,"
            f" {r['avg_response_chars']} chars avg answer"
        )
        lines.append(
            f"    cache: read={r['cache_read_tokens']} creation={r['cache_creation_tokens']}"
            f" cache_read_share={r['cache_read_share']:.1%}"
        )

    lines.append("")
    lines.append("Context repetition (share of prompt chars re-sent):")
    if not digest["context_repetition_ratio"]:
        lines.append("  not enough consecutive requests")
    for model, ratio in digest["context_repetition_ratio"].items():
        lines.append(f"  {model}: {ratio:.0%}")

    lines.append("")
    lines.append("Task categories (keyword heuristics, estimates):")
    for category, stats in sorted(digest["categories_heuristic"].items()):
        lines.append(
            f"  {category}: {stats['requests']} req, ${stats['cost_usd']:.4f}"
        )

    lines.append("")
    lines.append("Budget events:")
    if not digest["budget_events"]:
        lines.append("  none")
    for e in digest["budget_events"]:
        lines.append(
            f"  {e['day']}  {e['model']} {e['level'].upper()}:"
            f" ${e['spent_usd']:.4f} of ${e['budget_usd']:.2f}"
        )

    lines.append("")
    lines.append("Token quota events (sliding windows):")
    if not digest["quota_events"]:
        lines.append("  none")
    for e in digest["quota_events"]:
        lines.append(
            f"  {e['day']}  {e['model']} window={e['window_seconds']}s"
            f" {e['level'].upper()}: {e['spent_tokens']} of {e['limit_tokens']} tok"
        )

    lines.append("")
    lines.append('Phase 2 readiness (ROADMAP.md "Phase 2" gate; Delegated Task 3):')
    for criterion, entry in digest["phase2_readiness"].items():
        lines.append(format_phase2_line(criterion, entry))

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Ledger daily digest")
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "GATEWAY_DB_PATH", Path(__file__).parent / "requests.db"
        ),
    )
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--shadow-log",
        default=None,
        help=(
            "Path to docs/SHADOW_EVALUATION_LOG.md for the R1 criterion"
            " (default: docs/SHADOW_EVALUATION_LOG.md at the repo root, one"
            " level up from this file)"
        ),
    )
    args = parser.parse_args()

    if not Path(args.db).exists():
        raise SystemExit(f"request log not found: {args.db}")

    conn = sqlite3.connect(args.db)
    digest = daily_digest(conn, args.days, args.shadow_log)
    print(json.dumps(digest, indent=2) if args.json else format_digest(digest))


if __name__ == "__main__":
    main()
