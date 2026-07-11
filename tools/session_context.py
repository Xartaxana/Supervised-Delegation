"""SessionStart hook: surfaces "reality in the background" -- a few
measured facts a fresh session shouldn't have to ask about before
trusting its own boot picture:

- MODEL: which tier is this session actually running on (a measured
  input for the in-session tier-check, not the session narrating its
  own model name).
- BOOT BUDGET: how big is the boot path right now, against WARN/BREACH
  thresholds, without waiting for a weekly calibration run or a manual
  byte count to notice a slow creep.

A SessionStart hook registered in .claude/settings.json is a
self-activating enforcement file: it was delivered under a sibling
filename and placed on this live path only at review/acceptance time,
not by whoever wrote it.

Hard constraints (all load-bearing):
- NEVER breaks session start: any exception anywhere below collapses to
  ONE line, 'session-context warning: ...', and exit 0 (fail-open).
  main() is the single try/except boundary -- see its docstring for why
  a per-section try/except was deliberately NOT used.
- Fast (<2s) and NO network at all (the NOW line's whole point is to
  guard against a narrative-future timestamp: read the system clock,
  not a narrated/inferred time).
- ASCII-safe output: some consoles run a non-UTF8 codepage. Every line
  built here is plain ASCII -- including the one line built from a
  NON-hardcoded source (MODEL from stdin), which goes through
  _ascii_sanitize (unsanitized stdin could break this invariant, inject
  lines past MAX_LINES, or crash print mid-flush).
- <=25 lines total (MAX_LINES).
- Reading stdin must never block: only attempted when stdin is not a
  TTY (a manual `python tools/session_context.py` run from an
  interactive shell with nothing piped in must return instantly, not
  hang waiting for input that will never come).

Registered as the SessionStart hook via .claude/settings.json.
"""

import datetime
import json
import re
import sys
from pathlib import Path

# N4 (critic t-027, carried forward unchanged): this import used to sit
# unguarded at module level -- a failure here (no yaml installed, a
# syntax error in preflight_quota.py, any exception at all) happened
# DURING IMPORT of this module itself, before main()'s try/except
# boundary even exists yet, and escaped as a bare traceback -- exactly
# the "session start breaks" failure mode this whole hook exists to
# prevent (spec: fail-open is a hard constraint, not best-effort).
# Deferring the failure into a stub that raises only when CALLED means
# main()'s single try/except (see its docstring for why it is
# deliberately the ONE boundary) now also covers import-time failures
# of this dependency, not just runtime ones.
_IMPORT_ERROR = None
try:
    from preflight_quota import (
        alias_provider_models,
        load_budgets,
        load_config,
        parse_ts,
        usage_in_window,
    )
except Exception as _e:  # noqa: BLE001 -- deliberately broad, see comment above
    _IMPORT_ERROR = _e

    def _reraise_import_error(*_args, **_kwargs):
        raise _IMPORT_ERROR

    alias_provider_models = _reraise_import_error
    load_budgets = _reraise_import_error
    load_config = _reraise_import_error
    parse_ts = _reraise_import_error
    usage_in_window = _reraise_import_error

MAX_LINES = 25
QUOTA_WINDOW_SECONDS = 86400

# D-0068/D-0038 boot-budget thresholds (bytes).
BOOT_WARN_THRESHOLD = 90000
BOOT_BREACH_THRESHOLD = 100000
BOOT_BUDGET_LIMIT = 100000

_ALWAYS_INCLUDE_BOOT_FILE = "CLAUDE.md"

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# D-0056a tier mapping: substring of the model id (lowercased) -> tier
# label. Order matters only in that each id is expected to match at
# most one of these; first match wins.
_MODEL_TIER_SUBSTRINGS = (
    ("fable", "Lead(top)"),
    ("opus", "critic-tier"),
    ("sonnet", "builder-tier"),
    ("haiku", "scout-tier"),
)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def journal_path(root: Path) -> Path:
    return Path(root) / "logs" / "routing-log.jsonl"


def read_journal_events(root: Path) -> list:
    path = journal_path(root)
    if not path.exists():
        return []
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


def now_line(now: datetime.datetime = None) -> str:
    now = now or datetime.datetime.now()
    weekday = _WEEKDAYS[now.weekday()]
    return f"NOW: {now.strftime('%Y-%m-%d %H:%M:%S')} {weekday} (local system clock)"


def last_event_line(events: list) -> str:
    if not events:
        return "JOURNAL: empty or missing (logs/routing-log.jsonl)"
    e = events[-1]
    return (
        f"LAST EVENT: ts={e.get('ts')} event={e.get('event')}"
        f" agent={e.get('agent')} task_id={e.get('task_id') or '-'}"
    )


def open_degradation_window(events: list):
    """Scans the WHOLE journal (not just the tail): an unclosed
    lead_degraded can be arbitrarily far back if lead_restored never
    followed (D-0039 p.4: a safety-reset can leave the window open with
    no restore event ever written). Pairs each lead_degraded with the
    next lead_restored in journal order; returns the ts of the
    currently-open one, or None if the last pair closed."""
    open_since = None
    for e in events:
        event = e.get("event")
        if event == "lead_degraded":
            if open_since is None:
                open_since = e.get("ts")
        elif event == "lead_restored":
            open_since = None
    return open_since


