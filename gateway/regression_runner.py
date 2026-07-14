"""Regression runner for Shadow Evaluation.

Sends each prompt from a JSONL regression set to the proxy and collects
HTTP status and response length. Designed as a source of synthetic traffic
for shadow_eval: the prompts are representative coding tasks from the
regression set, and each proxy call is self-tagged with
metadata.traffic_kind="synthetic" so that sqlite_logger.py does NOT
count them as real-traffic for quota accounting or shadow evaluation
sampling (self-tagged synthetic traffic; real-traffic gate G1).

Named *_runner.py, not *_test.py, to stay outside the pytest glob (see
tools_stream_check.py docstring for the same convention).

Usage:
    python gateway/regression_runner.py --model builder
    python gateway/regression_runner.py --model builder --max-n 3 --pace 0.5
    python gateway/regression_runner.py --model builder --dry-run --max-n 2
    python gateway/regression_runner.py --model builder --set gateway/regression_set_coding.jsonl

Exit code 0 if all requests succeeded, 1 if any failed.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def build_payload(model: str, prompt: str, category: str = None) -> dict:
    """Build the POST payload for a single prompt.

    category: ground-truth category from the regression set row (e.g.
    "coding"). Carried in metadata so sqlite_logger stores it as the
    category column, letting shadow_eval.py prefer it over the keyword
    heuristic for regression-set traffic.
    """
    metadata: dict = {"traffic_kind": "synthetic"}
    if category is not None:
        metadata["category"] = category
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "metadata": metadata,
    }


def post(base_url: str, payload: dict, timeout: float):
    """POST payload to /v1/chat/completions; return urllib response object."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer anything",
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def run(
    set_path: str,
    model: str,
    base_url: str = "http://localhost:4000",
    pace: float = 2.0,
    timeout: float = 120.0,
    max_n: int = 0,
    dry_run: bool = False,
) -> int:
    """Run the regression set and return exit code (0 = all ok, 1 = any failed)."""
    rows = []
    with open(set_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    if max_n and max_n > 0:
        rows = rows[:max_n]

    sent = 0
    ok = 0
    failed = 0

    for i, row in enumerate(rows):
        prompt = row["prompt"]
        source_task = row.get("source_task", "?")
        payload = build_payload(model, prompt, category=row.get("category"))

        if dry_run:
            print(f"[{i + 1}/{len(rows)}] source_task={source_task} DRY-RUN payload:")
            print(json.dumps(payload, ensure_ascii=False))
            continue

        if i and pace:
            time.sleep(pace)

        sent += 1
        try:
            resp = post(base_url, payload, timeout)
            body = json.loads(resp.read().decode("utf-8"))
            status = resp.status
            content = (
                body.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            ) or ""
            length = len(content)
            print(
                f"[{i + 1}/{len(rows)}] source_task={source_task}"
                f" status={status} len={length}"
            )
            ok += 1
        except urllib.error.HTTPError as exc:
            print(
                f"[{i + 1}/{len(rows)}] source_task={source_task}"
                f" HTTP error {exc.code}: {exc.read().decode('utf-8', 'replace')}"
            )
            failed += 1
        except Exception as exc:
            print(
                f"[{i + 1}/{len(rows)}] source_task={source_task}"
                f" error: {exc!r}"
            )
            failed += 1

    if dry_run:
        return 0

    print(f"\nsummary: sent={sent} ok={ok} failed={failed}")
    return 0 if failed == 0 else 1


def main() -> None:
    default_set = str(
        Path(__file__).parent / "regression_set_coding.jsonl"
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--set",
        default=default_set,
        help="Path to JSONL regression set (default: regression_set_coding.jsonl)",
    )
    ap.add_argument(
        "--model",
        required=True,
        help="Source model alias to use, e.g. lead-sonnet",
    )
    ap.add_argument(
        "--base-url",
        default="http://localhost:4000",
        help="Proxy base URL (default: http://localhost:4000)",
    )
    ap.add_argument(
        "--pace",
        type=float,
        default=2.0,
        help="Seconds to sleep between requests (default: 2.0)",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="HTTP request timeout in seconds (default: 120)",
    )
    ap.add_argument(
        "--max-n",
        type=int,
        default=0,
        help="Max rows to process; 0 = all (default: 0)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print payloads without sending any requests",
    )
    args = ap.parse_args()

    exit_code = run(
        set_path=args.set,
        model=args.model,
        base_url=args.base_url,
        pace=args.pace,
        timeout=args.timeout,
        max_n=args.max_n,
        dry_run=args.dry_run,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
