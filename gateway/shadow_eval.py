"""Shadow Evaluation: replay sampled Lead requests on a cheaper model
and compare outputs, turning DELEGATION_TABLE.md estimates into
evidence-backed rows (ARCHITECTURE.md, "Shadow Evaluation"; D-0028).

For each sampled request: replay the same prompt on --target-model,
compare the replayed answer to the original. Two comparison modes:

- difflib character similarity (default) -- transparent but crude: it
  punishes verbose-but-correct answers (a real weakness, see
  judge_calibration.json for the labeled pairs that expose it);
- --judge-model ALIAS -- an LLM judge through the gateway (so judge
  cost lands in the Ledger, Rule #1) that sees the task and both
  anonymized answers and rules EQUIVALENT/WORSE ignoring verbosity
  and formatting. Judge verdicts override difflib in decide_status.
  Calibrate the judge first: --calibrate judge_calibration.json
  --judge-model ALIAS reports agreement with the manual labels.

Results are grouped by the same keyword-heuristic task category
metrics.py uses, so a category can accumulate enough samples across
runs to cross the --min-samples bar.

Per DELEGATION_TABLE.md Update Rule 4, cost comparison uses TOTAL
replay cost. Caveat: a single-shot replay does not measure retry
loops, so a "provisionally_validated" verdict here only confirms
one-shot quality, not the retry-loop cost risk rule 4 warns about --
and never means "production_validated" (that status requires a
full-week window + cost-per-accepted-task evidence, per
DELEGATION_TABLE.md's status vocabulary, D-0035); note the caveat in
DELEGATION_TABLE.md when relying on it.

Table status cells move ONLY via the weekly calibration process
(DELEGATION_TABLE.md Update Rule 1); this module does not write to
the table. Use --record-evidence to append this run's evidence line
to docs/SHADOW_EVALUATION_LOG.md for that process to consume.

Usage:
    python shadow_eval.py --source-model lead --target-model intern
    python shadow_eval.py --source-model lead --target-model intern --record-evidence
"""

import argparse
import datetime
import difflib
import json
import os
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import litellm

from metrics import categorize

# metrics.py category -> exact "Task type" cell text in DELEGATION_TABLE.md.
# Used only as a human-readable label in reports/evidence lines: no code
# path updates the table -- statuses move via weekly calibration only
# (Update Rule 1). "formatting" has no row of its own in this deployment's
# DELEGATION_TABLE.md (adapt CATEGORY_TO_TASK_TYPE's labels to your table's
# actual wording when they diverge), so it keeps the neutral label; it is
# still evaluated and recorded like any other category.
CATEGORY_TO_TASK_TYPE = {
    "coding": "Routine code generation",
    "summarization": "Summarization",
    "extraction": "Data extraction, format conversion",
    "classification": "Classification, tagging",
    "formatting": "(no table row)",
}

DEFAULT_SIMILARITY_THRESHOLD = 0.5
DEFAULT_MIN_SAMPLES = 2
DEFAULT_PASS_THRESHOLD = 0.75

# The judge sees only the task and two anonymized answers, never model
# names, and is instructed to ignore exactly what breaks difflib:
# verbosity, formatting and phrasing (see judge_calibration.json).
# parse_verdict() takes the LAST keyword, so the judge may reason before
# the final verdict line -- useful for a thinking model that wraps its
# reasoning in <think> blocks before committing to a verdict.
# NOTE: sample_requests() filters judge contamination by matching the
# first sentence of this prompt -- keep it stable or update the filter.
JUDGE_SYSTEM_PROMPT = (
    "You are an impartial judge comparing two answers to the same task. "
    "Decide whether Answer B accomplishes the task as well as Answer A. "
    "Judge ONLY against what the task explicitly asked for. "
    "Verbosity, formatting, phrasing, markdown fences and extra "
    "explanation do NOT matter. If Answer A includes extras the task "
    "did not ask for (input validation, error handling, edge-case "
    "tests, examples), Answer B is NOT worse for lacking them. "
    "First verify each answer against the task step by step; for code, "
    "trace the execution on one or two small inputs before claiming a "
    "bug. Then reply on the final line with exactly one word: "
    "EQUIVALENT if Answer B accomplishes what the task explicitly "
    "asked as well as Answer A (or better), or WORSE if Answer B "
    "fails or is incorrect at the explicit task."
)


