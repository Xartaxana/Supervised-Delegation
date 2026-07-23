"""Measured GO/NO-GO preflight check for a sliding-window token quota
("code on irreversible paths" -- a quota-bound run must not be
launched on a guess; this script forces a measured number in front of
the launch decision).

Lesson learned from a live incident where a run was rejected against
a stale quota window: a provider's per-model quota is shared across
every gateway alias that resolves to that provider model (e.g. two
aliases bound to the same underlying model on the same provider have
their token burn add up on the provider's side even though they are
separate ledger lines here), AND across every gateway/*.db file, not
just the primary requests.db (a side DB written via GATEWAY_DB_PATH
carries real burn against the SAME provider quota and is invisible to
a query scoped to one db).

Grouping by "provider_model" (spec's own term) empirically means
something narrower than gateway/config.yaml's litellm_params.model
string: the value LiteLLM's callback actually logs into requests.db's
provider_model column has its provider prefix (the part before the
first "/", e.g. "groq", "gemini", "anthropic", "ollama_chat") stripped
off. Verified against a live gateway db: litellm_params.model
"groq/openai/gpt-oss-120b" is logged as provider_model
"openai/gpt-oss-120b"; "groq/llama-3.3-70b-versatile" as
"llama-3.3-70b-versatile"; "gemini/gemini-3.5-flash" as
"gemini-3.5-flash"; "anthropic/claude-3-5-sonnet" (the "mock" alias)
as "claude-3-5-sonnet". This is a real spec/reality discrepancy from the
spec text's "provider_model matches the provider_model of the given
--alias" reading of litellm_params.model verbatim -- normalize_provider_model()
below is the fix (strip the first path segment before comparing).

ts format (spec-flagged, also verified against live data): both
'2026-07-10T02:18:52.122060' (ISO, T separator) and a possible
'2026-07-10 02:18:52.122060' (space separator) form exist across this
project's databases. SQL string-range comparison on ts is NOT safe
across the two formats (ASCII ' ' < 'T', so a space-separated row and
a T-separated row at the identical instant do not compare equal/ordered
the way you'd expect) -- parse_ts() below normalizes both into a
datetime.datetime in Python before any window comparison, which is
Rule #1-safe (query a superset, then filter precisely in Python) at
the row counts this project's databases actually have.

KNOWN LIMIT (a documented review finding): the DB's provider_model
column has already lost the provider prefix at logging time, so this
tool cannot tell two different providers serving an identically-named
model apart (a hypothetical together/llama-3.3-70b would merge with
Groq's into one quota group -- wrong, quotas are per provider). Same
mechanics would merge any two aliases that happen to share an
underlying model tail (e.g. two roles both bound to the same local
model, or a role and the smoke-test "mock" alias bound to the same
provider model) -- harmless when neither side has a quota wall, but
check this note before adding a second provider for an existing model
tail. The data to fix it does not exist in the DB; fixing it means
logging the provider (sqlite_logger) first.

FOLLOW-UP FIXES (from a documented review, findings N3/N5):
- N3: go_at/release_schedule used to be computed from the local SQLite
  sum alone even when --probe found the provider's own Used number
  higher (verdict already trusted the provider number; the horizon did
  not). Now two horizons are reported when a probe reveals such a
  delta: "optimistic" (local corzinas only, old behavior, unchanged
  when there is no delta) and "conservative" (base = provider Used;
  the off-ledger delta is modeled as a synthetic row timestamped at
  probe-time, so it is assumed to age out only at probe-time+window --
  the last possible moment). An explicit RECONCILIATION line (provider
  Used, local sum, delta) is always printed alongside a probe 429 with
  delta > 0 (reconcile the ledger against the provider's own answer,
  don't just silently act on it).
- N5: usage_in_window() used to let a locked gateway/*.db
  (sqlite3.OperationalError: "database is locked") propagate as a bare
  traceback. It now raises QuotaDatabaseLockedError (naming the locked
  db); main() catches it, prints one clear line, and exits 2 -- loud
  failure, not a silently understated quota number. Sibling fix found
  while testing this (same class, not in the original finding's own
  line range): discover_dbs()'s schema-probe query hits the identical
  lock error and had the SAME silent-swallow bug in its bare 'except
  sqlite3.Error: continue' -- verified empirically that even a bare
  schema read blocks under another connection's BEGIN EXCLUSIVE, which
  would have dropped a locked db from the discovered list before
  usage_in_window's guard ever saw it. Both sites now re-raise
  specifically on "locked" and leave other sqlite3.Error types
  silently skipped, unchanged.
"""

