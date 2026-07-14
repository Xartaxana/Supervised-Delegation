"""Counting script for the weekly calibration's field-completeness and
duplicate-task checks, plus the rejected/escalated pairing check. Takes
manual counting of journal counters off the reviewer's plate:
deterministically decidable work (counting, grouping, position in the
file) goes into code; VERDICTS (is this actually a violation, e.g. "the
thread was explicitly continued under a different task_id") stay with
the human reviewer. The script prints CANDIDATES, not verdicts.

Works with EITHER routing-journal formatting style:
  - compact JSON, no spaces after colons, task_id format t-NNN;
  - JSON WITH SPACES after colons, descriptive task_id strings (e.g.
    a slug like "at-bug-004").

Parsing is json.loads, line by line, ONLY (a lesson from an earlier
calibration run: grepping the spaced-JSON format gave a false empty
result because of the spaces after ':'; json.loads doesn't care about
whitespace). A line that fails to parse is a candidate violation in the
report, never a silent skip.

Exit code is always 0, except for IO/argument errors -- this script is
a measurement tool, not a gate (unlike tools/journal_validator.py,
which blocks the commit).

CLI:
    python tools/calibration_counts.py --journal PATH [--journal PATH ...]
        [--window-start ISO] [--window-end ISO] [--by-since ISO] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:  # keep output safe on Windows consoles with a non-UTF8 codepage
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

# Migration cutovers. A FRESH install enforces every typed field from
# day one -- leave both at the epoch. If you are adopting the policy on
# an EXISTING journal, set these to your own cutover moments: events
# earlier than the cutoff are read manually, append-only, and excluded
# from the field-completeness candidate count (see CLAUDE.md's "Routing
# log" section and the weekly calibration protocol).
DEFAULT_BY_SINCE = "1970-01-01T00:00:00"
LEGACY_CUTOFF = "1970-01-01T00:00:00"

MODEL_REQUIRED_EVENTS = {"delegated", "escalated", "accepted", "rejected"}
TASK_ID_REQUIRED_EVENTS = {"delegated", "accepted", "rejected", "escalated", "defect_found"}
FAILURE_CLASSES = {"spec", "capability", "recon", "tooling"}
LIFECYCLE_EVENTS = {"delegated", "accepted", "rejected", "escalated"}
ALWAYS_REQUIRED_FIELDS = ("agent", "category", "notes")


def parse_ts(ts: Optional[str]) -> Optional[datetime]:
    """Parses ts. Returns None if the field is missing/not a
    string/doesn't parse as ISO -- the caller decides what to do (a
    "no ts" candidate, not a crash)."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


class ParsedLine:
    __slots__ = ("line_no", "raw", "data", "parse_error", "ts")

    def __init__(self, line_no: int, raw: str, data: Optional[Dict[str, Any]],
                 parse_error: Optional[str] = None):
        self.line_no = line_no
        self.raw = raw
        self.data = data
        self.parse_error = parse_error
        self.ts = parse_ts(data.get("ts")) if data else None


def load_journal(path: str) -> List[ParsedLine]:
    lines: List[ParsedLine] = []
    with open(path, "r", encoding="utf-8") as fh:
        for i, raw in enumerate(fh, start=1):
            stripped = raw.rstrip("\n\r")
            if not stripped.strip():
                continue
            try:
                data = json.loads(stripped)
                if not isinstance(data, dict):
                    lines.append(ParsedLine(i, stripped, None, "not a JSON object (not a dict)"))
                    continue
            except json.JSONDecodeError as exc:
                lines.append(ParsedLine(i, stripped, None, f"invalid JSON: {exc}"))
                continue
            lines.append(ParsedLine(i, stripped, data))
    return lines


def _in_window(pl: ParsedLine, start: Optional[datetime], end: Optional[datetime]) -> bool:
    """An event with no parseable ts is not excluded by the window
    (there's no data to decide on) -- it counts as "in window";
    a missing ts is not itself checked separately by this script (ts
    is structurally required in the journal from the first line, and
    counting that separately was not part of this script's brief)."""
    if pl.ts is None:
        return True
    if start is not None and pl.ts < start:
        return False
    if end is not None and pl.ts >= end:
        return False
    return True