def sample_requests(conn: sqlite3.Connection, source_model: str, days: int, limit: int):
    """Random sample of successful requests for source_model, most recent --days.

    Judge calls are excluded: when the judge model doubles as a traffic
    model, its judge prompts land in the log under the same alias, and
    replaying them would contaminate the sample."""
    rows = conn.execute(
        "SELECT id, prompt, response, COALESCE(cost_usd, 0), category, completion_tokens"
        " FROM requests"
        " WHERE model = ? AND status = 'success' AND prompt IS NOT NULL"
        " AND response IS NOT NULL AND substr(ts, 1, 10) >= date('now', ?)"
        " AND traffic_kind NOT IN ('replay', 'judge')"
        " AND prompt NOT LIKE '%impartial judge comparing two answers%'"
        " ORDER BY RANDOM() LIMIT ?",
        (source_model, f"-{days} days", limit),
    ).fetchall()
    return [
        {"id": r[0], "prompt": r[1], "response": r[2], "cost_usd": r[3], "category": r[4],
         "completion_tokens": r[5]}
        for r in rows
    ]


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def _extract_cost(response, model: str, db_path, call_start):
    """Rule #1 cost accounting: take the cost the PROXY accounted,
    never recompute client-side. litellm.completion_cost(response)
    looks up the ALIAS name (e.g. "openai/middle-groq") in the
    client's own pricing map, which does not know gateway aliases ->
    silent $0.0000 on every call.

    response._hidden_params["response_cost"] is the correct source:
    verified empirically (a live call through the gateway) that it
    already equals the cost the proxy logged in requests.db for the
    same call, with no header parsing needed.

    Fallback (hidden_params missing/None): read the newest matching
    row from requests.db for this model, ts >= call_start. If that
    also fails, return None -- an explicit "unknown" is required
    instead of a silent $0.0000 (Rule #1)."""
    hidden_params = getattr(response, "_hidden_params", None) or {}
    cost = hidden_params.get("response_cost")
    if cost is not None:
        return cost
    if db_path is None:
        return None
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT cost_usd FROM requests WHERE model = ? AND ts >= ?"
            " ORDER BY ts DESC LIMIT 1",
            (model, call_start.isoformat()),
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def replay(messages: list, target_model: str, gateway: str, db_path=None,
           max_tokens: int = None, **kwargs):
    """Runs the same messages on target_model through the gateway.
    Returns (response_text, cost_usd, finish_reason); cost_usd is None
    if it could not be determined (see _extract_cost). kwargs pass
    through to litellm (tests use mock_response to avoid a live
    model/proxy).

    max_tokens: passed to litellm.completion when not None. Without
    it the replay target inherits the provider's own default cap,
    which can be far below what the source answer needed -- a replay
    target can truncate a reply the source model needed thousands of
    tokens for, which would measure the stand's own cap rather than
    the candidate's quality. None preserves the old not-passed
    behavior (callers that don't care, e.g. judge_pair's short
    verdict).

    traffic_kind is sent via extra_body, not litellm.completion's own
    metadata= kwarg: that kwarg only feeds litellm's local logging
    object and is never written into the HTTP request body sent to a
    remote api_base, so the proxy-side callback never sees it
    (verified empirically: metadata= produced 'real' rows on the
    proxy; extra_body={"metadata": ...} reaches the callback)."""
    call_start = datetime.datetime.now()
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    response = litellm.completion(
        model=f"openai/{target_model}",
        api_base=gateway.rstrip("/") + "/v1",
        api_key=os.environ.get("GATEWAY_API_KEY", "anything"),
        messages=messages,
        extra_body={"metadata": {"traffic_kind": "replay"}},
        **kwargs,
    )
    choice = response.choices[0]
    text = choice.message.content
    finish_reason = getattr(choice, "finish_reason", None)
    cost = _extract_cost(response, target_model, db_path, call_start)
    return text, cost, finish_reason


