"""Claude Code transcript telemetry (Delegated Task 5, D-0034).

Reads Claude Code session transcripts (~/.claude/projects/*/*.jsonl --
one file per session; the directory name encodes the project path),
normalizes each assistant API turn into a row in a NEW `cc_usage`
table in the gateway SQLite database (the existing `requests` table,
used by the API-contour gateway, is untouched), and prints a Ledger-
style usage report (tokens, accounted cost, cache economics) roughly
matching gateway/metrics.py's style.

PRIVACY (D-0034, unified plan section 5): this script reads only
`message.model` / `message.usage` and session/turn metadata. It never
reads message content, tool inputs/outputs, or any prompt text, and
writes none to the database or to reports.

Empirical findings (verified 2026-07-07 on this machine, see
CURRENT_CONTEXT.md "Delegated Task 5" for detail):

- One assistant API turn can appear as MULTIPLE JSONL lines sharing
  the same `requestId` (each with an identical `message.usage` block)
  -- observed when a single response contains several content blocks
  (e.g. multiple tool_use calls). `uuid` is unique per LINE, not per
  turn, so it is the wrong dedupe key: dedupe by `requestId` (first
  occurrence wins) or the API's true row identity would be inflated
  by a large factor (419 duplicate-requestId groups found in a single
  project's transcripts alone).
- `message.model == "<synthetic>"` rows are harness-internal
  rate-limit notices ("You've hit your session limit..."), always
  carrying all-zero usage. Skipped per spec.
- No `isSidechain: true` assistant rows exist anywhere among the
  TOP-LEVEL `<project>/*.jsonl` transcripts on this machine (0 of
  ~16k), but the column is populated regardless since subagent
  traffic is real traffic that must stay distinguishable (Lead
  clarification, item 2 of the spec). Sidechain traffic DOES exist,
  just not in that glob -- see Delegated Task 6 below.
- Non-assistant line `type`s observed: user, ai-title, last-prompt,
  queue-operation, system, mode, permission-mode, file-history-
  snapshot, pr-link, attachment. None carry a `usage` field; all are
  skipped (only `type == "assistant"` is read).

Delegated Task 6 (2026-07-07/08 follow-up): subagent/sidechain
transcripts live at a SECOND, deeper path --
`<project>/<session-id>/subagents/agent-*.jsonl` -- one file per
dispatched subagent, invisible to the original single glob above.
Re-verified empirically across all 61 such files on this machine
(3829 assistant lines total) before wiring in the second glob:
- every line's `sessionId` JSON field equals the PARENT session's
  UUID (the `<session-id>` directory name one level above
  `subagents/`), 0 mismatches -- so these files' turns correctly
  attach to their parent session, not a synthetic "subagents"
  session.
- `isSidechain` is true on all of them.
- `requestId` is present on all of them (0 missing; the uuid
  fallback below remains untested on real data but is kept for the
  same defensive reason as the top-level case).
- `agentId` is present on all of them. `promptId` was NOT found on
  any (0 of 3829) on this machine, despite being listed as an
  expected extra field in the spec -- noted here since it contradicts
  that assumption; harmless either way since neither field is read.
- No `dedupe_key` (session_id + requestId) collisions were found
  between a parent session's own turns and its subagents' turns, nor
  between sibling subagent files of the same session (checked across
  all 14 sessions on this machine that have a subagents/ directory).

Delegated Task 7 (2026-07-08 follow-up): agent attribution + haiku
pricing gaps closed. Re-verified empirically across all 62 subagent
files on this machine (6631 lines total, 3917 assistant lines) before
wiring in the two new columns:
- `agentId` is a per-line top-level field on EVERY line of a subagent
  file (assistant and non-assistant alike; 6631 of 6631) -- confirms
  the module docstring's earlier claim and gives a stable per-row
  agent identity key.
- `agentType` (the human-readable subagent slug, e.g.
  "test-maintainer") does NOT appear anywhere as a literal JSON key on
  any of the 6631 lines checked -- contradicts the Task 6 report's
  phrasing ("agentType: test-maintainer"), which turns out to have
  been describing a value, not a literal key. The literal key holding
  that exact value is `attributionAgent`, present as a TOP-LEVEL field
  on 3911 of 3917 assistant lines (the 6 missing are all
  `model == "<synthetic>"` harness stop-sequence lines, already
  skipped by SKIP_MODELS regardless). Every agentId maps to exactly
  one attributionAgent value across all 62 files (0 files with a
  varying value) -- confirmed identical to the *sidecar*
  `agent-<id>.meta.json` file's own `agentType` field (spot-checked:
  agent-aade8b2de22556abd's meta.json says
  `"agentType":"fix-verifier"`, and every assistant line in that same
  agent's .jsonl carries `"attributionAgent":"fix-verifier"`) -- so
  `attributionAgent` is the reliable, per-line, no-extra-file-read
  source used for the new `agent_type` column below. The .meta.json
  sidecars (one per subagent file, holding agentType/description/
  toolUseId/spawnDepth) are session-level metadata files, NOT matched
  by any existing transcript glob, and are intentionally left unread --
  `attributionAgent` already gives the same value per-line.
- Top-level (non-subagent) transcript lines carry neither `agentId`
  nor `attributionAgent`; both new columns are NULL there, as
  expected.
- Model `claude-haiku-4-5-20251001` was observed with `NULL`
  accounted_cost_usd (unpriced) in this machine's existing
  gateway/requests.db (7 rows, all NULL) before this task's pricing
  fix.
"""

