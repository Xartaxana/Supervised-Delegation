"""Analyst: a small local model narrating Ledger telemetry.

ARCHITECTURE.md, "Analyst"; D-0027. The Analyst reads Ledger output —
never raw conversations — and answers operator questions like
"where did tokens go?".

Calls go through the gateway under the dedicated alias "analyst"
(config.yaml), so supervision cost itself lands in the Ledger and
stays measurable (Rule #1: supervision must cost less than it saves).

Usage:
    python analyst.py "why was yesterday expensive?" [--days N]
    python analyst.py --digest-only [--days N]   # show what the model sees
"""

import argparse
import json
import os
import sqlite3
from pathlib import Path

import litellm

from metrics import daily_digest

SYSTEM_PROMPT = (
    "You are the Analyst of an LLM gateway. You receive a telemetry digest "
    "(JSON) of the request log: per-model daily usage, costs, context "
    "repetition, heuristic task categories and budget events. Answer the "
    "operator's question using ONLY this digest. Be concise and concrete: "
    "name models, numbers and days. If the digest cannot answer the "
    "question, say exactly what data is missing. Reply in the language "
    "of the question."
)


def build_messages(question: str, digest: dict) -> list:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "Telemetry digest:\n"
            + json.dumps(digest, ensure_ascii=False)
            + "\n\nQuestion: "
            + question,
        },
    ]


def ask(question: str, digest: dict, gateway: str, **kwargs) -> str:
    response = litellm.completion(
        model="openai/analyst",
        api_base=gateway.rstrip("/") + "/v1",
        api_key=os.environ.get("GATEWAY_API_KEY", "anything"),
        messages=build_messages(question, digest),
        **kwargs,
    )
    return response.choices[0].message.content


def main():
    parser = argparse.ArgumentParser(description="Analyst over Ledger telemetry")
    parser.add_argument("question", nargs="?", default="Where did tokens go?")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--gateway", default="http://localhost:4000")
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "GATEWAY_DB_PATH", Path(__file__).parent / "requests.db"
        ),
    )
    parser.add_argument("--digest-only", action="store_true")
    args = parser.parse_args()

    if not Path(args.db).exists():
        raise SystemExit(f"request log not found: {args.db}")

    digest = daily_digest(sqlite3.connect(args.db), args.days)
    if args.digest_only:
        print(json.dumps(digest, indent=2, ensure_ascii=False))
        return
    print(ask(args.question, digest, args.gateway))


if __name__ == "__main__":
    main()