def _auto_max_tokens(source_completion_tokens) -> int:
    """Auto per-pair replay max_tokens: 1.3x the source's own
    completion_tokens, floored at 8192 so the replay target isn't
    starved by its own provider default. NULL/0 source (pre-migration
    rows, or a source call whose usage wasn't logged) floors straight
    to 8192 rather than raising on None * 1.3."""
    tokens = source_completion_tokens or 0
    return max(int(tokens * 1.3), 8192)


def parse_verdict(text: str):
    """Extracts the judge's verdict from its reply. Thinking models
    may wrap reasoning in <think> blocks or restate both options --
    take the last keyword outside think blocks."""
    if not text:
        return None
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    matches = re.findall(r"\b(equivalent|worse)\b", cleaned, flags=re.IGNORECASE)
    if not matches:
        return None
    return "equivalent" if matches[-1].lower() == "equivalent" else "target_worse"


def judge_pair(task_prompt: str, source_answer: str, target_answer: str,
               judge_model: str, gateway: str, db_path=None, **kwargs):
    """Asks judge_model (through the gateway, so judge cost lands in
    the Ledger) whether the target answer is as good as the source's.
    Returns (verdict, cost_usd): verdict is 'equivalent', 'target_worse',
    or None if unparseable; cost_usd is the judge call's own cost (Rule
    #1: supervision cost must be visible where the delegation decision
    is recorded), or None if it could not be determined (_extract_cost).

    temperature=0: at default temperature the verdict on borderline
    pairs can flip between runs, which makes calibration numbers
    irreproducible. Tests and callers may still override via kwargs."""
    kwargs.setdefault("temperature", 0)
    call_start = datetime.datetime.now()
    response = litellm.completion(
        model=f"openai/{judge_model}",
        api_base=gateway.rstrip("/") + "/v1",
        api_key=os.environ.get("GATEWAY_API_KEY", "anything"),
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Task:\n{task_prompt}\n\n"
                f"Answer A:\n{source_answer}\n\n"
                f"Answer B:\n{target_answer}\n\nVerdict:",
            },
        ],
        extra_body={"metadata": {"traffic_kind": "judge"}},
        **kwargs,
    )
    cost = _extract_cost(response, judge_model, db_path, call_start)
    return parse_verdict(response.choices[0].message.content), cost


def last_user_content(messages: list) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return message.get("content") or ""
    return ""


def evaluate(conn, source_model: str, target_model: str, gateway: str, days: int, sample_n: int, judge_model: str = None, categories: set = None, db_path=None, pace: float = 0.0, max_tokens_override: int = None, **replay_kwargs):
    """categories: optional whitelist. A replay only supports the table
    row whose 'Delegate to' tier the target actually is, so a run
    aimed at one row (e.g. coding -> Middle) must not touch rows whose
    named tier differs from the target.
    pace: seconds to sleep between pairs (free-tier RPM/TPM ceilings).
    max_tokens_override: falsy (None or 0) -> auto per-pair max_tokens
    from the source row's completion_tokens (_auto_max_tokens);
    truthy -> that fixed value is used for every pair instead.

    Category priority: a stored category (requests.category, set by
    the regression runner from the ground-truth JSONL field) is used
    directly; categorize() is only the fallback for rows with no
    stored category (NULL), i.e. untagged real traffic."""
    results = []
    for i, row in enumerate(sample_requests(conn, source_model, days, sample_n)):
        if i and pace:
            time.sleep(pace)
        try:
            messages = json.loads(row["prompt"])
        except (TypeError, json.JSONDecodeError):
            continue
        category = row["category"] or categorize(row["prompt"])
        if categories and category not in categories:
            continue
        target_max_tokens = (
            max_tokens_override if max_tokens_override
            else _auto_max_tokens(row["completion_tokens"])
        )
        try:
            replayed_text, replayed_cost, finish_reason = replay(
                messages, target_model, gateway, db_path=db_path,
                max_tokens=target_max_tokens, **replay_kwargs
            )
            error = None
        except Exception as exc:
            replayed_text, replayed_cost, finish_reason, error = None, None, None, str(exc)
        verdict, judge_cost = None, None
        if judge_model and error is None:
            try:
                verdict, judge_cost = judge_pair(
                    last_user_content(messages), row["response"], replayed_text,
                    judge_model, gateway, db_path=db_path, **replay_kwargs,
                )
            except Exception as exc:
                error = f"judge: {exc}"
        results.append(
            {
                "request_id": row["id"],
                "category": category,
                "source_cost_usd": row["cost_usd"],
                "target_cost_usd": replayed_cost,
                "similarity": similarity(row["response"], replayed_text) if error is None else 0.0,
                "verdict": verdict,
                "judge_cost_usd": judge_cost,
                "error": error,
                "truncated": finish_reason == "length",
            }
        )
    return results