import argparse
import datetime
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

DEFAULT_WINDOW_SECONDS = 86400


class QuotaDatabaseLockedError(RuntimeError):
    """Raised by usage_in_window() when a gateway/*.db file is locked
    (sqlite3.OperationalError: 'database is locked') -- N5 (a
    documented review finding): a locked db must fail LOUD (CLI exit
    2, one clear line naming the
    db) rather than have the caller's uncaught traceback stand in for
    an error message, and rather than silently dropping that db's
    usage out of the sum -- a quiet drop would understate real quota
    burn exactly like a silent $0 would (same class as the sibling
    'no silent $0' rule)."""

    def __init__(self, db_name: str):
        self.db_name = db_name
        super().__init__(f"database is locked: {db_name}")


def default_root() -> Path:
    """gateway/ directory: home of config.yaml, budgets.yaml, and every
    *.db this script sums over. Callers needing a different location
    (tests; a second deploy) pass root= explicitly to every function
    below instead of relying on this default."""
    return Path(__file__).resolve().parent.parent / "gateway"


def parse_ts(ts: str) -> datetime.datetime:
    """Parses a requests.db `ts` value in either observed format:
    ISO with 'T' ('2026-07-10T02:18:52.122060') or with a space
    ('2026-07-10 02:18:52.122060'). Raises ValueError on anything else
    (caller's job to decide whether to skip or propagate)."""
    s = ts.strip()
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    return datetime.datetime.fromisoformat(s)


