"""Deterministic zero-tool-call fabrication guard for non-Claude
("Pi") workers -- the zero-tool-call fabrication class.

A candidate model can answer a scout-style prompt with zero REAL tool
calls while producing a confident, detailed, entirely fabricated
answer (observed empirically: a small local model answered every
golden question AND fabricated a "Trail" section citing files that do
not exist in the repo at all). This script stamps PASS/REJECTED/
INCONCLUSIVE on a worker's run BEFORE a coordinator reads its prose
report, so a fabricated "I found X at file:line" cannot be graded on
its own say-so.

stdlib only (argparse, json, re, sqlite3) -- no provider-specific
imports, so this runs standalone against saved artifacts without a
live proxy.

SOURCES (need at least one, both are accepted and combined):

  --json <file>   a Pi-style `--mode json` event-stream file (one JSON
                   object per line):
                     - tool_execution_start / tool_execution_end events
                       carry `toolCallId` + `toolName` directly (the
                       structural tool-call signal).
                     - assistant message `content` is an array of
                       blocks; the three block types that matter are
                       "text" (field `text`), "thinking" (field
                       `thinking`), and "toolCall" (camelCase, not
                       "tool_use" or "tool-call").
                     - the run's final answer is the last `agent_end`
                       event's tail assistant message; `message_end` is
                       the fallback for a session that never reached
                       agent_end (cut off mid-run).

  --db <requests.db> --model <alias> --since <ts> [--until <ts>]
                   a window of gateway-style requests.db rows (schema:
                   ts, model, provider_model, status, prompt, response,
                   error). Opened READ-ONLY (sqlite3 URI mode=ro) --
                   this guard reads telemetry, it never writes to any
                   DB (spec requirement).

                   Structural tool-call signal here: a successful row
                   only ever stores the assistant's text in `response`
                   -- it does NOT store a structured tool_calls array.
                   The only place a completed tool round-trip survives
                   is the NEXT turn's `prompt` column, which the caller
                   re-serializes with a `{"role": "tool", ...,
                   "tool_call_id": "<id>"}` message appended. The id
                   count grows monotonically across a single session's
                   consecutive turns as history is re-sent each turn --
                   so counting is by DISTINCT id VALUE across the whole
                   window, not by occurrence, or later turns would
                   recount every earlier call once per turn.

                   Final-answer signal: the chronologically LAST row in
                   the window (ORDER BY ts, id -- id is the tie-breaker
                   since concurrent requests can log with an identical
                   ts), counted content-ful only if status == 'success'
                   AND response is non-empty after stripping. A
                   'success' row with an EMPTY response is a
                   tool-call-issuing turn with no visible text -- it
                   must NOT be read as a substantive answer even though
                   its own status says success.

KNOWN GOTCHA (worth repeating to any coordinator choosing a
--since/--until window): a --db window is a plain time-box, not a run
boundary. A window drawn loosely across a whole session can pick up
one unrelated, incidental real tool call from a connectivity probe
earlier in the window, and then PASS wrongly on an otherwise
fabricated answer later in the same window -- because that incidental
call did not inform the graded answer at all. The verdict below is
exactly the literal spec algorithm (aggregate distinct tool-call ids
across every row the window matches, vs. the window's own last
content-ful row) -- it does NOT try to associate a given tool call with
a given answer. Precise window scoping (tight --since/--until around
the specific answer being graded, excluding unrelated probe turns) is
the CALLER's responsibility, not something this script infers.

The END side of the window is equally load-bearing: the final-answer
signal is read off the window's LAST row, so a trailing empty
retry/probe row AFTER the graded answer turns a real fabrication into
INCONCLUSIVE, masking it as an ops abort. The window must END at the
specific answer being graded.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

TOOL_CALL_ID_RE = re.compile(r'"tool_call_id"\s*:\s*"([^"]+)"')


# ---------------------------------------------------------------------
# --json source (Pi `--mode json` event stream)
# ---------------------------------------------------------------------

def parse_json_events(path):
    """Yields one parsed event dict per non-blank line of a Pi
    `--mode json` output file. Lines that fail to parse as JSON are
    skipped (defensive -- docs/json.md documents "each line is a JSON
    object", but stdout could in principle carry stray text)."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _assistant_text(message: dict) -> str:
    """Joins the "text"-type content blocks of one assistant message.
    "thinking" blocks are deliberately excluded -- they are not the
    visible answer a coordinator reads. "toolCall" blocks carry no
    text of their own."""
    blocks = message.get("content") or []
    texts = [
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "".join(texts)


def analyze_json_events(events) -> tuple[set, str | None]:
    """Returns (tool_call_ids, final_text_or_None) for one --mode json
    stream. events: an iterable of already-parsed event dicts."""
    tool_call_ids = set()
    last_assistant_message = None
    last_agent_end_messages = None

    for event in events:
        etype = event.get("type")
        if etype == "tool_execution_start":
            tcid = event.get("toolCallId")
            if tcid:
                tool_call_ids.add(tcid)
        elif etype == "message_end":
            message = event.get("message") or {}
            if message.get("role") == "assistant":
                last_assistant_message = message
        elif etype == "agent_end":
            last_agent_end_messages = event.get("messages") or []

    final_message = None
    if last_agent_end_messages:
        for m in reversed(last_agent_end_messages):
            if m.get("role") == "assistant":
                final_message = m
                break
    if final_message is None:
        final_message = last_assistant_message

    final_text = None
    if final_message is not None:
        text = _assistant_text(final_message)
        final_text = text  # may be "" -- caller decides content-ful-ness

    return tool_call_ids, final_text


# ---------------------------------------------------------------------
# --db source (gateway/requests.db-schema window)
# ---------------------------------------------------------------------

def query_db_rows(db_path, model: str, since: str, until: str | None = None) -> list:
    """Read-only query (sqlite3 URI mode=ro -- this guard never writes
    to any DB). Returns rows as plain dicts, ordered chronologically
    (ts, then id as tie-breaker -- see module docstring: concurrent
    requests can share an identical ts)."""
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if until is not None:
            rows = conn.execute(
                "SELECT * FROM requests WHERE model = ? AND ts >= ? AND ts <= ?"
                " ORDER BY ts, id",
                (model, since, until),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM requests WHERE model = ? AND ts >= ?"
                " ORDER BY ts, id",
                (model, since),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def analyze_db_rows(rows: list) -> tuple[set, str | None]:
    """Returns (tool_call_ids, final_text_or_None) for a window of
    requests-table rows (list of dicts, chronological order -- see
    query_db_rows). Pure function, no DB access, so it is directly
    testable against hand-built row lists."""
    tool_call_ids = set()
    for row in rows:
        prompt = row.get("prompt") or ""
        tool_call_ids.update(TOOL_CALL_ID_RE.findall(prompt))

    final_text = None
    if rows:
        last = rows[-1]
        if last.get("status") == "success" and not last.get("error"):
            resp = (last.get("response") or "").strip()
            if resp:
                final_text = resp

    return tool_call_ids, final_text


# ---------------------------------------------------------------------
# Combine + verdict
# ---------------------------------------------------------------------

def combine_sources(json_result=None, db_result=None) -> tuple[int, bool, str | None]:
    """json_result / db_result: (tool_call_ids: set, final_text: str|None)
    or None if that source was not used. Returns
    (total_tool_calls, content_ok, final_text_used).

    KNOWN LIMITATION: if both sources describe the SAME physical run,
    their tool-call id namespaces differ (Pi's own toolCallId vs. the
    provider's tool_call_id) and totals are simply summed -- this can
    double-count the same underlying call when both sources are given
    for one run. Spec explicitly allows "either or both"; de-duplicating
    across namespaces would need a shared id, which the two telemetry
    layers do not share. Documented, not solved -- see module
    docstring's KNOWN GOTCHA on window scoping for the same class of
    caveat."""
    total = 0
    content_ok = False
    final_text_used = None
    for result in (json_result, db_result):
        if result is None:
            continue
        ids, text = result
        total += len(ids)
        if text is not None and text.strip():
            content_ok = True
            if final_text_used is None:
                final_text_used = text
    return total, content_ok, final_text_used


def verdict(tool_calls: int, content_ok: bool) -> tuple[str, int]:
    """Spec's verdict table, exactly:
    - no content-ful final answer anywhere -> INCONCLUSIVE (ops abort,
      e.g. 429/session cut -- not a verdict on the model), exit 2,
      REGARDLESS of tool_calls (a run that issued tool calls but never
      produced a final answer is not evidence either way).
    - content-ful answer + zero structural tool calls -> REJECTED
      (the zero-tool-call fabrication class), exit 1.
    - content-ful answer + >=1 structural tool call -> PASS, exit 0."""
    if not content_ok:
        return "INCONCLUSIVE", 2
    if tool_calls == 0:
        return "REJECTED", 1
    return "PASS", 0


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic zero-tool-call fabrication guard for Pi-style workers."
    )
    parser.add_argument("--json", help="Path to a Pi `--mode json` event-stream file")
    parser.add_argument("--db", help="Gateway requests.db-schema SQLite file (read-only)")
    parser.add_argument("--model", help="Gateway model alias to filter --db rows on (required with --db)")
    parser.add_argument("--since", help="ISO ts lower bound, inclusive (required with --db)")
    parser.add_argument("--until", default=None, help="ISO ts upper bound, inclusive (optional with --db)")
    args = parser.parse_args(argv)

    if not args.json and not args.db:
        parser.error("need at least one of --json or --db")
    if args.db and not (args.model and args.since):
        parser.error("--db requires --model and --since")

    json_result = None
    db_result = None
    sources_desc = []

    if args.json:
        events = list(parse_json_events(args.json))
        ids, text = analyze_json_events(events)
        json_result = (ids, text)
        sources_desc.append(f"json={args.json} ({len(events)} events, {len(ids)} distinct tool_call ids)")

    if args.db:
        rows = query_db_rows(args.db, args.model, args.since, args.until)
        ids, text = analyze_db_rows(rows)
        db_result = (ids, text)
        window = f"{args.since}..{args.until or 'open'}"
        sources_desc.append(
            f"db={args.db} (model={args.model}, window={window}, {len(rows)} rows,"
            f" {len(ids)} distinct tool_call ids)"
        )

    tool_calls, content_ok, final_text = combine_sources(json_result, db_result)
    label, code = verdict(tool_calls, content_ok)

    print("=== PI RUN GUARD (zero-tool-call fabrication check) ===")
    for d in sources_desc:
        print(f"source: {d}")
    print(f"structural_tool_calls: {tool_calls}")
    print(f"content_ok: {content_ok}")
    if final_text is not None:
        preview = final_text[:200].replace("\n", " ")
        print(f"final_answer_preview: {preview!r}")
    print(f"verdict: {label}")
    if label == "REJECTED":
        print("reason: zero structural tool calls with a substantive final answer -- the zero-tool-call fabrication class")
    elif label == "INCONCLUSIVE":
        print("reason: no content-ful final answer in scope (ops abort -- 429/cut session/empty window), not a verdict on the model")
    print("=============================================================")

    return code


if __name__ == "__main__":
    sys.exit(main())
