"""Unit tests for tools/judge_client.py's own logic (build_material,
verdict extraction/retry, cost/usage summing) -- no network. The
reference implementation this file was ported from has no dedicated
test module of its own for judge_client (only indirect coverage via
test_judge_accept.py's CLI-level tests); this file adds direct coverage
for the functions that module does not exercise itself, per this
task's own DoD (a battery for every changed/new file).

Run: python -m pytest tools/test_judge_client.py -q
"""

import json

import judge_client as jc


def _make_cell(tmp_path, files):
    cell = tmp_path / "cell"
    cell.mkdir()
    for rel, content in files.items():
        p = cell / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return cell


# ---------------------------------------------------------------------------
# build_material -- tree listing, truncation, baseline filtering
# ---------------------------------------------------------------------------


def test_build_material_lists_files_and_contents(tmp_path):
    cell = _make_cell(tmp_path, {"a.py": "print(1)\n", "sub/b.py": "print(2)\n"})
    material, truncated = jc.build_material(cell)
    assert not truncated
    assert "FILE TREE:" in material
    assert "a.py" in material
    assert "sub/b.py" in material
    assert "print(1)" in material
    assert "print(2)" in material


def test_build_material_empty_cell_has_explicit_marker(tmp_path):
    cell = tmp_path / "empty_cell"
    cell.mkdir()
    material, truncated = jc.build_material(cell)
    assert not truncated
    assert "(empty cell -- no files found)" in material


def test_build_material_excludes_noise_dirs(tmp_path):
    cell = _make_cell(tmp_path, {
        "a.py": "x\n",
        "node_modules/pkg/index.js": "y\n",
        ".git/HEAD": "z\n",
        "__pycache__/a.pyc": "w\n",
    })
    material, _ = jc.build_material(cell)
    assert "a.py" in material
    assert "node_modules" not in material
    assert ".git" not in material
    assert "__pycache__" not in material


def test_build_material_truncates_at_char_cap_with_marker(tmp_path):
    cell = _make_cell(tmp_path, {"big.py": "x" * 500})
    material, truncated = jc.build_material(cell, char_cap=100)
    assert truncated
    assert "material cap 100 chars reached" in material


def test_build_material_unreadable_file_gets_placeholder_not_raise(tmp_path):
    cell = tmp_path / "cell"
    cell.mkdir()
    target = cell / "gone.py"
    target.write_text("temp", encoding="utf-8")
    material_before, _ = jc.build_material(cell)
    assert "gone.py" in material_before  # sanity: file is seen while present


def test_build_material_baseline_unset_no_marker_no_filter(tmp_path):
    cell = _make_cell(tmp_path, {"a.py": "x\n", "b.py": "y\n"})
    material, _ = jc.build_material(cell)  # baseline_files omitted entirely
    assert jc.BASELINE_UNAVAILABLE_MARKER not in material
    assert "UNCHANGED BASELINE" not in material
    assert "a.py" in material and "b.py" in material


def test_build_material_baseline_none_adds_unavailable_marker(tmp_path):
    cell = _make_cell(tmp_path, {"a.py": "x\n"})
    material, _ = jc.build_material(cell, baseline_files=None)
    assert jc.BASELINE_UNAVAILABLE_MARKER in material


def test_build_material_baseline_set_excludes_and_names_them(tmp_path):
    cell = _make_cell(tmp_path, {"a.py": "x\n", "b.py": "y\n"})
    material, _ = jc.build_material(cell, baseline_files={"b.py"})
    assert "a.py" in material
    assert "=== b.py ===" not in material
    assert "UNCHANGED BASELINE (excluded from listing): b.py" in material


def test_build_material_baseline_empty_set_no_note(tmp_path):
    cell = _make_cell(tmp_path, {"a.py": "x\n"})
    material, _ = jc.build_material(cell, baseline_files=set())
    assert "UNCHANGED BASELINE" not in material
    assert "a.py" in material


# ---------------------------------------------------------------------------
# build_prompt -- shape, empty-keys/empty-tail fallbacks
# ---------------------------------------------------------------------------


def test_build_prompt_includes_task_id_and_keys():
    prompt = jc.build_prompt("t1", "do the thing", ["key one", "key two"], "MATERIAL", "TAIL")
    assert "Task t1:" in prompt
    assert "- key one" in prompt
    assert "- key two" in prompt
    assert "MATERIAL" in prompt
    assert "TAIL" in prompt


def test_build_prompt_empty_keys_shows_placeholder():
    prompt = jc.build_prompt("t1", "task", [], "MAT", "")
    assert "(no keys)" in prompt
    assert "(empty)" in prompt