def load_config(root: Path) -> dict:
    """A documented finding (class D-0043, alongside load_budgets right
    below): a missing config.yaml used to raise FileNotFoundError from
    the bare open() call. Harmless for this file's own CLI (main()
    below already wraps its call in a targeted try/except), but a real
    hole for any OTHER caller with no config.yaml-shaped fallback of
    its own -- namely session_context.py's quota_lines(), which by
    design has NO local try/except (it relies on main()'s single
    fail-open boundary): a missing config.yaml used to take down that
    SessionStart hook's ENTIRE context output (NOW/MODEL/JOURNAL/BOOT
    BUDGET/etc, not just the quota lines), because the uncaught
    exception propagated all the way to that hook's outermost handler,
    which discards everything gathered so far.

    Same exists-guard shape as load_budgets() right below: the absence
    of the file is a valid, expected state (a fresh subscription-
    contour checkout that never generated a gateway/config.yaml is
    this toolkit's own DEFAULT contour, not a misconfiguration) and
    gets an honest empty-dict default, not an exception.

    A config.yaml that EXISTS but is not valid YAML is a DIFFERENT
    failure class (corrupt content, not absence) and is deliberately
    NOT guarded HERE; yaml.safe_load's own exception (yaml.YAMLError)
    still propagates unchanged in that case. This function's own
    caller with no fallback of its own (tools/session_context.py's
    quota_lines()) catches that exception at ITS OWN boundary instead
    -- an external guard, not an internal one. load_budgets() right
    below used to share this exact asymmetry (guard existence only, not
    parseability) but no longer does: it now has an INTERNAL
    parse-guard, by deliberate choice -- the two functions are guarded
    at DIFFERENT layers now, not identically."""
    path = Path(root) / "config.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_budgets(root: Path) -> dict:
    """An EXISTING-but-unparseable budgets.yaml (corrupt YAML content,
    NOT absence -- absence already honestly degrades via the
    exists-guard above) no longer propagates yaml.safe_load's exception
    to the caller -- instead it returns the SAME default the missing-
    file branch already returns ({"quota_windows": {}}), PLUS an honest
    "_parse_error" key (string, the first line of the exception message
    -- multi-line yaml errors are truncated to one line for a caller
    that needs a single-line reason) -- a caller that cares about the
    reason (tools/session_context.py's quota_lines(), see its own
    docstring) can read and surface it; a caller that ignores it gets
    EXACTLY the same {"quota_windows": {...}} shape as before. This
    asymmetry with load_config() (which deliberately does NOT guard
    parsing, see its own docstring) is DELIBERATE: load_config() is
    guarded EXTERNALLY, in session_context.py itself; load_budgets() is
    guarded INTERNALLY, here, by explicit choice."""
    path = Path(root) / "budgets.yaml"
    if not path.exists():
        return {"quota_windows": {}}
    try:
        with open(path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except Exception as e:
        text = str(e).strip()
        reason = text.splitlines()[0] if text else type(e).__name__
        return {"quota_windows": {}, "_parse_error": reason}
    config.setdefault("quota_windows", {})
    return config


def normalize_provider_model(raw_model: str) -> str:
    """Strips the litellm provider prefix (the segment before the FIRST
    '/') from a litellm_params.model string, matching what actually
    lands in requests.db's provider_model column (see module
    docstring). A model string with no '/' at all is returned as-is."""
    if "/" in raw_model:
        _, rest = raw_model.split("/", 1)
        return rest
    return raw_model


def alias_provider_models(config: dict) -> dict:
    """{gateway alias: normalized provider_model} for every entry in
    config.yaml's model_list."""
    mapping = {}
    for entry in config.get("model_list", []) or []:
        name = entry.get("model_name")
        raw_model = (entry.get("litellm_params") or {}).get("model", "")
        if name:
            mapping[name] = normalize_provider_model(raw_model)
    return mapping


def resolve_target(config: dict, alias: str):
    """Returns (provider_model, group_aliases) where group_aliases is
    every alias (including `alias` itself) that shares the same
    normalized provider_model -- the set whose traffic sums against one
    provider quota (spec example: judge-groq + builder-groq on
    gpt-oss-120b). Raises KeyError if `alias` is not in config.yaml."""
    mapping = alias_provider_models(config)
    if alias not in mapping:
        raise KeyError(alias)
    target_pm = mapping[alias]
    group = {a for a, pm in mapping.items() if pm == target_pm}
    return target_pm, group


def discover_dbs(root: Path) -> list:
    """Every *.db directly under root that has a `requests` table
    (F-27: side DBs count too). Sorted for deterministic output.

    N5 sibling fix (found while testing the usage_in_window fix, not in
    the spec's own line range): this schema check is itself a read and
    hits the SAME sqlite3.OperationalError("database is locked") a
    locked db produces -- verified empirically that even the bare
    'SELECT 1 FROM sqlite_master' schema probe blocks under another
    connection's BEGIN EXCLUSIVE. The pre-existing bare
    'except sqlite3.Error: continue' below would have silently dropped
    a locked db from the discovered list BEFORE usage_in_window's own
    query ever ran on it, making that fix unreachable for a db locked
    for the whole discover+sum sequence (the realistic, testable case).
    Locked errors are re-raised (loud); any other sqlite3.Error (not a
    database, corrupt file, etc.) still just skips that file, unchanged
    from before."""
    dbs = []
    for f in sorted(Path(root).glob("*.db")):
        conn = None
        has_table = False
        try:
            conn = sqlite3.connect(f)
            has_table = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='requests'"
                ).fetchone()
                is not None
            )
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                raise QuotaDatabaseLockedError(f.name) from e
            continue
        except sqlite3.Error:
            continue
        finally:
            if conn is not None:
                conn.close()
        if has_table:
            dbs.append(f)
    return dbs