import argparse
import glob
import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

SCHEMA = """
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

# agent_id / agent_type (Delegated Task 7): nullable, populated only for
# subagent/sidechain rows (see module docstring for the empirical
# source of each -- `agentId` and `attributionAgent` respectively,
# both top-level JSONL fields). NULL on every top-level/non-subagent
# row, which is the correct "not applicable" value, not a missing-data
# bug.

# Column set added by Delegated Task 7, for the ALTER TABLE migration
# in _connect() (existing databases predate these columns; SCHEMA
# above is CREATE TABLE IF NOT EXISTS so it never retrofits an
# already-existing table).
_TASK7_NEW_COLUMNS = ("agent_id", "agent_type")

# dedupe_key convention: session_id + ":" + requestId. Verified empirically
# 2026-07-07 that a single API turn can be split across multiple JSONL
# lines (distinct `uuid` per line, identical `message.usage`, shared
# `requestId`) -- requestId (scoped to its session; request IDs are not
# guaranteed globally unique across sessions) is the correct one-row-
# per-API-turn key, not uuid. See module docstring.

# Accounted API list prices, USD per token (D-0032 Rule #1, D-0034).
# Source: Anthropic pricing, as cached in the claude-api skill
# (SKILL.md "Current Models", cache date 2026-06-24) and cross-checked
# against gateway/config.yaml's own anthropic/claude-fable-5 and
# anthropic/claude-sonnet-5 aliases in this repo -- verified 2026-07-07.
# Cache write/read multipliers are the documented Anthropic-wide rule
# (shared/prompt-caching.md in the same skill): cache writes cost
# 1.25x base input price at the default 5-minute TTL (2x at 1-hour
# TTL; Claude Code's own TTL mix is not observable from the transcript
# fields we read, so we use the 5-minute/base rate -- see the
# "cache_write_multiplier" comment below); cache reads cost 0.1x base
# input price. Sonnet 5's introductory price ($2/$10 through
# 2026-08-31) is NOT used here -- we price at the standard list rate
# per Rule #1 ("list prices"), not a time-limited promotion; note this
# as a caveat in the report if it matters later.
#
# Unknown models get cost=None (Rule #1: never a silent $0). Do NOT
# add a model here without a verified source -- guessing is exactly
# what Rule #1 forbids.
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute TTL write premium
CACHE_READ_MULTIPLIER = 0.1

PRICES_PER_TOKEN_USD = {
    # model_id: (input_price, output_price)
    "claude-fable-5": (10.00 / 1_000_000, 50.00 / 1_000_000),
    "claude-opus-4-8": (5.00 / 1_000_000, 25.00 / 1_000_000),
    "claude-sonnet-5": (3.00 / 1_000_000, 15.00 / 1_000_000),
    "claude-sonnet-4-6": (3.00 / 1_000_000, 15.00 / 1_000_000),
    # Scout tier (Delegated Task 7, GAP 2): $1.00/$5.00 per 1M tokens,
    # API list price. Every other model above happens to appear in
    # real transcripts under its bare id (no date suffix); haiku is
    # the one exception observed on this machine --
    # "claude-haiku-4-5-20251001" (7 rows in gateway/requests.db,
    # verified 2026-07-08) -- so both the exact dated id AND the bare
    # id are keyed here, mapping to the same price tuple, in case a
    # future transcript ever reports the bare form instead.
    "claude-haiku-4-5-20251001": (1.00 / 1_000_000, 5.00 / 1_000_000),
    "claude-haiku-4-5": (1.00 / 1_000_000, 5.00 / 1_000_000),
}

SKIP_MODELS = {"<synthetic>"}


def transcript_glob(base_dir: Path = None) -> list:
    """Returns the default glob PATTERNS (plural -- a list, not a
    single string) for Claude Code transcripts on this machine:

    1. top-level session transcripts: <project>/<session>.jsonl
    2. subagent/sidechain transcripts, one directory layer deeper
       (Delegated Task 6): <project>/<session>/subagents/agent-*.jsonl

    The two layouts do not share a single glob pattern, hence the
    list. import_transcripts() accepts either this list or a single
    pattern string (the latter for CLI-override / backward-compat
    with existing callers/tests that pass one path)."""
    base = base_dir or (Path.home() / ".claude" / "projects")
    return [
        str(base / "*" / "*.jsonl"),
        str(base / "*" / "*" / "subagents" / "*.jsonl"),
    ]


def iter_assistant_turns(path: str):
    """Yields one dict per JSONL line with type == 'assistant', skipping
    every other line type (none of which carry a usage field) and
    <synthetic> rows (harness-internal, always zero usage).

    session_id: the JSON "sessionId" field is preferred when present
    (defensive / test-fixture friendly); the fallback depends on the
    transcript's directory layout (see below).

    project / fallback session_id derivation handles BOTH transcript
    layouts (Delegated Task 6):
    - top-level: <project>/<session>.jsonl -- verified empirically
      2026-07-07 that every real transcript's per-line "sessionId"
      always equals its own filename stem (0 mismatches), so the
      filename stem is both the project's session and the fallback.
    - subagent/sidechain: <project>/<session>/subagents/agent-*.jsonl
      -- the file's OWN stem is the sub-agent id (e.g. "agent-a6d8..."),
      not a session id, so it would be wrong as a session_id fallback;
      the real parent session id is the directory name one level above
      "subagents/". Detected by checking whether the immediate parent
      directory is literally named "subagents" (re-verified across 61
      real subagent files, 0 sessionId mismatches against this
      directory name -- see module docstring)."""
    p = Path(path)
    if p.parent.name == "subagents":
        project = p.parent.parent.parent.name
        filename_session_id = p.parent.parent.name
    else:
        project = p.parent.name
        filename_session_id = p.stem
    turn_index = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "assistant":
                continue
            session_id = obj.get("sessionId") or filename_session_id
            message = obj.get("message") or {}
            model = message.get("model")
            if model in SKIP_MODELS:
                continue
            usage = message.get("usage") or {}
            request_id = obj.get("requestId")
            if not request_id:
                # No requestId to dedupe on -- fall back to this line's own
                # uuid so the row is still captured (rare/defensive path;
                # not observed in practice on this machine).
                request_id = obj.get("uuid")
            yield {
                "ts": obj.get("timestamp"),
                "project": project,
                "session_id": session_id,
                "turn_index": turn_index,
                "model": model,
                "input_tokens": usage.get("input_tokens") or 0,
                "output_tokens": usage.get("output_tokens") or 0,
                "cache_creation_tokens": usage.get("cache_creation_input_tokens") or 0,
                "cache_read_tokens": usage.get("cache_read_input_tokens") or 0,
                "is_sidechain": 1 if obj.get("isSidechain") else 0,
                # agent_id / agent_type (Delegated Task 7, GAP 1): both
                # None on top-level (non-subagent) lines, which never
                # carry either field -- verified empirically, see
                # module docstring. `attributionAgent` (not a made-up
                # "agentType" key -- that literal key was NOT found
                # anywhere in real data) is the per-line field that
                # matches the subagent's sidecar meta.json `agentType`
                # value exactly.
                "agent_id": obj.get("agentId"),
                "agent_type": obj.get("attributionAgent"),
                "dedupe_key": f"{session_id}:{request_id}",
            }
            turn_index += 1


def accounted_cost(model: str, input_tokens: int, output_tokens: int,
                    cache_creation_tokens: int, cache_read_tokens: int):
    """Returns (cost_usd_or_None, warning_or_None). Never a silent $0
    for an unknown model (Rule #1)."""
    prices = PRICES_PER_TOKEN_USD.get(model)
    if prices is None:
        return None, f"WARNING: unknown model '{model}' -- accounted_cost_usd left as None (no price)"
    input_price, output_price = prices
    cost = (
        input_tokens * input_price
        + output_tokens * output_price
        + cache_creation_tokens * input_price * CACHE_WRITE_MULTIPLIER
        + cache_read_tokens * input_price * CACHE_READ_MULTIPLIER
    )
    return cost, None


def db_path() -> Path:
    return Path(os.environ.get("GATEWAY_DB_PATH", Path(__file__).parent.parent / "gateway" / "requests.db"))


def _connect(path: Path) -> sqlite3.Connection:
    """Opens the DB and ensures the schema is current.

    SCHEMA above is CREATE TABLE IF NOT EXISTS, so it only creates the
    table with the NEW columns on a fresh DB -- it never adds a column
    to a table that already exists from before Delegated Task 7. The
    ALTER TABLE loop below is the idempotent migration for EXISTING
    databases: it checks PRAGMA table_info first and only adds a
    column that is actually missing, so re-running this function
    (i.e. every normal import) is always a safe no-op once the columns
    exist."""
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(cc_usage)")}
    for col in _TASK7_NEW_COLUMNS:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE cc_usage ADD COLUMN {col} TEXT")
    conn.commit()
    return conn


def backfill_costs(conn: sqlite3.Connection) -> int:
    """Recomputes accounted_cost_usd for rows where it is currently
    NULL, using the CURRENT PRICES_PER_TOKEN_USD and each row's
    already-stored token counts (no transcript re-read needed).

    Exists so that adding a new model's price (e.g. haiku, Delegated
    Task 7 GAP 2) retroactively prices rows imported before that price
    existed, instead of leaving them permanently NULL. Idempotent: a
    row only qualifies while its cost is NULL, so a second call with
    no newly-priced models updates 0 rows. Returns the number of rows
    updated."""
    rows = conn.execute(
        """
        SELECT id, model, input_tokens, output_tokens,
               cache_creation_tokens, cache_read_tokens
        FROM cc_usage WHERE accounted_cost_usd IS NULL
        """
    ).fetchall()
    updated = 0
    for row_id, model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens in rows:
        cost, _warning = accounted_cost(
            model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens
        )
        if cost is not None:
            conn.execute(
                "UPDATE cc_usage SET accounted_cost_usd = ? WHERE id = ?",
                (cost, row_id),
            )
            updated += 1
    return updated


def import_transcripts(glob_pattern, db_file: Path):
    """Idempotent import: re-running over the same transcripts does not
    duplicate rows, enforced by the UNIQUE constraint on dedupe_key
    (INSERT OR IGNORE). Returns (rows_imported, sessions_seen, warnings).

    glob_pattern accepts either a single glob string (backward
    compatible with existing callers/tests and the CLI override) or a
    list/tuple of glob strings, as returned by transcript_glob()'s
    default (one pattern per transcript directory layout, Delegated
    Task 6). Paths matched by more than one pattern are processed only
    once.

    Backfill (Delegated Task 7): this function ALSO backfills rows
    that were already imported before agent_id/agent_type existed as
    columns, or before their model had a price. There is no separate
    --backfill flag -- backfilling runs automatically as part of every
    normal import, since both passes are naturally idempotent (a
    second run touches 0 rows once caught up):
    - agent fields: when INSERT OR IGNORE finds a dedupe_key already
      present (rowcount == 0) and this transcript line carries an
      agent_id/agent_type, an UPDATE ... WHERE agent_id IS NULL OR
      agent_type IS NULL fills the existing row's NULL column(s)
      without touching a row that already has them.
    - costs: backfill_costs() runs once at the end over the whole
      table, recomputing accounted_cost_usd for any row still NULL
      (covers rows whose model has since been added to
      PRICES_PER_TOKEN_USD, e.g. haiku, GAP 2)."""
    patterns = [glob_pattern] if isinstance(glob_pattern, str) else list(glob_pattern)
    warnings = []
    sessions_seen = set()
    rows_imported = 0
    conn = _connect(db_file)
    paths = set()
    for pattern in patterns:
        paths.update(glob.glob(pattern))
    try:
        for path in sorted(paths):
            for turn in iter_assistant_turns(path):
                sessions_seen.add(turn["session_id"])
                cost, warning = accounted_cost(
                    turn["model"], turn["input_tokens"], turn["output_tokens"],
                    turn["cache_creation_tokens"], turn["cache_read_tokens"],
                )
                if warning and warning not in warnings:
                    warnings.append(warning)
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO cc_usage
                        (ts, project, session_id, turn_index, model,
                         input_tokens, output_tokens, cache_creation_tokens,
                         cache_read_tokens, accounted_cost_usd, traffic_kind,
                         is_sidechain, agent_id, agent_type, dedupe_key)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'real', ?, ?, ?, ?)
                    """,
                    (
                        turn["ts"], turn["project"], turn["session_id"],
                        turn["turn_index"], turn["model"],
                        turn["input_tokens"], turn["output_tokens"],
                        turn["cache_creation_tokens"], turn["cache_read_tokens"],
                        cost, turn["is_sidechain"],
                        turn["agent_id"], turn["agent_type"], turn["dedupe_key"],
                    ),
                )
                rows_imported += cur.rowcount
                if cur.rowcount == 0 and (turn["agent_id"] is not None or turn["agent_type"] is not None):
                    # Row already existed (pre-Task-7 import, or a prior
                    # run of this same loop) -- backfill its agent
                    # fields if still NULL. COALESCE keeps whatever is
                    # already there, so this is a no-op once filled.
                    conn.execute(
                        """
                        UPDATE cc_usage
                        SET agent_id = COALESCE(agent_id, ?),
                            agent_type = COALESCE(agent_type, ?)
                        WHERE dedupe_key = ? AND (agent_id IS NULL OR agent_type IS NULL)
                        """,
                        (turn["agent_id"], turn["agent_type"], turn["dedupe_key"]),
                    )
        backfill_costs(conn)
        conn.commit()
    finally:
        conn.close()
    return rows_imported, sessions_seen, warnings


def build_report(db_file: Path, days: int) -> dict:
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    since = f"-{days} days"

    where_days = "substr(ts, 1, 10) >= date('now', ?)" if days is not None else "1=1"
    params = (since,) if days is not None else ()

    rows = conn.execute(
        f"SELECT * FROM cc_usage WHERE {where_days} ORDER BY ts", params
    ).fetchall()

    totals = {
        "rows": len(rows),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "accounted_cost_usd": 0.0,
        "unknown_cost_rows": 0,
    }
    per_day = defaultdict(lambda: _empty_bucket())
    per_model = defaultdict(lambda: _empty_bucket())
    per_project = defaultdict(lambda: _empty_bucket())
    per_session = defaultdict(lambda: _empty_bucket())
    sidechain_tokens = 0
    sidechain_cost = 0.0
    total_tokens_all = 0

    for r in rows:
        totals["input_tokens"] += r["input_tokens"]
        totals["output_tokens"] += r["output_tokens"]
        totals["cache_creation_tokens"] += r["cache_creation_tokens"]
        totals["cache_read_tokens"] += r["cache_read_tokens"]
        if r["accounted_cost_usd"] is None:
            totals["unknown_cost_rows"] += 1
        else:
            totals["accounted_cost_usd"] += r["accounted_cost_usd"]

        row_total_tokens = (
            r["input_tokens"] + r["output_tokens"]
            + r["cache_creation_tokens"] + r["cache_read_tokens"]
        )
        total_tokens_all += row_total_tokens
        if r["is_sidechain"]:
            sidechain_tokens += row_total_tokens
            sidechain_cost += r["accounted_cost_usd"] or 0.0

        day = r["ts"][:10] if r["ts"] else "unknown"
        _accumulate(per_day[day], r)
        _accumulate(per_model[r["model"]], r)
        _accumulate(per_project[r["project"]], r)
        _accumulate(per_session[(r["project"], r["session_id"])], r)

    cache_read_share_of_input = None
    total_input_side = (
        totals["input_tokens"] + totals["cache_creation_tokens"] + totals["cache_read_tokens"]
    )
    if total_input_side:
        cache_read_share_of_input = totals["cache_read_tokens"] / total_input_side

    # Accounted savings vs. what the same cache_read tokens would have
    # cost if sent as fresh (uncached) input tokens, per model.
    uncached_equivalent_cost = 0.0
    for r in rows:
        prices = PRICES_PER_TOKEN_USD.get(r["model"])
        if prices is None:
            continue
        input_price = prices[0]
        uncached_equivalent_cost += r["cache_read_tokens"] * input_price
    cache_accounted_savings_usd = None
    if uncached_equivalent_cost or totals["cache_read_tokens"]:
        actual_cache_read_cost = sum(
            r["cache_read_tokens"] * PRICES_PER_TOKEN_USD[r["model"]][0] * CACHE_READ_MULTIPLIER
            for r in rows if r["model"] in PRICES_PER_TOKEN_USD
        )
        cache_accounted_savings_usd = uncached_equivalent_cost - actual_cache_read_cost

    top_sessions = sorted(
        (
            {"project": proj, "session_id": sid, **bucket}
            for (proj, sid), bucket in per_session.items()
        ),
        key=lambda b: b["accounted_cost_usd"],
        reverse=True,
    )[:5]

    sidechain_share_of_tokens = (
        sidechain_tokens / total_tokens_all if total_tokens_all else None
    )

    return {
        "days": days,
        "totals": totals,
        "per_day": dict(sorted(per_day.items())),
        "per_model": dict(per_model),
        "per_project": dict(per_project),
        "top_sessions_by_cost": top_sessions,
        "cache_read_share_of_input": cache_read_share_of_input,
        "cache_accounted_savings_usd": cache_accounted_savings_usd,
        "sidechain_tokens": sidechain_tokens,
        "sidechain_accounted_cost_usd": sidechain_cost,
        "sidechain_share_of_tokens": sidechain_share_of_tokens,
    }


def _empty_bucket():
    return {
        "rows": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "accounted_cost_usd": 0.0,
    }


def _accumulate(bucket, row):
    bucket["rows"] += 1
    bucket["input_tokens"] += row["input_tokens"]
    bucket["output_tokens"] += row["output_tokens"]
    bucket["cache_creation_tokens"] += row["cache_creation_tokens"]
    bucket["cache_read_tokens"] += row["cache_read_tokens"]
    bucket["accounted_cost_usd"] += row["accounted_cost_usd"] or 0.0


def format_report(report: dict, warnings: list) -> str:
    lines = [f"CLAUDE CODE USAGE REPORT (last {report['days']} day(s))" if report["days"] is not None
             else "CLAUDE CODE USAGE REPORT (all time)", ""]

    t = report["totals"]
    lines.append("Totals:")
    lines.append(
        f"  {t['rows']} turns, {t['input_tokens']}+{t['output_tokens']} tok"
        f" (+{t['cache_creation_tokens']} cache-write, +{t['cache_read_tokens']} cache-read),"
        f" ${t['accounted_cost_usd']:.4f} accounted"
    )
    if t["unknown_cost_rows"]:
        lines.append(f"  {t['unknown_cost_rows']} turn(s) with unknown-model cost (excluded from the total above)")

    lines.append("")
    lines.append("Per day:")
    if not report["per_day"]:
        lines.append("  no turns")
    for day, b in report["per_day"].items():
        lines.append(
            f"  {day}: {b['rows']} turns, {b['input_tokens']}+{b['output_tokens']} tok, ${b['accounted_cost_usd']:.4f}"
        )

    lines.append("")
    lines.append("Per model:")
    for model, b in sorted(report["per_model"].items()):
        lines.append(
            f"  {model}: {b['rows']} turns, {b['input_tokens']}+{b['output_tokens']} tok"
            f" (+{b['cache_creation_tokens']} cache-write, +{b['cache_read_tokens']} cache-read),"
            f" ${b['accounted_cost_usd']:.4f}"
        )

    lines.append("")
    lines.append("Per project:")
    for project, b in sorted(report["per_project"].items()):
        lines.append(
            f"  {project}: {b['rows']} turns, {b['input_tokens']}+{b['output_tokens']} tok, ${b['accounted_cost_usd']:.4f}"
        )

    lines.append("")
    lines.append("Top 5 sessions by accounted cost:")
    if not report["top_sessions_by_cost"]:
        lines.append("  none")
    for s in report["top_sessions_by_cost"]:
        lines.append(
            f"  {s['project']} / {s['session_id']}: {s['rows']} turns, ${s['accounted_cost_usd']:.4f}"
        )

    lines.append("")
    lines.append("Cache economics:")
    share = report["cache_read_share_of_input"]
    lines.append(f"  cache_read share of input: {share:.1%}" if share is not None else "  cache_read share of input: n/a (no input tokens)")
    savings = report["cache_accounted_savings_usd"]
    lines.append(
        f"  accounted savings vs. uncached: ${savings:.4f}" if savings is not None
        else "  accounted savings vs. uncached: n/a"
    )

    lines.append("")
    lines.append("Sidechain (subagent) traffic:")
    lines.append(
        f"  {report['sidechain_tokens']} tok, ${report['sidechain_accounted_cost_usd']:.4f} accounted"
    )
    sc_share = report["sidechain_share_of_tokens"]
    lines.append(
        f"  share of total tokens: {sc_share:.1%}" if sc_share is not None else "  share of total tokens: n/a"
    )

    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in warnings:
            lines.append(f"  {w}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Claude Code transcript usage report")
    parser.add_argument(
        "--transcripts-glob",
        default=None,
        help=(
            "Override the transcript glob with a SINGLE custom pattern "
            "(default, with no override: TWO patterns are scanned -- "
            "~/.claude/projects/*/*.jsonl for session transcripts and "
            "~/.claude/projects/*/*/subagents/*.jsonl for subagent/"
            "sidechain transcripts, Delegated Task 6). Passing this flag "
            "replaces both default patterns with just this one."
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Gateway SQLite DB path (default: $GATEWAY_DB_PATH or gateway/requests.db)",
    )
    parser.add_argument("--days", type=int, default=7, help="Report window in days; use --all for no limit")
    parser.add_argument("--all", action="store_true", help="Report over all imported history, ignoring --days")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    db_file = Path(args.db) if args.db else db_path()
    pattern = args.transcripts_glob or transcript_glob()

    rows_imported, sessions_seen, warnings = import_transcripts(pattern, db_file)

    days = None if args.all else args.days
    report = build_report(db_file, days)
    report["import_summary"] = {
        "rows_imported_this_run": rows_imported,
        "sessions_seen_this_run": len(sessions_seen),
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report, warnings))
        print("")
        pattern_desc = pattern if isinstance(pattern, str) else " + ".join(pattern)
        print(
            f"(this run: {rows_imported} new row(s) imported from"
            f" {len(sessions_seen)} session file(s) matching {pattern_desc})"
        )


if __name__ == "__main__":
    main()
