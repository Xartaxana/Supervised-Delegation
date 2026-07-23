"""SessionStart hook: surfaces "reality in the background" -- a few
measured facts a fresh session shouldn't have to ask about before
trusting its own boot picture:

- MODEL: which tier is this session actually running on (a measured
  input for the in-session tier-check, not the session narrating its
  own model name).
- BOOT BUDGET: how big is the boot path right now, against WARN/BREACH
  thresholds, without waiting for a weekly calibration run or a manual
  byte count to notice a slow creep.
- OPEN DISPATCH: task_ids the routing journal still shows as
  outstanding. Class-defect this line guards against: a session wrote
  a `delegated` event to the routing log and never actually launched
  the worker -- a phantom open dispatch, the journal recording intent
  as fact (kin to the NOW line's anti-narrative-timestamp guard, but
  for task lifecycles instead of clocks). A task_id counts as OPEN iff
  its LAST lifecycle event (delegated/accepted/rejected/escalated/
  decomposable -- see _OPEN_LIFECYCLE_EVENTS) is `delegated`; anything
  else (dispatch_skipped, defect_found, lead_*, journal_created,
  calibrated) neither opens nor closes a task BY ITS OWN TYPE -- but its
  `notes` field is still scanned for a `closes:<task-id>` token (see
  _CLOSES_RE below), which DOES close a task regardless of the event's
  own type.

A SessionStart hook registered in .claude/settings.json is a
self-activating enforcement file: it was delivered under a sibling
filename and placed on this live path only at review/acceptance time,
not by whoever wrote it.

Ported from HQ 2026-07-20: adds the `closes:<task-id>` token scan
(previously, this hook read only event TYPES, so a plain-English
closing note in a later event's `notes` was invisible to it and
produced a false OPEN DISPATCH line for a task already closed out in
prose) and tightens the BOOT BUDGET breach line so it cannot be
misread as a self-authorizing command (see boot_budget_lines()).

Hard constraints (all load-bearing):
- NEVER breaks session start: any exception anywhere below collapses to
  ONE line, 'session-context warning: ...', and exit 0 (fail-open).
  main() is the single try/except boundary -- see its docstring for why
  a per-section try/except was deliberately NOT used. The
  open_dispatches()/open_dispatch_lines() functions follow the same
  rule: no local try/except, failures propagate to main()'s one
  boundary, exactly like quota_lines().
- Fast (<2s) and NO network at all (the NOW line's whole point is to
  guard against a narrative-future timestamp: read the system clock,
  not a narrated/inferred time).
- ASCII-safe output: some consoles run a non-UTF8 codepage. Every line
  built here is plain ASCII -- including the one line built from a
  NON-hardcoded source (MODEL from stdin), which goes through
  _ascii_sanitize (unsanitized stdin could break this invariant, inject
  lines past MAX_LINES, or crash print mid-flush). OPEN DISPATCH lines
  are built from journal-sourced task_id/agent/ts, also externally
  sourced (an agent field could in principle carry anything a session
  wrote into the journal) -- so each of those three values is routed
  through the same _ascii_sanitize helper before being formatted into
  a line.
- <=25 lines total (MAX_LINES) -- the OPEN DISPATCH addition can only
  ever add up to 4 lines (3 OPEN DISPATCH + 1 summary), and
  build_context_lines() still truncates to MAX_LINES at the end.
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

# N4 (carried forward from review): this import used to sit
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

# Events that open/close a dispatch's lifecycle. A task_id is OPEN iff
# its LAST such event is 'delegated'. Events outside this set
# (dispatch_skipped, defect_found, lead_*, journal_created, calibrated)
# neither open nor close a task BY THEIR OWN TYPE -- but see _CLOSES_RE
# below: their `notes` field is still scanned for closes: tokens.
_OPEN_LIFECYCLE_EVENTS = {"delegated", "accepted", "rejected", "escalated", "decomposable"}

# A bare `closes:<task-id>` token in ANY event's notes closes that
# task_id's open dispatch (CLAUDE.md's own convention for closing an
# open dispatch inside a later event's notes). The format is
# deliberately exact -- no whitespace after the colon, lowercase
# literal, the id must start with `t-` -- the same "bare token right
# after the colon" contract as `replaces_worker:` (a regex takes the
# first non-whitespace token, so loose punctuation right after the
# marker breaks the match by design).
#
# Left-anchored: an unanchored `closes:` substring would otherwise
# match INSIDE a longer word too -- `discloses:t-001` or
# `encloses:t-133` both contain the literal "closes:" and would
# silently close a task nobody meant to close (the dangerous
# direction: a false CLOSE hides a real phantom dispatch). `(?<!\w)`
# requires the character immediately before "closes:" to be either
# absent (start of string) or a non-word character -- so start-of-notes
# and punctuation/whitespace before the token are both legal, but a
# preceding letter/digit/underscore is not.
_CLOSES_RE = re.compile(r"(?<!\w)closes:(t-\d+)")


def _closes_task_ids(notes) -> list:
    """Extracts closes:t-NNN task ids from a notes field via findall.
    Returns [] for anything that is not a string (missing notes, or a
    malformed journal line where notes ended up a number/None in JSON)
    -- must never raise; open_dispatches() has no local try/except
    either, so this has to be safe on its own rather than relying on a
    boundary above it."""
    if not isinstance(notes, str):
        return []
    return _CLOSES_RE.findall(notes)


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


def open_dispatches(events: list) -> list:
    """A task_id is OPEN iff it has no `accepted` AND its LAST remaining
    event from _OPEN_LIFECYCLE_EVENTS is 'delegated' (a delegated with
    no closing event is a phantom open dispatch -- the class-defect
    that motivated this hook line: a session wrote 'delegated' and
    never launched the worker). Returns those last-delegated event
    dicts sorted by ts ascending (oldest first). Continuation
    dispatches (critic-gate entry) and retries stay open until a
    closing event. No local try/except -- failures propagate to
    main()'s single fail-open boundary, like quota_lines().

    Closure by `accepted` is JOURNAL LAW, not event ordering: reopen
    after accepted is forbidden (validator-enforced), so ANY accepted
    closes its task unconditionally -- regardless of where the line
    sits or what ts it carries. This is what survives two live journal
    anomalies in opposite directions -- a mid-file retro insertion
    where position lies, and a mistyped ts where ts lies; the
    accepted-law resolves both. No (ts, position) ordering rule can
    resolve both directions at once; the law does not need to. For
    tasks WITHOUT an accepted, 'last' is judged by (ts, file position):
    max ts wins, file position only breaks exact ts ties (retro pairs
    share one ts, and the closing line is written below the delegated
    one, so on a tie the later line wins).

    Ported from HQ 2026-07-20: a plain-English closing note in a later
    event's `notes` used to be invisible to this scan (it only ever
    read event TYPE), producing false OPEN DISPATCH lines for tasks
    already closed out in the journal's own prose. Fix: a bare
    `closes:t-NNN` token (see _CLOSES_RE) in ANY event's notes --
    lifecycle or not, e.g. `calibrated`, `dispatch_skipped` -- is a
    closing TOUCH of that task, keyed by the marker-carrying event's own
    (ts, file_idx). Per task_id, every touch is compared as
    (ts, idx, sub): a real lifecycle event contributes sub=0, a closes:
    marker contributes sub=1 at the SAME (ts, idx) as the event it sits
    in -- so at an exact tie the marker outranks the lifecycle event it
    came from. The task is OPEN iff its overall-latest touch is a real
    `delegated` event: a later marker closes it (even one sitting in an
    unrelated event's notes); a later `delegated` (retry/replacement)
    reopens it past an earlier marker; and -- documented as a
    deliberate contract, not a bug -- a closes:t-X token placed in t-X's
    OWN delegated event's notes closes that same event, because its
    marker-touch key ties the lifecycle key and the marker wins ties.
    `accepted` does not participate in this ts/idx comparison at all: it
    stays the unconditional law above, checked first and independent of
    any marker."""
    accepted_tids = set()
    lifecycle_last = {}  # tid -> (ts_str, file_idx, event_dict): last real lifecycle touch
    close_last = {}  # tid -> (ts_str, file_idx): last closes: marker touch
    for idx, e in enumerate(events):
        ts_key = (str(e.get("ts") or ""), idx)

        for closed_tid in _closes_task_ids(e.get("notes")):
            if closed_tid not in close_last or ts_key > close_last[closed_tid]:
                close_last[closed_tid] = ts_key

        event = e.get("event")
        if event not in _OPEN_LIFECYCLE_EVENTS:
            continue
        tid = e.get("task_id")
        if not tid:
            continue
        if event == "accepted":
            accepted_tids.add(tid)
            continue
        if tid not in lifecycle_last or ts_key > lifecycle_last[tid][:2]:
            lifecycle_last[tid] = (ts_key[0], ts_key[1], e)

    opens = []
    for tid, (ts, idx, e) in lifecycle_last.items():
        if tid in accepted_tids:
            continue
        if e.get("event") != "delegated":
            continue
        marker = close_last.get(tid)
        if marker is not None and marker >= (ts, idx):
            continue
        opens.append(e)
    opens.sort(key=lambda e: str(e.get("ts") or ""))
    return opens


def open_dispatch_lines(events: list) -> list:
    """Up to 3 'OPEN DISPATCH: t-NNN agent=X since <ts>' lines (oldest
    first) plus one summary line when more than 3 are open. task_id,
    agent and ts are journal-sourced -> each goes through
    _ascii_sanitize (non-UTF8-console invariant). Empty when nothing is
    open."""
    opens = open_dispatches(events)
    if not opens:
        return []
    lines = []
    for e in opens[:3]:
        tid = _ascii_sanitize(str(e.get("task_id") or "-"))
        agent = _ascii_sanitize(str(e.get("agent") or "-"))
        ts = _ascii_sanitize(str(e.get("ts") or "-"))
        lines.append(f"OPEN DISPATCH: {tid} agent={agent} since {ts}")
    if len(opens) > 3:
        lines.append(f"OPEN DISPATCHES: {len(opens)} total, {len(opens) - 3} more not shown")
    return lines


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
    24h request count.

    An EXISTING-but-unparseable config.yaml (corrupt YAML content, NOT
    absence -- preflight_quota.load_config() only guards absence, per
    its own docstring, and still lets yaml.YAMLError propagate on
    corrupt content) is caught HERE, locally, and reported as a single
    "quota: config unreadable (<reason>)" line instead of propagating
    uncaught to main()'s single fail-open boundary. This is a
    DELIBERATE, NARROW reversal of this file's general "half a context
    is worse than none" principle (see main()'s docstring) for JUST
    this one section: a session losing NOW/MODEL/LAST EVENT/BOOT
    BUDGET too, over a fault scoped entirely to the quota subsystem's
    own config file, is a strictly worse outcome than a full context
    with one line explicitly marked broken. Any OTHER, genuinely
    unforeseen failure below this point (e.g. an unreadable requests.db)
    still propagates unchanged to main()'s outer boundary -- this
    reversal was originally scoped to load_config() alone; a malformed
    budgets.yaml is a DIFFERENT (and now closed) case:
    preflight_quota.load_budgets() got its OWN internal parse-guard (see
    that function's docstring) -- it never raises on corrupt content in
    the first place, so there is nothing left here to catch for that
    path. This function surfaces load_budgets()'s honest "_parse_error"
    key (if present) as one additional "quota: budgets unreadable
    (<reason>)" line -- see the lines below load_config()'s try/except
    for that wiring; unlike the config.yaml case, a broken budgets.yaml
    does NOT blank the rest of quota_lines()'s output, because the
    failure is caught INSIDE load_budgets() itself, not by unwinding out
    of this function. ImportError/SyntaxError are deliberately
    RE-RAISED, not caught here (N4): those
    mean the quota subsystem ITSELF is unusable (missing yaml, a broken
    preflight_quota sibling module) -- a different failure class from
    "this config.yaml's own content is broken" -- and must still reach
    main()'s single fail-open boundary unchanged."""
    lines = []
    try:
        config = load_config(gateway_root)
    except (ImportError, SyntaxError):
        raise
    except Exception as e:
        # Single-line, ASCII-safe marker: yaml.YAMLError's own str() is
        # typically MULTI-LINE (a "problem" line plus a "in <file>, line
        # N, column N" context line) -- splitlines()[0] plus
        # _ascii_sanitize keep this section's failure honest without
        # letting it inject extra lines or non-ASCII bytes into the
        # console output (same invariant as MODEL/OPEN DISPATCH/WIRING).
        text = str(e).strip()
        reason = text.splitlines()[0] if text else type(e).__name__
        return [f"quota: config unreadable ({_ascii_sanitize(reason, 150)})"]
    budgets = load_budgets(gateway_root)
    mapping = alias_provider_models(config)

    # load_budgets() now guards budgets.yaml parsing internally (see its
    # own docstring in preflight_quota.py) and honestly returns
    # "_parse_error" instead of raising -- this caller HAS an output
    # line for the reason, so it shows it: the rest of this section
    # (per-alias QUOTA/REQUESTS lines, built from config, not budgets)
    # still prints normally alongside it -- unlike a broken config.yaml
    # (which blanks this whole function to one marker line), a broken
    # budgets.yaml does not, because the failure is contained INSIDE
    # load_budgets() rather than caught here.
    budgets_error = budgets.get("_parse_error")
    if budgets_error:
        lines.append(f"quota: budgets unreadable ({_ascii_sanitize(str(budgets_error), 150)})")

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
    source must stay ASCII/single-line before a non-UTF8 console".
    MODEL was this module's only externally-sourced input at the time
    that class was named; the OPEN DISPATCH lines are the second
    consumer -- task_id/agent/ts there are journal-sourced (a session
    could in principle write anything into those fields), so they route
    through this same helper rather than getting a parallel one."""
    s = str(s).strip()
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)  # control chars incl. \n \r \t
    s = s.encode("ascii", "replace").decode("ascii")
    return s[:max_len]


def model_line(stdin_payload=None) -> str:
    """F-37: the payload model is the harness's SessionStart
    DECLARATION, not a measurement -- it can be stale (observed live: a
    payload named a lower tier than the session actually ran on; the
    provider-side usage log was the ground truth). A present-but-stale
    id stated confidently is worse than an absent one, so the line
    carries the "declared by harness, not measured" marker. An
    in-hook measured cross-check is NOT implementable at SessionStart
    time: the session's own first request has not landed in the usage
    database yet, so the freshest rows there belong to a previous
    session -- a recorded limitation, not an oversight. The measured
    verification duty stays where it already lives: the tier-
    verification-at-entry check (in-session) and the weekly
    calibration's transcripts-vs-declarations check."""
    model_id = extract_model_id(stdin_payload)
    if not model_id:
        return "MODEL: not provided by hook input -- verify tier yourself (D-0056a)"
    sanitized = _ascii_sanitize(model_id)
    if not sanitized:
        # whitespace-only (or entirely-stripped) model id: same fallback
        # as "no model id at all" -- there is nothing left to report.
        return "MODEL: not provided by hook input -- verify tier yourself (D-0056a)"
    tier = model_tier(sanitized)
    return (
        f"MODEL: {sanitized} -> tier {tier}"
        " (declared by harness, not measured -- F-37; Lead tier = fable)"
    )


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
        # Informs the Boot Report's Next Required Action line; NOT an
        # auto-run command -- boot recovery is not work authorization by
        # itself (a breach line is a flag for the report, not a silent
        # trigger to start the diet before the operator has seen it).
        status_suffix = " BREACH -> boot-diet due (D-0068; report first, operator word starts it)"
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

    lines.extend(open_dispatch_lines(events))

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