def usage_in_window(root: Path, provider_model: str, window_seconds: int,
                     now: datetime.datetime = None) -> dict:
    """Sums total_tokens for status='success' rows whose provider_model
    matches, across every db discover_dbs() finds, restricted (in
    Python, after parse_ts -- see module docstring) to ts >= since.

    Returns {"used_tokens": int, "since": datetime, "by_db": {name: tok},
    "rows": [(ts_datetime, tokens), ...] sorted by ts}."""
    now = now or datetime.datetime.now()
    since = now - datetime.timedelta(seconds=window_seconds)
    by_db = {}
    rows = []
    total = 0
    for db_file in discover_dbs(root):
        conn = sqlite3.connect(db_file)
        try:
            db_rows = conn.execute(
                "SELECT ts, total_tokens FROM requests"
                " WHERE status = 'success' AND provider_model = ?",
                (provider_model,),
            ).fetchall()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                raise QuotaDatabaseLockedError(db_file.name) from e
            raise
        finally:
            conn.close()
        db_total = 0
        for ts_raw, tok in db_rows:
            if not ts_raw:
                continue
            try:
                ts_dt = parse_ts(ts_raw)
            except ValueError:
                continue
            if ts_dt >= since:
                tok = tok or 0
                db_total += tok
                total += tok
                rows.append((ts_dt, tok))
        by_db[db_file.name] = db_total
    rows.sort(key=lambda r: r[0])
    return {"used_tokens": total, "since": since, "by_db": by_db, "rows": rows}


def resolve_limit(budgets: dict, alias: str, window_seconds: int,
                   override: int = None):
    """--limit-tokens wins outright; otherwise the budgets.yaml
    quota_windows[alias] entry whose window_seconds matches; else None
    (caller's cue to exit 2 -- "limit not measured and not given")."""
    if override is not None:
        return override
    for window in budgets.get("quota_windows", {}).get(alias, []):
        if window.get("window_seconds") == window_seconds:
            return window.get("limit_tokens")
    return None


def release_schedule(rows: list, limit: int, window_seconds: int, need: int,
                      now: datetime.datetime = None):
    """Hourly-bucketed forecast, for the next 24h, of how much of the
    currently-counted usage ages out of the sliding window (each row's
    tokens leave the window window_seconds after that row's own ts) and
    the resulting headroom at each hour boundary. Returns
    (schedule: list[dict], go_at: datetime|None) where go_at is the
    first hour boundary at which headroom >= need (None if no hour in
    the next 24h reaches it at current usage -- a real, reportable
    answer, not an error).

    Bucketed at 1h resolution and evaluated at the END of each hour, so
    go_at is a safe (rounded-up, never optimistic) estimate: real
    headroom can only be reached earlier within that hour, never
    later."""
    now = now or datetime.datetime.now()
    releases = sorted((ts + datetime.timedelta(seconds=window_seconds), tok) for ts, tok in rows)
    remaining = sum(tok for _, tok in rows)
    schedule = []
    go_at = None
    idx = 0
    for hour in range(1, 25):
        bucket_end = now + datetime.timedelta(hours=hour)
        released = 0
        while idx < len(releases) and releases[idx][0] <= bucket_end:
            released += releases[idx][1]
            remaining -= releases[idx][1]
            idx += 1
        headroom = limit - remaining
        schedule.append(
            {
                "hour_offset": hour,
                "bucket_end": bucket_end,
                "released_tokens": released,
                "remaining_used_tokens": remaining,
                "headroom_tokens": headroom,
            }
        )
        if go_at is None and headroom >= need:
            go_at = bucket_end
    return schedule, go_at