def analyze_journal(path: str, window_start: Optional[datetime], window_end: Optional[datetime],
                     by_since: datetime) -> Dict[str, Any]:
    all_lines = load_journal(path)
    unparsable = [
        {"line": pl.line_no, "error": pl.parse_error, "raw": pl.raw}
        for pl in all_lines if pl.data is None
    ]
    parsed_lines = [pl for pl in all_lines if pl.data is not None]
    in_window = [pl for pl in parsed_lines if _in_window(pl, window_start, window_end)]

    legacy_cutoff_dt = parse_ts(LEGACY_CUTOFF)

    # --- 1. Counts by event type and by tier (agent x event) ---
    by_event: Dict[str, int] = {}
    by_agent_event: Dict[str, Dict[str, int]] = {}
    for pl in in_window:
        ev = pl.data.get("event", "<none>")
        by_event[ev] = by_event.get(ev, 0) + 1
        agent = pl.data.get("agent")
        if isinstance(agent, str) and agent:
            by_agent_event.setdefault(agent, {})
            by_agent_event[agent][ev] = by_agent_event[agent].get(ev, 0) + 1

    # --- 2. Escalation-pairing check: rule-6 candidates ---
    # Group rejected by (task_id, agent) among events IN WINDOW;
    # >=2 rejected -> look for an escalated with the same task_id LATER
    # than the 2nd rejected BY POSITION IN THE FILE (among ALL parsed
    # lines, not just the window -- the escalation could sit outside
    # the window's boundary).
    rejected_groups: Dict[Tuple[str, str], List[int]] = {}
    for pl in in_window:
        if pl.data.get("event") != "rejected":
            continue
        tid = pl.data.get("task_id")
        agent = pl.data.get("agent")
        if not tid:
            continue
        key = (tid, agent if isinstance(agent, str) else "<none>")
        rejected_groups.setdefault(key, []).append(pl.line_no)

    escalated_by_task: Dict[str, List[int]] = {}
    for pl in parsed_lines:
        if pl.data.get("event") == "escalated":
            tid = pl.data.get("task_id")
            if tid:
                escalated_by_task.setdefault(tid, []).append(pl.line_no)

    rule6_candidates = []
    for (tid, agent), line_nos in rejected_groups.items():
        if len(line_nos) < 2:
            continue
        second_reject_line = sorted(line_nos)[1]
        esc_lines = [l for l in escalated_by_task.get(tid, []) if l > second_reject_line]
        if not esc_lines:
            rule6_candidates.append({
                "task_id": tid,
                "agent": agent,
                "rejected_lines": sorted(line_nos),
                "note": (
                    "candidate; no escalated with this task_id found after the 2nd "
                    "rejected (by file position). Could be closed/superseded (the "
                    "thread continued under a different task_id) -- a human verdict, "
                    "not something this script can decide."
                ),
            })

    # --- 3. Missing typed fields (typed-fields schema) ---
    field_violations = []
    legacy_events = []
    for pl in in_window:
        d = pl.data
        ev = d.get("event")
        tid = d.get("task_id")
        missing = []

        for f in ALWAYS_REQUIRED_FIELDS:
            v = d.get(f)
            if not isinstance(v, str) or not v.strip():
                missing.append(f)

        if ev in MODEL_REQUIRED_EVENTS:
            v = d.get("model")
            if not isinstance(v, str) or not v.strip():
                missing.append("model")

        if ev in TASK_ID_REQUIRED_EVENTS:
            if not isinstance(tid, str) or not tid.strip():
                missing.append("task_id")

        if ev == "rejected":
            fc = d.get("failure_class")
            if fc not in FAILURE_CLASSES:
                missing.append("failure_class")
            attempt = d.get("attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool):
                missing.append("attempt")

        if ev == "accepted" and d.get("agent") == "builder":
            w = d.get("witness")
            if not isinstance(w, str) or not w.strip():
                missing.append("witness")

        if ev == "defect_found":
            ref = d.get("ref")
            if not isinstance(ref, str) or not ref.strip():
                missing.append("ref")

        if not missing:
            continue

        entry = {"line": pl.line_no, "event": ev, "task_id": tid, "missing_fields": missing}
        if legacy_cutoff_dt is not None and pl.ts is not None and pl.ts < legacy_cutoff_dt:
            legacy_events.append(entry)
        else:
            field_violations.append(entry)

    # --- 4. Missing 'by' relative to --by-since ---
    by_violations = []
    for pl in in_window:
        ev = pl.data.get("event")
        if ev not in ("accepted", "rejected"):
            continue
        if pl.ts is None or pl.ts < by_since:
            continue  # before the validator was activated -- legal
        by_val = pl.data.get("by")
        if not isinstance(by_val, str) or not by_val.strip():
            by_violations.append({
                "line": pl.line_no, "event": ev, "task_id": pl.data.get("task_id"),
                "ts": pl.data.get("ts"),
            })

    # --- 5. task_id integrity: repeated delegated ---
    # Walk IN FILE ORDER (all distinct-parsed events, not just the
    # window, so the "last lifecycle status" doesn't lose history from
    # before the window) -- but only register a repeat in the report
    # when its own delegated line falls inside the window.
    last_status: Dict[str, str] = {}          # task_id -> last lifecycle event
    seen_delegated: Dict[str, bool] = {}      # task_id -> at least one delegated seen
    duplicate_delegates = []
    for pl in parsed_lines:
        d = pl.data
        ev = d.get("event")
        tid = d.get("task_id")
        if not isinstance(tid, str) or not tid:
            continue
        if ev == "delegated":
            agent = d.get("agent")
            attempt = d.get("attempt")
            has_attempt_ge2 = (isinstance(attempt, int) and not isinstance(attempt, bool)
                                and attempt >= 2)
            prior = last_status.get(tid)
            if seen_delegated.get(tid):
                # a repeated delegated on an already-seen task_id
                if agent == "critic":
                    branch = "critic-entry"
                elif prior == "accepted" and not has_attempt_ge2:
                    branch = "candidate-duplicate"
                elif prior == "rejected":
                    branch = "continuation"
                elif has_attempt_ge2:
                    branch = "retry"
                else:
                    branch = "other"
                if _in_window(pl, window_start, window_end):
                    duplicate_delegates.append({
                        "line": pl.line_no, "task_id": tid, "agent": agent,
                        "attempt": attempt, "prior_status": prior, "branch": branch,
                    })
            seen_delegated[tid] = True
            last_status[tid] = "delegated"
        elif ev in ("accepted", "rejected", "escalated"):
            last_status[tid] = ev
        # defect_found does not move the original task's lifecycle status
        # (it has its own task_id for the new finding; ref points back
        # to the original).

    # --- 6. ts monotonicity ---
    ts_anomalies = []
    prev_ts = None
    prev_line = None
    for pl in in_window:
        if pl.ts is None:
            continue
        if prev_ts is not None and pl.ts < prev_ts:
            ts_anomalies.append({
                "line": pl.line_no, "ts": pl.data.get("ts"),
                "prev_line": prev_line, "prev_ts": prev_ts.isoformat(),
            })
        prev_ts = pl.ts
        prev_line = pl.line_no

    # --- 7. False-accept rate by tier ---
    defect_by_agent: Dict[str, int] = {}
    accepted_by_agent: Dict[str, int] = {}
    for pl in in_window:
        ev = pl.data.get("event")
        agent = pl.data.get("agent")
        if not isinstance(agent, str) or not agent:
            continue
        if ev == "defect_found":
            defect_by_agent[agent] = defect_by_agent.get(agent, 0) + 1
        elif ev == "accepted":
            accepted_by_agent[agent] = accepted_by_agent.get(agent, 0) + 1
    false_accept = {}
    for agent in set(list(defect_by_agent) + list(accepted_by_agent)):
        d_count = defect_by_agent.get(agent, 0)
        a_count = accepted_by_agent.get(agent, 0)
        rate = (d_count / a_count) if a_count else None
        false_accept[agent] = {"defect_found": d_count, "accepted": a_count, "rate": rate}

    # --- 8. rejected distribution by failure_class x agent x model ---
    rejected_distribution: Dict[Tuple[str, str, str], int] = {}
    for pl in in_window:
        if pl.data.get("event") != "rejected":
            continue
        fc = pl.data.get("failure_class", "<none>")
        agent = pl.data.get("agent", "<none>")
        model = pl.data.get("model", "<none>")
        key = (fc, agent, model)
        rejected_distribution[key] = rejected_distribution.get(key, 0) + 1

    # --- 9. Degradation: lead_degraded/lead_restored pairs ---
    degradation_pairs = []
    open_degraded = None
    for pl in in_window:
        ev = pl.data.get("event")
        if ev == "lead_degraded":
            if open_degraded is not None:
                degradation_pairs.append({
                    "degraded_line": open_degraded["line"], "degraded_ts": open_degraded["ts"],
                    "restored_line": None, "restored_ts": None,
                    "note": "not closed by a following lead_degraded (another degraded before a restored)",
                })
            open_degraded = {"line": pl.line_no, "ts": pl.data.get("ts")}
        elif ev == "lead_restored":
            if open_degraded is not None:
                degradation_pairs.append({
                    "degraded_line": open_degraded["line"], "degraded_ts": open_degraded["ts"],
                    "restored_line": pl.line_no, "restored_ts": pl.data.get("ts"),
                    "note": "closed",
                })
                open_degraded = None
            else:
                degradation_pairs.append({
                    "degraded_line": None, "degraded_ts": None,
                    "restored_line": pl.line_no, "restored_ts": pl.data.get("ts"),
                    "note": "lead_restored with no preceding lead_degraded in window",
                })
    if open_degraded is not None:
        degradation_pairs.append({
            "degraded_line": open_degraded["line"], "degraded_ts": open_degraded["ts"],
            "restored_line": None, "restored_ts": None,
            "note": "NOT CLOSED by the end of the window/file -- legal if the session "
                    "is still alive / recorded as the last event",
        })

    # --- 10. Unclosed tasks ---
    unclosed_tasks = []
    for tid, status in last_status.items():
        if status == "delegated":
            unclosed_tasks.append(tid)
    unclosed_tasks.sort()

    return {
        "journal": path,
        "total_lines": len(all_lines),
        "parsed_lines": len(parsed_lines),
        "unparsable": unparsable,
        "in_window_count": len(in_window),
        "counts": {"by_event": by_event, "by_agent_event": by_agent_event},
        "rule6_candidates": rule6_candidates,
        "field_violations": field_violations,
        "legacy_events": legacy_events,
        "by_violations": by_violations,
        "duplicate_delegates": duplicate_delegates,
        "ts_anomalies": ts_anomalies,
        "false_accept": false_accept,
        "rejected_distribution": [
            {"failure_class": fc, "agent": a, "model": m, "count": c}
            for (fc, a, m), c in sorted(rejected_distribution.items())
        ],
        "degradation_pairs": degradation_pairs,
        "unclosed_tasks": unclosed_tasks,
    }


def _fmt_section(title: str) -> str:
    return f"\n=== {title} ===\n"


def render_text(report: Dict[str, Any]) -> str:
    out = []
    out.append(f"# Journal: {report['journal']}")
    out.append(f"Total lines: {report['total_lines']}; parsed: {report['parsed_lines']}; "
               f"in window: {report['in_window_count']}")

    out.append(_fmt_section("Unparsable lines (candidate violation)"))
    if report["unparsable"]:
        for u in report["unparsable"]:
            out.append(f"  line {u['line']}: {u['error']}")
    else:
        out.append("  (none)")

    out.append(_fmt_section("Counts by event type"))
    for ev, c in sorted(report["counts"]["by_event"].items()):
        out.append(f"  {ev}: {c}")

    out.append(_fmt_section("Counts by tier x event (agent x event)"))
    for agent, evs in sorted(report["counts"]["by_agent_event"].items()):
        out.append(f"  {agent}: " + ", ".join(f"{ev}={c}" for ev, c in sorted(evs.items())))

    out.append(_fmt_section("Escalation-pairing candidates (a rejected pair with no escalated)"))
    if report["rule6_candidates"]:
        for c in report["rule6_candidates"]:
            out.append(f"  task_id={c['task_id']} agent={c['agent']} "
                       f"rejected_lines={c['rejected_lines']} -- {c['note']}")
    else:
        out.append("  (no candidates)")

    out.append(_fmt_section("Missing typed fields (candidate violation, post-legacy)"))
    if report["field_violations"]:
        for v in report["field_violations"]:
            out.append(f"  line {v['line']} event={v['event']} task_id={v['task_id']}: "
                       f"missing {v['missing_fields']}")
    else:
        out.append("  (none)")

    out.append(_fmt_section(f"Legacy (ts < {LEGACY_CUTOFF}, not violations)"))
    out.append(f"  {len(report['legacy_events'])} event(s) with missing fields, pre-dating the typed-fields schema")

    out.append(_fmt_section("Missing 'by' (post by-since)"))
    if report["by_violations"]:
        for v in report["by_violations"]:
            out.append(f"  line {v['line']} event={v['event']} task_id={v['task_id']} ts={v['ts']}")
    else:
        out.append("  (none)")

    out.append(_fmt_section("Repeated delegated by task_id (branch classification)"))
    if report["duplicate_delegates"]:
        for v in report["duplicate_delegates"]:
            note = (" (anomalous repeat outside the canonical branches -- needs a human verdict)"
                    if v["branch"] == "other" else "")
            out.append(f"  line {v['line']} task_id={v['task_id']} agent={v['agent']} "
                       f"attempt={v['attempt']} prior_status={v['prior_status']} "
                       f"-> {v['branch']}{note}")
    else:
        out.append("  (no repeats)")

    out.append(_fmt_section("ts anomalies (informational, known non-monotonic-clock classes)"))
    if report["ts_anomalies"]:
        for a in report["ts_anomalies"]:
            out.append(f"  line {a['line']} ts={a['ts']} < line {a['prev_line']} "
                       f"prev_ts={a['prev_ts']}")
    else:
        out.append("  (none)")

    out.append(_fmt_section("False-accept rate by tier"))
    for agent, fa in sorted(report["false_accept"].items()):
        rate_s = f"{fa['rate']:.4f}" if fa["rate"] is not None else "n/a (accepted=0)"
        out.append(f"  {agent}: defect_found={fa['defect_found']} / accepted={fa['accepted']} "
                   f"= {rate_s}")

    out.append(_fmt_section("Rejected by failure_class x agent x model"))
    if report["rejected_distribution"]:
        for r in report["rejected_distribution"]:
            out.append(f"  {r['failure_class']} / {r['agent']} / {r['model']}: {r['count']}")
    else:
        out.append("  (no rejected in window)")

    out.append(_fmt_section("lead_degraded/lead_restored pairs"))
    if report["degradation_pairs"]:
        for p in report["degradation_pairs"]:
            out.append(f"  degraded(line={p['degraded_line']}, ts={p['degraded_ts']}) -> "
                       f"restored(line={p['restored_line']}, ts={p['restored_ts']}): {p['note']}")
    else:
        out.append("  (no degradation events in window)")

    out.append(_fmt_section("Unclosed tasks (last lifecycle event = delegated)"))
    if report["unclosed_tasks"]:
        out.append("  " + ", ".join(report["unclosed_tasks"]))
    else:
        out.append("  (none)")

    return "\n".join(out)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--journal", action="append", required=True,
                    help="path to routing-log.jsonl (repeatable)")
    p.add_argument("--window-start", default=None, help="ISO ts, inclusive (>=)")
    p.add_argument("--window-end", default=None, help="ISO ts, exclusive (<)")
    p.add_argument("--by-since", default=DEFAULT_BY_SINCE,
                    help="when the 'by'-field validator was activated (default "
                         f"{DEFAULT_BY_SINCE})")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    window_start = parse_ts(args.window_start) if args.window_start else None
    if args.window_start and window_start is None:
        print(f"calibration_counts: invalid --window-start {args.window_start!r}",
              file=sys.stderr)
        return 2
    window_end = parse_ts(args.window_end) if args.window_end else None
    if args.window_end and window_end is None:
        print(f"calibration_counts: invalid --window-end {args.window_end!r}",
              file=sys.stderr)
        return 2
    by_since = parse_ts(args.by_since)
    if by_since is None:
        print(f"calibration_counts: invalid --by-since {args.by_since!r}", file=sys.stderr)
        return 2

    reports = []
    for path in args.journal:
        try:
            reports.append(analyze_journal(path, window_start, window_end, by_since))
        except OSError as exc:
            print(f"calibration_counts: failed to read {path!r}: {exc}", file=sys.stderr)
            return 2

    if args.json:
        print(json.dumps({"journals": reports}, ensure_ascii=False, indent=2))
        return 0

    for report in reports:
        print(render_text(report))

    if len(reports) > 1:
        print(_fmt_section("SUMMARY across all journals"))
        for report in reports:
            n_rule6 = len(report["rule6_candidates"])
            n_field = len(report["field_violations"])
            n_by = len(report["by_violations"])
            n_dup = len(report["duplicate_delegates"])
            n_ts = len(report["ts_anomalies"])
            n_rejected = report["counts"]["by_event"].get("rejected", 0)
            print(f"  {report['journal']}: rejected={n_rejected}, rule6_candidates={n_rule6}, "
                  f"field_violations={n_field}, by_violations={n_by}, "
                  f"duplicate_delegates={n_dup}, ts_anomalies={n_ts}, "
                  f"unparsable={len(report['unparsable'])}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