def aggregate_by_category(results: list) -> dict:
    buckets = defaultdict(list)
    for r in results:
        buckets[r["category"]].append(r)

    aggregated = {}
    for category, items in buckets.items():
        n = len(items)
        judged = [i for i in items if i.get("verdict")]
        judge_costs = [i["judge_cost_usd"] for i in items if i.get("judge_cost_usd") is not None]
        aggregated[category] = {
            "n": n,
            "mean_similarity": round(sum(i["similarity"] for i in items) / n, 4),
            "mean_source_cost_usd": round(sum(i["source_cost_usd"] for i in items) / n, 6),
            "mean_target_cost_usd": round(
                sum(i["target_cost_usd"] or 0 for i in items) / n, 6
            ),
            "errors": sum(1 for i in items if i["error"]),
            "truncated": sum(1 for i in items if i.get("truncated")),
            "pass_rate": round(
                sum(1 for i in judged if i["verdict"] == "equivalent") / len(judged), 4
            )
            if judged
            else None,
            "mean_judge_cost_usd": round(sum(judge_costs) / len(judge_costs), 6)
            if judge_costs
            else None,
        }
    return aggregated


def decide_status(agg: dict, similarity_threshold: float, min_samples: int,
                  pass_threshold: float = DEFAULT_PASS_THRESHOLD) -> str:
    """"estimated" means inconclusive here (not enough evidence yet to
    move off the table's default), distinct from a positive validation.
    A positive shadow-eval run can only ever return
    "provisionally_validated" (DELEGATION_TABLE.md status vocabulary,
    D-0035): a single-shot replay against a sample is exactly what that
    status means, and never "production_validated" (which requires a
    full-week real-traffic window + cost-per-accepted-task evidence).
    When judge verdicts are present (pass_rate) they override the
    difflib similarity -- the heuristic produces false rejections on
    verbose-but-correct answers (see judge_calibration.json)."""
    if agg["n"] < min_samples or agg["errors"] == agg["n"]:
        return "estimated"
    if agg["mean_target_cost_usd"] > agg["mean_source_cost_usd"]:
        return "rejected"
    if agg.get("pass_rate") is not None:
        return "provisionally_validated" if agg["pass_rate"] >= pass_threshold else "rejected"
    return "provisionally_validated" if agg["mean_similarity"] >= similarity_threshold else "rejected"


_SHADOW_EVAL_HEADER_RE = re.compile(
    r"^#{1,6}\s*Shadow Evaluation Log\s*$", re.MULTILINE
)


def append_evidence_log(text: str, entries: list) -> str:
    """Appends one line per entry to the chronological tail of the Shadow
    Evaluation Log (docs/SHADOW_EVALUATION_LOG.md -- kept as its own file
    so DELEGATION_TABLE.md holds only current Status cells, and the run
    history lives here). The heading is matched at any depth
    (metrics.py's parse_shadow_eval_log uses the same regex: the file's own
    top-level heading is "# Shadow Evaluation Log", H1, not a "##"
    subsection heading nested inside another document). If the heading is
    missing entirely (e.g. a fresh/empty file), one is created at H1."""
    match = _SHADOW_EVAL_HEADER_RE.search(text)
    if not match:
        text = text.rstrip("\n") + (
            "\n\n# Shadow Evaluation Log\n\n"
            "Evidence for DELEGATION_TABLE.md Update Rule 1. One line per"
            " Shadow Evaluation run.\n\n"
        )
    lines = text.splitlines()
    insert_at = len(lines)
    for entry in entries:
        lines.insert(insert_at, f"- {entry}")
        insert_at += 1
    return "\n".join(lines) + "\n"