# Groq-style 429 body field extraction. The retry-duration group is
# anchored on the trailing seconds unit ("...NNs", optionally preceded
# by "NNh" and/or "NNm") so it stops at the duration itself and does
# not swallow the sentence-ending period that follows it in the
# provider's message:
#   short TPM wait: "...Limit 12000, Used 11862, Requested 1758. Please
#     try again in 3.1s. Need more tokens? ..." (verbatim, captured from
#     a live gateway db on a groq-bound alias)
#   long TPD wait: "...Limit 100000, Used 90614, Requested 17053, try
#     again in 1h50m" (notes from a live incident where a run was
#     rejected against a stale quota window -- paraphrased there, but
#     the Limit/Used/Requested field names and order match the
#     verbatim example above)
_LIMIT_RE = re.compile(r"\bLimit\s+(\d+)")
_USED_RE = re.compile(r"\bUsed\s+(\d+)")
_REQUESTED_RE = re.compile(r"\bRequested\s+(\d+)")
_RETRY_RE = re.compile(r"try again in\s+((?:\d+h)?(?:\d+m)?\d+(?:\.\d+)?s)", re.IGNORECASE)


def parse_provider_429(text: str):
    """Extracts the provider's own accounting from a Groq-style 429
    error body -- this is ground truth over our SQLite-summed usage
    (side DBs / off-proxy traffic are invisible to us but not to
    the provider). Returns None if the text has neither Limit nor Used
    (not a recognizable quota-wall message).

    Canonical verbatim example (captured from a live gateway db, a
    groq-bound alias, llama-3.3-70b-versatile TPM wall):
        'Rate limit reached for model `llama-3.3-70b-versatile` in '
        'organization `org_xxxxxxxxxxxxxxxxxxxxxxxx` service tier '
        '`on_demand` on tokens per minute (TPM): Limit 12000, Used '
        '11862, Requested 1758. Please try again in 3.1s. Need more '
        'tokens? Upgrade to Dev Tier today at '
        'https://console.groq.com/settings/billing'
    parse_provider_429(that) == {"limit": 12000, "used": 11862,
    "requested": 1758, "retry_after_text": "3.1s"}
    """
    limit_m = _LIMIT_RE.search(text)
    used_m = _USED_RE.search(text)
    if not limit_m or not used_m:
        return None
    requested_m = _REQUESTED_RE.search(text)
    retry_m = _RETRY_RE.search(text)
    return {
        "limit": int(limit_m.group(1)),
        "used": int(used_m.group(1)),
        "requested": int(requested_m.group(1)) if requested_m else None,
        "retry_after_text": retry_m.group(1) if retry_m else None,
    }


