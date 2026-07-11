"""Tests for tools/pi_run_guard.py (t-017, F-14 zero-tool-call guard).

No network, no litellm/pi imports -- pure parsing/SQL over small
synthetic fixtures. The --mode json event/message shapes used below
match docs/json.md and the compiled dist/*.js of the installed
@earendil-works/pi-coding-agent 0.80.3 package (see pi_run_guard.py's
module docstring for the exact files/lines checked); the --db row
shapes match gateway/sqlite_logger.py's SCHEMA and the empirically
observed tool_call_id-in-prompt convention (see same docstring).

Run from repo root: python -m pytest tools/ gateway/ -q
"""

import json
import sqlite3

import pytest

import pi_run_guard as g


# ---------------------------------------------------------------------
# --json source
# ---------------------------------------------------------------------

def _write_jsonl(path, objs):
    with open(path, "w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o) + "\n")


def test_parse_json_events_skips_blank_and_invalid_lines(tmp_path):
    p = tmp_path / "run.json"
    p.write_text(
        '{"type": "agent_start"}\n'
        "\n"
        "not json at all\n"
        '{"type": "turn_start"}\n',
        encoding="utf-8",
    )
    events = list(g.parse_json_events(str(p)))
    assert [e["type"] for e in events] == ["agent_start", "turn_start"]


def test_analyze_json_events_zero_tool_calls_fabrication_shape():
    # Shape of t-011/t-016: agent_start/turn_start/message_end/agent_end,
    # no tool_execution_* events at all, substantive final text.
    events = [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_end", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Q1: fake/file.py:42 ..."}],
        }},
        {"type": "turn_end", "message": {}, "toolResults": []},
        {"type": "agent_end", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "questions"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Q1: fake/file.py:42 ..."}]},
        ]},
    ]
    ids, text = g.analyze_json_events(events)
    assert ids == set()
    assert text == "Q1: fake/file.py:42 ..."


def test_analyze_json_events_counts_distinct_tool_execution_start_ids():
    events = [
        {"type": "tool_execution_start", "toolCallId": "a1", "toolName": "read", "args": {}},
        {"type": "tool_execution_end", "toolCallId": "a1", "toolName": "read", "result": {}, "isError": False},
        {"type": "tool_execution_start", "toolCallId": "a2", "toolName": "bash", "args": {}},
        # a repeat start event for the same id must not double count
        {"type": "tool_execution_start", "toolCallId": "a1", "toolName": "read", "args": {}},
        {"type": "agent_end", "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "real answer"}]},
        ]},
    ]
    ids, text = g.analyze_json_events(events)
    assert ids == {"a1", "a2"}
    assert text == "real answer"


def test_analyze_json_events_prefers_agent_end_over_message_end():
    events = [
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "draft, superseded"}]}},
        {"type": "agent_end", "messages": [
            {"role": "user", "content": []},
            {"role": "assistant", "content": [{"type": "text", "text": "final"}]},
        ]},
    ]
    ids, text = g.analyze_json_events(events)
    assert text == "final"


def test_analyze_json_events_falls_back_to_message_end_without_agent_end():
    # Session cut short (e.g. process killed mid-run) -- no agent_end at all.
    events = [
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "last seen text"}]}},
    ]
    ids, text = g.analyze_json_events(events)
    assert text == "last seen text"


def test_analyze_json_events_no_assistant_message_gives_none():
    events = [{"type": "agent_start"}, {"type": "turn_start"}]
    ids, text = g.analyze_json_events(events)
    assert ids == set()
    assert text is None


def test_analyze_json_events_thinking_blocks_excluded_from_text():
    events = [
        {"type": "agent_end", "messages": [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "I will just make this up."},
                {"type": "text", "text": "the visible answer"},
            ]},
        ]},
    ]
    ids, text = g.analyze_json_events(events)
    assert text == "the visible answer"
    assert "make this up" not in text


def test_analyze_json_events_tool_call_only_final_message_has_empty_text():
    # Final assistant turn is a bare tool call with no text block at all
    # (content-ful check is the CALLER's job -- this just reports "").
    events = [
        {"type": "agent_end", "messages": [
            {"role": "assistant", "content": [{"type": "toolCall", "toolCallId": "z1", "toolName": "read"}]},
        ]},
    ]
    ids, text = g.analyze_json_events(events)
    assert text == ""


# ---------------------------------------------------------------------
# --db source (analyze_db_rows: pure function, no DB access)
# ---------------------------------------------------------------------