def last_calibration_line(events: list, now: datetime.datetime = None) -> str:
    now = now or datetime.datetime.now()
    cal_events = [e for e in events if e.get("event") == "calibrated"]
    if not cal_events:
        return "Last calibration: NONE"
    ts = cal_events[-1].get("ts")
    try:
        days = (now - parse_ts(ts)).days
        return f"Last calibration: {ts} ({days} days ago)"
    except (ValueError, TypeError):
        return f"Last calibration: {ts} (age unknown -- unparsable ts)"


def gemini_aliases(config: dict) -> list:
    """Gateway aliases whose RAW litellm_params.model starts with
    'gemini/' -- Gemini free tier limits per-model requests/day, not
    tokens (spec: don't hardcode the limit, just report
    'requests last 24h: N')."""
    aliases = []
    for entry in config.get("model_list", []) or []:
        raw_model = (entry.get("litellm_params") or {}).get("model", "")
        if raw_model.startswith("gemini/"):
            name = entry.get("model_name")
            if name:
                aliases.append(name)
    return aliases


def quota_lines(gateway_root: Path, now: datetime.datetime = None) -> list:
    """One line per 86400s-window alias in budgets.yaml (used/limit +
    up to 3 nearest release moments), plus one line per Gemini alias's
    24h request count. A failure anywhere here is NOT swallowed locally
    -- it propagates to main()'s single fail-open boundary by design
    (see main() docstring)."""
    lines = []
    config = load_config(gateway_root)
    budgets = load_budgets(gateway_root)
    mapping = alias_provider_models(config)

    for alias, windows in (budgets.get("quota_windows") or {}).items():
        matching = [w for w in windows if w.get("window_seconds") == QUOTA_WINDOW_SECONDS]
        if not matching or alias not in mapping:
            continue
        limit = matching[0].get("limit_tokens")
        provider_model = mapping[alias]
        usage = usage_in_window(gateway_root, provider_model, QUOTA_WINDOW_SECONDS, now)
        releases = sorted(
            ts + datetime.timedelta(seconds=QUOTA_WINDOW_SECONDS) for ts, _tok in usage["rows"]
        )
        next_releases = [r.strftime("%H:%M") for r in releases[:3]]
        releases_str = ", ".join(next_releases) if next_releases else "none pending"
        lines.append(
            f"QUOTA {alias}: {usage['used_tokens']}/{limit} tok (24h);"
            f" next release(s): {releases_str}"
        )

    for alias in gemini_aliases(config):
        provider_model = mapping.get(alias)
        if not provider_model:
            continue
        usage = usage_in_window(gateway_root, provider_model, QUOTA_WINDOW_SECONDS, now)
        lines.append(f"REQUESTS {alias}: {len(usage['rows'])} last 24h")

    return lines


# ---------------------------------------------------------------------------
# New in b3: MODEL line (D-0056a)
# ---------------------------------------------------------------------------


def read_stdin_payload():
    """Reads and JSON-parses stdin, but ONLY when stdin is not a TTY.
    A SessionStart hook receives the harness's JSON on stdin; a human
    running this script by hand from an interactive shell has no piped
    input, and blocking on sys.stdin.read() there would hang forever --
    the isatty() guard is what keeps both modes safe. Any failure
    (unreadable stdin, empty input, invalid JSON) returns None rather
    than raising; callers treat None exactly like "no model info"."""
    if sys.stdin.isatty():
        return None
    try:
        data = sys.stdin.read()
    except Exception:
        return None
    if not data or not data.strip():
        return None
    try:
        return json.loads(data)
    except Exception:
        return None


def extract_model_id(payload):
    """Looks for the model id under, in order: top-level "model" as a
    string; top-level "model" as a dict with an "id" or "model" key;
    top-level "model_id". Returns None if none of these yield a
    non-empty string (covers missing stdin, non-dict payload, and
    payloads that simply don't carry a model at all)."""
    if not isinstance(payload, dict):
        return None

    model = payload.get("model")
    if isinstance(model, str) and model:
        return model
    if isinstance(model, dict):
        for key in ("id", "model"):
            value = model.get(key)
            if isinstance(value, str) and value:
                return value

    model_id = payload.get("model_id")
    if isinstance(model_id, str) and model_id:
        return model_id

    return None


def model_tier(model_id: str) -> str:
    low = model_id.lower()
    for substr, tier in _MODEL_TIER_SUBSTRINGS:
        if substr in low:
            return tier
    return "unknown"