def probe(alias: str, proxy_url: str = "http://localhost:4000/v1/chat/completions",
          timeout: float = 15.0) -> dict:
    """Sends ONE minimal request through the live proxy (network -- only
    called from main() when --probe is passed; never called by tests).
    Tags the call traffic_kind=synthetic so it does not pollute 'real'
    accounting (D-0033 convention, sqlite_logger.py)."""
    body = {
        "model": alias,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "metadata": {"traffic_kind": "synthetic"},
    }
    req = urllib.request.Request(
        proxy_url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            usage = payload.get("usage") or {}
            return {"ok": True, "status": resp.status, "usage": usage}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        result = {"ok": False, "status": e.code, "raw_error": raw}
        if e.code == 429:
            parsed = parse_provider_429(raw)
            if parsed:
                result["provider_429"] = parsed
        return result
    except (urllib.error.URLError, OSError) as e:
        return {"ok": False, "status": None, "error": f"proxy unreachable: {e}"}


def _append_horizon(lines: list, label: str | None, go_at, schedule: list) -> None:
    """Renders one release-schedule horizon (N3: there can be two --
    optimistic from local corzinas only, conservative when a probe
    revealed off-ledger delta). label=None reproduces the pre-N3
    single-horizon text verbatim (no prefix) so the no-probe path is
    byte-for-byte unchanged."""
    prefix = f"[{label}] " if label else ""
    if go_at:
        lines.append(
            f"  {prefix}next possible GO (measured release schedule): ~{go_at}"
            " (first hour headroom >= need)"
        )
    else:
        lines.append(
            f"  {prefix}no hour in the next 24h reaches headroom >= need at current usage"
        )
    lines.append(f"  {prefix}release schedule (hours where tokens fall out of the window, next 24h):")
    any_release = False
    for s in schedule:
        if s["released_tokens"] > 0:
            any_release = True
            lines.append(
                f"    +{s['hour_offset']}h ({s['bucket_end']}):"
                f" -{s['released_tokens']} tok released, headroom={s['headroom_tokens']}"
            )
    if not any_release:
        lines.append(f"  {prefix}(no tokens fall out of the window within the next 24h at current usage)")


def format_text(report: dict) -> str:
    lines = [
        f"PREFLIGHT QUOTA CHECK -- alias={report['alias']}"
        f" provider_model={report['provider_model']}"
    ]
    if len(report["group_aliases"]) > 1:
        lines.append(
            f"  aliases sharing this provider quota: {', '.join(sorted(report['group_aliases']))}"
        )
    lines.append(
        f"  window: {report['window_seconds']}s, now={report['now']}, since={report['since']}"
    )
    lines.append(f"  limit: {report['limit_tokens']} tokens (measured source: {report['limit_source']})")
    lines.append(
        f"  used (measured, status=success, summed over {len(report['by_db'])} db(s) with a requests table):"
    )
    for db_name, tok in report["by_db"].items():
        lines.append(f"    {db_name}: {tok} tok")
    lines.append(f"  used total: {report['used_tokens']} tokens")
    lines.append(f"  headroom: {report['headroom_tokens']} tokens (limit - used)")
    lines.append(f"  need: {report['need_tokens']} tokens")

    probe_result = report.get("probe")
    recon = report.get("reconciliation")
    if probe_result:
        if probe_result.get("provider_429"):
            pp = probe_result["provider_429"]
            lines.append(
                f"  PROBE 429 (provider truth): Limit={pp['limit']} Used={pp['used']}"
                f" Requested={pp.get('requested')} retry_in={pp.get('retry_after_text')}"
            )
            if recon:
                # N3 / OpenClaw p.2: explicit ledger-vs-provider reconciliation
                # line, always printed when the probe found off-ledger delta --
                # not just implied by the (possibly bumped) used_tokens total.
                lines.append(
                    f"  RECONCILIATION: provider Used={recon['provider_used']} tok,"
                    f" local sum={recon['local_used']} tok, delta={recon['delta']} tok"
                    " (off-ledger traffic our db does not see; provider number"
                    " used for the verdict above, F-27)"
                )
            elif pp["used"] != report["local_used_tokens"]:
                lines.append(
                    f"  note: provider Used={pp['used']} differs from our measured"
                    f" {report['local_used_tokens']} (not greater -- verdict kept on our measurement)"
                )
        elif probe_result.get("ok"):
            lines.append(f"  PROBE OK: usage={probe_result.get('usage')}")
        else:
            lines.append(
                f"  PROBE FAILED: {probe_result.get('error') or probe_result.get('raw_error')}"
            )

    lines.append(f"VERDICT: {report['verdict']} (exit {0 if report['verdict'] == 'GO' else 1})")
    if report["verdict"] == "NO-GO":
        if recon:
            # N3: go_at from local corzinas alone is optimistic once probe
            # truth shows more is actually burned -- report BOTH horizons.
            _append_horizon(lines, "optimistic: local corzinas only", report["go_at"], report["schedule"])
            _append_horizon(
                lines, "conservative: off-ledger delta released at window end",
                report["go_at_conservative"], report["schedule_conservative"],
            )
        else:
            _append_horizon(lines, None, report["go_at"], report["schedule"])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Measured GO/NO-GO preflight check for a sliding-window token quota"
    )
    parser.add_argument("--alias", required=True, help="gateway alias (config.yaml model_name)")
    parser.add_argument("--need", type=int, required=True, help="tokens needed for the upcoming run")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument("--limit-tokens", type=int, default=None, dest="limit_tokens")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--root", default=None,
        help="Override the gateway/ root directory (default: <repo>/gateway; for testing)",
    )
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else default_root()

    try:
        config = load_config(root)
    except FileNotFoundError:
        print(f"error: config.yaml not found under {root}", file=sys.stderr)
        return 2
    budgets = load_budgets(root)

    try:
        provider_model, group_aliases = resolve_target(config, args.alias)
    except KeyError:
        print(f"error: unknown alias '{args.alias}' in {root / 'config.yaml'}", file=sys.stderr)
        return 2

    limit = resolve_limit(budgets, args.alias, args.window, args.limit_tokens)
    if limit is None:
        print(
            f"NO-GO: limit not measured and not given for alias '{args.alias}'"
            f" window={args.window}s -- pass --limit-tokens, or add a"
            f" budgets.yaml quota_windows['{args.alias}'] entry with"
            f" window_seconds={args.window}",
            file=sys.stderr,
        )
        return 2
    limit_source = "--limit-tokens" if args.limit_tokens is not None else "budgets.yaml"

    now = datetime.datetime.now()
    try:
        usage = usage_in_window(root, provider_model, args.window, now)
    except QuotaDatabaseLockedError as e:
        print(f"error: {e} -- usage cannot be measured, aborting (N5: loud, not a silently understated number)",
              file=sys.stderr)
        return 2
    local_used = usage["used_tokens"]
    used = local_used

    probe_result = None
    reconciliation = None
    schedule_conservative = None
    go_at_conservative = None
    if args.probe:
        probe_result = probe(args.alias)
        provider_429 = probe_result.get("provider_429") if probe_result else None
        if provider_429 and provider_429["used"] > used:
            delta = provider_429["used"] - used
            used = provider_429["used"]
            reconciliation = {
                "provider_used": provider_429["used"],
                "local_used": local_used,
                "delta": delta,
            }
            # N3: conservative horizon -- base remaining on provider Used
            # (not the local sum); known local corzinas still release on
            # their own ts-based schedule, and the off-ledger delta is
            # modeled as a synthetic row timestamped at probe-time (now),
            # i.e. it ages out at probe-time + window -- the last possible
            # moment, never earlier (release_schedule's own ts+window_seconds
            # formula does this for free once the row is added).
            augmented_rows = usage["rows"] + [(now, delta)]
            schedule_conservative, go_at_conservative = release_schedule(
                augmented_rows, limit, args.window, args.need, now
            )

    headroom = limit - used
    schedule, go_at = release_schedule(usage["rows"], limit, args.window, args.need, now)
    verdict = "GO" if headroom >= args.need else "NO-GO"

    report = {
        "alias": args.alias,
        "provider_model": provider_model,
        "group_aliases": sorted(group_aliases),
        "window_seconds": args.window,
        "limit_tokens": limit,
        "limit_source": limit_source,
        "used_tokens": used,
        "local_used_tokens": local_used,
        "headroom_tokens": headroom,
        "need_tokens": args.need,
        "verdict": verdict,
        "by_db": usage["by_db"],
        "since": usage["since"].isoformat(),
        "now": now.isoformat(),
        "schedule": [
            {**s, "bucket_end": s["bucket_end"].isoformat()} for s in schedule
        ],
        "go_at": go_at.isoformat() if go_at else None,
        "schedule_conservative": (
            [{**s, "bucket_end": s["bucket_end"].isoformat()} for s in schedule_conservative]
            if schedule_conservative is not None else None
        ),
        "go_at_conservative": go_at_conservative.isoformat() if go_at_conservative else None,
        "reconciliation": reconciliation,
        "probe": probe_result,
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        # format_text expects human-readable bucket_end/go_at; the report
        # dict above already carries isoformat strings for JSON, which
        # render fine as text too.
        print(format_text(report))

    return 0 if verdict == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