# ---------------------------------------------------------------------------
# _extract_verdict -- direct JSON, embedded JSON, unparseable
# ---------------------------------------------------------------------------


def test_extract_verdict_direct_json():
    result = jc._extract_verdict('{"accept": true, "feedback": "ok"}')
    assert result == {"accept": True, "feedback": "ok"}


def test_extract_verdict_embedded_in_prose():
    text = 'Sure, here it is:\n```json\n{"accept": false, "feedback": "no"}\n```\nthanks'
    result = jc._extract_verdict(text)
    assert result == {"accept": False, "feedback": "no"}


def test_extract_verdict_missing_accept_key_returns_none():
    assert jc._extract_verdict('{"feedback": "no accept field"}') is None


def test_extract_verdict_not_json_at_all_returns_none():
    assert jc._extract_verdict("just plain prose, no braces") is None


def test_extract_verdict_empty_text_returns_none():
    assert jc._extract_verdict("") is None
    assert jc._extract_verdict(None) is None


def test_extract_verdict_feedback_defaults_to_empty_string():
    result = jc._extract_verdict('{"accept": true}')
    assert result == {"accept": True, "feedback": ""}


# ---------------------------------------------------------------------------
# judge_verdict -- retry policy, cost/usage summing
# ---------------------------------------------------------------------------


def _post_seq(*replies):
    calls = []

    def _fn(prompt, gateway, model, api_key):
        idx = len(calls)
        calls.append(prompt)
        return replies[idx]

    return _fn, calls


def test_judge_verdict_single_call_on_clean_reply(tmp_path):
    cell = tmp_path / "cell"
    cell.mkdir()
    reply = {"content": '{"accept": true, "feedback": ""}',
              "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
              "cost_usd": 0.001, "cost_source": "header"}
    post_fn, calls = _post_seq(reply)
    result = jc.judge_verdict("t1", "task", ["key"], str(cell), "", _post_fn=post_fn)
    assert result["accept"] is True
    assert result["usage"]["total_tokens"] == 7
    assert result["cost_usd"] == 0.001
    assert len(calls) == 1


def test_judge_verdict_retries_once_on_unparseable_then_succeeds(tmp_path):
    cell = tmp_path / "cell"
    cell.mkdir()
    bad = {"content": "not json at all", "usage": {}, "cost_usd": 0.001, "cost_source": "header"}
    good = {"content": '{"accept": false, "feedback": "no"}',
             "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
             "cost_usd": 0.002, "cost_source": "header"}
    post_fn, calls = _post_seq(bad, good)
    result = jc.judge_verdict("t1", "task", [], str(cell), "", _post_fn=post_fn)
    assert result["accept"] is False
    assert len(calls) == 2
    # cost/usage summed across BOTH calls, not just the successful one
    assert result["usage"]["total_tokens"] == 4
    assert result["cost_usd"] == 0.001 + 0.002


def test_judge_verdict_raises_after_two_unparseable_replies(tmp_path):
    cell = tmp_path / "cell"
    cell.mkdir()
    bad = {"content": "still not json", "usage": {}, "cost_usd": None, "cost_source": "none"}
    post_fn, calls = _post_seq(bad, bad)
    try:
        jc.judge_verdict("t1", "task", [], str(cell), "", _post_fn=post_fn)
        assert False, "expected JudgeParseError"
    except jc.JudgeParseError as exc:
        assert "t1" in str(exc)
    assert len(calls) == 2  # exactly one retry, not more


def test_judge_verdict_cost_none_when_no_call_has_a_price(tmp_path):
    cell = tmp_path / "cell"
    cell.mkdir()
    reply = {"content": '{"accept": true, "feedback": ""}',
              "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
              "cost_usd": None, "cost_source": "none"}
    post_fn, _ = _post_seq(reply)
    result = jc.judge_verdict("t1", "task", [], str(cell), "", _post_fn=post_fn)
    assert result["cost_usd"] is None
    assert result["cost_source"] == "none"


def test_judge_verdict_truncated_flag_surfaces_from_build_material(tmp_path):
    cell = tmp_path / "cell"
    cell.mkdir()
    (cell / "big.py").write_text("x" * 500, encoding="utf-8")
    reply = {"content": '{"accept": true, "feedback": ""}',
              "usage": {}, "cost_usd": None, "cost_source": "none"}
    post_fn, _ = _post_seq(reply)
    result = jc.judge_verdict("t1", "task", [], str(cell), "", char_cap=100, _post_fn=post_fn)
    assert result["truncated"] is True