def _row(id, ts, status="success", response="", error=None, prompt="[]"):
    return {"id": id, "ts": ts, "status": status, "response": response, "error": error, "prompt": prompt}


def test_analyze_db_rows_zero_tool_calls_fabrication_shape():
    # Mirrors gateway/requests.db ids 249-255/257 (t-016 retro window,
    # minus the id-256 probe): repeated empty-response attempts then one
    # substantive, zero-tool-call final answer.
    rows = [
        _row(249, "2026-07-09T15:37:57", response=""),
        _row(250, "2026-07-09T15:39:27", response=""),
        _row(257, "2026-07-09T15:45:51", response="Q1: fake/file.py:42 ..."),
    ]
    ids, text = g.analyze_db_rows(rows)
    assert ids == set()
    assert text == "Q1: fake/file.py:42 ..."


def test_analyze_db_rows_dedupes_tool_call_id_across_growing_history():
    # Mirrors gateway/t013.db: each later row's prompt re-serializes the
    # WHOLE growing history, so counting occurrences (not distinct
    # values) would wildly overcount.
    p1 = '[{"role": "tool", "content": "x", "tool_call_id": "c1"}]'
    p2 = '[{"role": "tool", "content": "x", "tool_call_id": "c1"}, {"role": "tool", "content": "y", "tool_call_id": "c2"}]'
    rows = [
        _row(1, "2026-07-09T18:30:00", response=""),
        _row(2, "2026-07-09T18:30:05", response="", prompt=p1),
        _row(3, "2026-07-09T18:30:10", response="final answer text", prompt=p2),
    ]
    ids, text = g.analyze_db_rows(rows)
    assert ids == {"c1", "c2"}
    assert text == "final answer text"


def test_analyze_db_rows_success_with_empty_response_is_not_content_ful():
    # t013.db ids 21/24/26/30 shape: status success, response "" -- a
    # tool-call-issuing turn, not a substantive answer.
    rows = [_row(1, "2026-07-09T18:30:00", status="success", response="")]
    ids, text = g.analyze_db_rows(rows)
    assert text is None


def test_analyze_db_rows_failure_status_is_not_content_ful_even_with_text():
    rows = [_row(1, "2026-07-09T18:30:00", status="failure", response="", error="RateLimitError")]
    ids, text = g.analyze_db_rows(rows)
    assert text is None


def test_analyze_db_rows_final_text_only_from_last_row():
    rows = [
        _row(1, "2026-07-09T18:30:00", response="earlier text, not the final answer"),
        _row(2, "2026-07-09T18:30:05", response="", status="failure", error="boom"),
    ]
    ids, text = g.analyze_db_rows(rows)
    # last row (by list order, which query_db_rows guarantees is
    # chronological ts,id) is the failure -- no content-ful answer.
    assert text is None


def test_analyze_db_rows_empty_window_gives_no_content():
    ids, text = g.analyze_db_rows([])
    assert ids == set()
    assert text is None


# ---------------------------------------------------------------------
# --db source (query_db_rows: real SQLite, read-only contract)
# ---------------------------------------------------------------------

SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    model TEXT,
    provider_model TEXT,
    status TEXT NOT NULL,
    latency_ms REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    cost_usd REAL,
    prompt TEXT,
    response TEXT,
    error TEXT,
    traffic_kind TEXT NOT NULL DEFAULT 'real'
);
"""


@pytest.fixture()
def seeded_db(tmp_path):
    db_file = tmp_path / "requests.db"
    conn = sqlite3.connect(db_file)
    conn.execute(SCHEMA)
    conn.executemany(
        "INSERT INTO requests (ts, model, status, prompt, response, error) VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("2026-07-09T15:00:00", "intern", "success", "[]", "", None),
            ("2026-07-09T15:37:57", "intern", "success", "[]", "", None),
            ("2026-07-09T15:45:51", "intern", "success", "[]", "fabricated answer", None),
            ("2026-07-09T16:00:00", "intern", "success", "[]", "outside window", None),
            ("2026-07-09T15:50:00", "middle-groq", "success", "[]", "different model", None),
        ],
    )
    conn.commit()
    conn.close()
    return db_file


def test_query_db_rows_filters_by_model_and_window(seeded_db):
    rows = g.query_db_rows(str(seeded_db), "intern", "2026-07-09T15:37:00", "2026-07-09T15:46:00")
    assert [r["response"] for r in rows] == ["", "fabricated answer"]


def test_query_db_rows_since_without_until_is_open_ended(seeded_db):
    rows = g.query_db_rows(str(seeded_db), "intern", "2026-07-09T15:37:00")
    responses = [r["response"] for r in rows]
    assert responses == ["", "fabricated answer", "outside window"]


def test_query_db_rows_excludes_other_models(seeded_db):
    rows = g.query_db_rows(str(seeded_db), "middle-groq", "2026-07-09T00:00:00")
    assert len(rows) == 1
    assert rows[0]["response"] == "different model"


def test_query_db_rows_never_writes(seeded_db):
    # Read-only URI mode -- an attempted write through this connection
    # must fail closed, not silently succeed, proving the guard cannot
    # mutate the DB it inspects even if a future edit tried to.
    uri = f"file:{seeded_db.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO requests (ts, model, status) VALUES ('x', 'x', 'x')")
    conn.close()

    before = sqlite3.connect(seeded_db).execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    g.query_db_rows(str(seeded_db), "intern", "2026-07-09T00:00:00")
    after = sqlite3.connect(seeded_db).execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    assert before == after


# ---------------------------------------------------------------------
# combine_sources / verdict
# ---------------------------------------------------------------------

def test_combine_sources_sums_ids_and_ors_content_ok():
    total, content_ok, text = g.combine_sources(
        json_result=({"a", "b"}, None),
        db_result=(set(), "the answer"),
    )
    assert total == 2
    assert content_ok is True
    assert text == "the answer"


def test_combine_sources_ignores_missing_source():
    total, content_ok, text = g.combine_sources(json_result=({"a"}, "hi"), db_result=None)
    assert total == 1
    assert content_ok is True
    assert text == "hi"


def test_combine_sources_no_source_used_gives_no_content():
    total, content_ok, text = g.combine_sources()
    assert total == 0
    assert content_ok is False
    assert text is None


def test_combine_sources_blank_text_is_not_content_ok():
    total, content_ok, text = g.combine_sources(json_result=(set(), "   "))
    assert content_ok is False


def test_verdict_rejected_zero_tool_calls_content_ful():
    label, code = g.verdict(0, True)
    assert (label, code) == ("REJECTED", 1)


def test_verdict_pass_with_tool_calls_and_content():
    label, code = g.verdict(3, True)
    assert (label, code) == ("PASS", 0)


def test_verdict_inconclusive_no_content_even_with_tool_calls():
    # A run that issued tool calls but never produced a final answer
    # (e.g. cut off by a 429 before the closing turn) is an ops abort,
    # not evidence either way -- must NOT read as PASS.
    label, code = g.verdict(5, False)
    assert (label, code) == ("INCONCLUSIVE", 2)


def test_verdict_inconclusive_no_content_no_tool_calls():
    label, code = g.verdict(0, False)
    assert (label, code) == ("INCONCLUSIVE", 2)


# ---------------------------------------------------------------------
# CLI end-to-end (argument validation + exit codes)
# ---------------------------------------------------------------------

def test_main_requires_at_least_one_source(capsys):
    with pytest.raises(SystemExit) as exc:
        g.main([])
    assert exc.value.code == 2  # argparse.error() exit code


def test_main_db_without_model_or_since_errors(tmp_path):
    db_file = tmp_path / "requests.db"
    with pytest.raises(SystemExit) as exc:
        g.main(["--db", str(db_file)])
    assert exc.value.code == 2


def test_main_db_source_rejected_end_to_end(seeded_db, capsys):
    code = g.main([
        "--db", str(seeded_db), "--model", "intern",
        "--since", "2026-07-09T15:37:00", "--until", "2026-07-09T15:46:00",
    ])
    out = capsys.readouterr().out
    assert code == 1
    assert "verdict: REJECTED" in out


def test_main_json_source_pass_end_to_end(tmp_path, capsys):
    p = tmp_path / "run.json"
    _write_jsonl(str(p), [
        {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "read", "args": {}},
        {"type": "tool_execution_end", "toolCallId": "t1", "toolName": "read", "result": {}, "isError": False},
        {"type": "agent_end", "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "verified via read"}]},
        ]},
    ])
    code = g.main(["--json", str(p)])
    out = capsys.readouterr().out
    assert code == 0
    assert "verdict: PASS" in out


def test_main_inconclusive_end_to_end(tmp_path, capsys):
    p = tmp_path / "run.json"
    _write_jsonl(str(p), [{"type": "agent_start"}, {"type": "turn_start"}])
    code = g.main(["--json", str(p)])
    out = capsys.readouterr().out
    assert code == 2
    assert "verdict: INCONCLUSIVE" in out