def _ascii_sanitize(s: str, max_len: int = 80) -> str:
    """Fix for the class "an output line built from a NON-hardcoded
    source must stay ASCII/single-line before a cp1251 console" (critic
    t-043). MODEL is this module's only externally-sourced input; if a
    second consumer of externally-sourced text shows up, whether it
    shares this helper or gets its own SIBLING_MAP axis is a recorded
    Lead decision, not something to infer here."""
    s = str(s).strip()
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)  # control chars incl. \n \r \t
    s = s.encode("ascii", "replace").decode("ascii")
    return s[:max_len]


def model_line(stdin_payload=None) -> str:
    model_id = extract_model_id(stdin_payload)
    if not model_id:
        return "MODEL: not provided by hook input -- verify tier yourself (D-0056a)"
    sanitized = _ascii_sanitize(model_id)
    if not sanitized:
        # whitespace-only (or entirely-stripped) model id: same fallback
        # as "no model id at all" -- there is nothing left to report.
        return "MODEL: not provided by hook input -- verify tier yourself (D-0056a)"
    tier = model_tier(sanitized)
    return f"MODEL: {sanitized} -> tier {tier} (Lead tier = fable)"


# ---------------------------------------------------------------------------
# New in b3: BOOT BUDGET line(s) (D-0068/D-0038)
# ---------------------------------------------------------------------------


def boot_path_files(root: Path) -> list:
    """Parses BOOT.md's own "Read X.md" lines for the boot-path file
    list (BOOT.md stays the single owner of that list -- this hook only
    mirrors it for budget arithmetic, it does not maintain a second copy
    of the sequence), then always appends CLAUDE.md, which the harness
    auto-loads separately from the BOOT.md sequence (D-0041) but still
    counts against the same boot-budget bytes. Missing BOOT.md (or an
    unreadable one) yields just the always-included CLAUDE.md, not an
    exception -- callers still get a usable, if degraded, budget line."""
    boot_md = Path(root) / "BOOT.md"
    names = []
    try:
        text = boot_md.read_text(encoding="utf-8")
    except OSError:
        text = ""
    for m in re.finditer(r"Read ([A-Z_]+\.md)", text):
        name = m.group(1)
        if name not in names:
            names.append(name)
    if _ALWAYS_INCLUDE_BOOT_FILE not in names:
        names.append(_ALWAYS_INCLUDE_BOOT_FILE)
    return names


def boot_budget_lines(root: Path) -> list:
    """Sums the byte size of every boot-path file that exists (a
    missing file counts as 0 bytes toward the total, and is called out
    by name so the gap is visible rather than silently absorbed into a
    lower total). Emits one summary line always, plus a top-3-by-size
    breakdown (one line each, "  <bytes>  <file>") whenever the total
    crosses either the WARN (>90000) or BREACH (>100000) threshold from
    D-0068/D-0038."""
    root = Path(root)
    names = boot_path_files(root)

    sizes = []
    missing = []
    for name in names:
        try:
            size = (root / name).stat().st_size
        except OSError:
            size = 0
            missing.append(name)
        sizes.append((name, size))

    total = sum(size for _name, size in sizes)
    base = f"BOOT BUDGET: {total} bytes / {BOOT_BUDGET_LIMIT} ({len(names)} files)"
    missing_suffix = "".join(f" [missing: {name}]" for name in missing)

    if total > BOOT_BREACH_THRESHOLD:
        status_suffix = " BREACH -> run boot-diet skill"
    elif total > BOOT_WARN_THRESHOLD:
        status_suffix = " WARN"
    else:
        status_suffix = ""

    lines = [base + missing_suffix + status_suffix]

    if status_suffix:
        top3 = sorted(sizes, key=lambda t: t[1], reverse=True)[:3]
        for name, size in top3:
            lines.append(f"  {size}  {name}")

    return lines


def build_context_lines(
    root: Path = None,
    now: datetime.datetime = None,
    stdin_payload=None,
) -> list:
    root = Path(root) if root else repo_root()
    now = now or datetime.datetime.now()
    gateway_root = root / "gateway"

    events = read_journal_events(root)

    lines = [now_line(now), model_line(stdin_payload), last_event_line(events)]

    open_since = open_degradation_window(events)
    if open_since:
        lines.append(f"OPEN DEGRADATION WINDOW since {open_since}")

    lines.append(last_calibration_line(events, now))
    lines.extend(quota_lines(gateway_root, now))
    lines.extend(boot_budget_lines(root))

    return lines[:MAX_LINES]


def main(root: Path = None) -> int:
    """The ONE try/except boundary for the whole script (spec: NEVER
    crashes -> one line -> exit 0). Deliberately not per-section:
    a partially-built context (e.g. journal read fine, quota lookup
    half-crashed) is a worse failure mode than no context at all --
    a session trusting a half-populated 'reality' block is exactly the
    kind of silent gap this hook exists to prevent. So any error, from
    anywhere in reading stdin or build_context_lines(), discards
    everything gathered so far and prints only the warning line."""
    try:
        stdin_payload = read_stdin_payload()
        for line in build_context_lines(root, stdin_payload=stdin_payload):
            print(line)
    except Exception as e:  # fail-open: this hook must never break session start
        print(f"session-context warning: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