def record_evidence(shadow_log_path: Path, date: str, source_model: str,
                    target_model: str, aggregated: dict, statuses: dict,
                    judge_model: str = None):
    """Appends this run's evidence lines to shadow_log_path
    (docs/SHADOW_EVALUATION_LOG.md). Table status cells are NOT touched
    here: per DELEGATION_TABLE.md Update Rule 1, statuses move only via
    the weekly calibration process, which reads this log as evidence.
    This function writes only the evidence side of that split -- a code
    path that writes table statuses directly is exactly what Update
    Rule 1 says must not exist outside weekly calibration."""
    entries = []
    for category in CATEGORY_TO_TASK_TYPE:
        if category not in aggregated:
            continue
        agg = aggregated[category]
        status = statuses[category]
        # Rule #1: pass_rate and judge cost are decoupled (as in
        # format_report). If verdicts drove the status, the evidence
        # line must say so even when cost extraction failed -- an
        # explicit "judge_cost=unknown" instead of silently dropping
        # the whole judged segment.
        judged = ""
        if agg.get("pass_rate") is not None:
            judged = f"  judge={judge_model} pass_rate={agg['pass_rate']:.2f}"
            if agg.get("mean_judge_cost_usd") is not None:
                judged += f" judge_cost=${agg['mean_judge_cost_usd']:.4f}"
            else:
                judged += " judge_cost=unknown"
        # errors= (unjudged pairs) and truncated= (stand-clipped replies)
        # must survive in the durable evidence line, not only the console
        # report: both are the kind of thing a retrospective audit needs
        # to find without re-running the eval.
        entries.append(
            f"{date}  category={category}  source={source_model} target={target_model}"
            f"  n={agg['n']}  sim={agg['mean_similarity']:.2f}{judged}"
            f"  cost_source=${agg['mean_source_cost_usd']:.4f}"
            f" cost_target=${agg['mean_target_cost_usd']:.4f}"
            f"  errors={agg.get('errors', 0)} truncated={agg.get('truncated', 0)}"
            f"  -> {status}"
        )
    if entries:
        shadow_log_path = Path(shadow_log_path)
        log_text = shadow_log_path.read_text(encoding="utf-8") if shadow_log_path.exists() else ""
        log_text = append_evidence_log(log_text, entries)
        shadow_log_path.write_text(log_text, encoding="utf-8")


def calibrate(pairs: list, judge_model: str, gateway: str,
              pace: float = 0.0, **kwargs) -> dict:
    """Runs the judge on manually labeled pairs (judge_calibration.json)
    and reports agreement with the human labels. The judge must
    reproduce them before its verdicts are trusted in --record-evidence.
    pace: seconds to sleep between pairs (free-tier RPM ceilings)."""
    agreements, mismatches = 0, []
    for i, pair in enumerate(pairs):
        if i and pace:
            time.sleep(pace)
        verdict, _judge_cost = judge_pair(
            pair["prompt"], pair["source_response"], pair["target_response"],
            judge_model, gateway, **kwargs,
        )
        if verdict == pair["verdict"]:
            agreements += 1
        else:
            mismatches.append(
                {"index": i, "category": pair.get("category"),
                 "expected": pair["verdict"], "got": verdict,
                 "prompt": pair["prompt"][:80]}
            )
    return {"n": len(pairs), "agreements": agreements, "mismatches": mismatches}


