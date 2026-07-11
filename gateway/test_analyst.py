"""Tests for the Analyst. No local model or API keys required:
litellm mock_response short-circuits the network call.

Run: python -m pytest gateway/test_analyst.py
"""

import json

from analyst import ask, build_messages

DIGEST = {
    "days": 1,
    "per_day": [
        {"day": "2026-07-03", "model": "lead", "requests": 3,
         "failures": 0, "prompt_tokens": 5100, "completion_tokens": 950,
         "cost_usd": 0.127, "avg_latency_ms": 850.0, "avg_response_chars": 11.0}
    ],
    "categories_heuristic": {"coding": {"requests": 1, "cost_usd": 0.044}},
    "context_repetition_ratio": {"lead": 0.38},
    "budget_events": [],
}


def test_build_messages_contains_digest_and_question():
    messages = build_messages("why so expensive?", DIGEST)
    assert messages[0]["role"] == "system"
    assert "ONLY this digest" in messages[0]["content"]
    body = messages[1]["content"]
    assert "why so expensive?" in body
    assert json.loads(body.split("Telemetry digest:\n")[1].split("\n\nQuestion:")[0]) == DIGEST


def test_ask_returns_model_answer():
    answer = ask(
        "where did tokens go?",
        DIGEST,
        gateway="http://localhost:4000",
        mock_response="lead spent $0.127 across 3 requests",
    )
    assert answer == "lead spent $0.127 across 3 requests"