def format_report(source_model, target_model, aggregated, statuses) -> str:
    lines = [f"SHADOW EVALUATION: {source_model} -> {target_model}", ""]
    if not aggregated:
        lines.append(f"  no successful {source_model!r} requests in range")
        return "\n".join(lines)
    for category, agg in sorted(aggregated.items()):
        mapped = CATEGORY_TO_TASK_TYPE.get(category, "(no table row)")
        judged = (
            f" pass_rate={agg['pass_rate']:.0%}" if agg.get("pass_rate") is not None else ""
        )
        judge_cost = (
            f" judge_cost=${agg['mean_judge_cost_usd']:.4f}"
            if agg.get("mean_judge_cost_usd") is not None
            else ""
        )
        lines.append(
            f"  {category} [{mapped}]: n={agg['n']} sim={agg['mean_similarity']:.0%}{judged}{judge_cost}"
            f" cost {source_model}=${agg['mean_source_cost_usd']:.4f}"
            f" vs {target_model}=${agg['mean_target_cost_usd']:.4f}"
            f" errors={agg['errors']} truncated={agg.get('truncated', 0)}"
            f" -> {statuses[category]}"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Shadow Evaluation: replay + compare")
    parser.add_argument("--source-model", default="lead", help="gateway alias whose requests to sample")
    parser.add_argument("--target-model", default="intern", help="cheaper gateway alias to replay on")
    parser.add_argument("--gateway", default="http://localhost:4000")
    parser.add_argument(
        "--db",
        default=os.environ.get("GATEWAY_DB_PATH", Path(__file__).parent / "requests.db"),
    )
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--sample", type=int, default=10)
    parser.add_argument("--threshold", type=float, default=DEFAULT_SIMILARITY_THRESHOLD)
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--judge-model", help="gateway alias for the LLM judge; overrides difflib similarity")
    parser.add_argument("--pass-threshold", type=float, default=DEFAULT_PASS_THRESHOLD,
                        help="min share of 'equivalent' judge verdicts for 'provisionally_validated'")
    parser.add_argument("--calibrate", metavar="PAIRS_JSON",
                        help="run --judge-model on labeled pairs and report agreement; no replay")
    parser.add_argument("--pace", type=float, default=0.0,
                        help="seconds between calibration pairs (free-tier RPM ceilings)")
    parser.add_argument("--max-tokens", type=int, default=0,
                        help="replay completion token cap. Positive = fixed override for"
                        " every pair; 0 (default) = auto per-pair, 1.3x the source's own"
                        " completion_tokens floored at 8192 (without this the replay"
                        " target inherits its provider's default cap and can truncate"
                        " answers longer than that default)")
    parser.add_argument("--categories",
                        help="comma-separated category whitelist (e.g. 'coding');"
                        " restricts the run to rows whose Delegate-to tier matches the target")
    parser.add_argument(
        "--record-evidence", action="store_true",
        help="append run evidence lines to docs/SHADOW_EVALUATION_LOG.md;"
             " table statuses move only via weekly calibration, Update Rule 1",
    )
    parser.add_argument(
        "--shadow-log",
        default=Path(__file__).parent.parent / "docs" / "SHADOW_EVALUATION_LOG.md",
        help="Path to docs/SHADOW_EVALUATION_LOG.md; one evidence line per"
             " run is appended here (kept as its own file so the table"
             " keeps only current Status cells)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.calibrate:
        if not args.judge_model:
            raise SystemExit("--calibrate requires --judge-model")
        pairs = json.loads(Path(args.calibrate).read_text(encoding="utf-8"))
        report = calibrate(pairs, args.judge_model, args.gateway, pace=args.pace)
        print(f"JUDGE CALIBRATION: {args.judge_model} on {report['n']} labeled pairs")
        print(f"  agreement: {report['agreements']}/{report['n']}")
        for m in report["mismatches"]:
            print(
                f"  MISMATCH #{m['index']} [{m['category']}]"
                f" expected={m['expected']} got={m['got']} :: {m['prompt']}"
            )
        return

    if not Path(args.db).exists():
        raise SystemExit(f"request log not found: {args.db}")
    if args.source_model == args.target_model:
        raise SystemExit(
            "source and target model must differ: comparing a model to itself"
            " is not evidence a cheaper tier can substitute for it"
        )

    conn = sqlite3.connect(args.db)
    categories = set(args.categories.split(",")) if args.categories else None
    results = evaluate(conn, args.source_model, args.target_model, args.gateway,
                       args.days, args.sample, judge_model=args.judge_model,
                       categories=categories, db_path=args.db, pace=args.pace,
                       max_tokens_override=args.max_tokens or None)
    aggregated = aggregate_by_category(results)
    statuses = {
        category: decide_status(agg, args.threshold, args.min_samples, args.pass_threshold)
        for category, agg in aggregated.items()
    }

    if args.json:
        print(json.dumps({"aggregated": aggregated, "statuses": statuses, "results": results}, indent=2))
    else:
        print(format_report(args.source_model, args.target_model, aggregated, statuses))

    if args.record_evidence and aggregated:
        record_evidence(
            Path(args.shadow_log),
            datetime.date.today().isoformat(),
            args.source_model,
            args.target_model,
            aggregated,
            statuses,
            judge_model=args.judge_model,
        )


if __name__ == "__main__":
    main()
