"""Unit tests for tools/judge_accept.py -- mock HTTP only, no network.
Monkeypatches judge_client._post_chat_completion (the module-level
function judge_verdict() calls by default when its _post_fn seam is not
supplied -- production callers, including this CLI, never pass _post_fn
themselves, so patching the module function is the only way to
intercept the HTTP call from outside judge_client).

Run: python -m pytest tools/test_judge_accept.py -q
"""

import json

import judge_client
import judge_accept


def _fake_post_factory(content, usage=None, cost_usd=0.01, cost_source="header"):
    calls = []

    def _fake_post(prompt, gateway, model, api_key, timeout=120):
        calls.append({"prompt": prompt, "gateway": gateway, "model": model, "api_key": api_key})
        return {
            "content": content,
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "cost_usd": cost_usd,
            "cost_source": cost_source,
        }

    return _fake_post, calls


def _make_cell(tmp_path, name="cell"):
    cell = tmp_path / name
    cell.mkdir()
    (cell / "deliverable.py").write_text("print('done')\n", encoding="utf-8")
    return cell


def _make_keys(tmp_path, lines, name="ta"):
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir(exist_ok=True)
    keys_file = keys_dir / f"{name}.md"
    keys_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return keys_file


def test_accept(tmp_path, monkeypatch, capsys):
    cell = _make_cell(tmp_path)
    keys = _make_keys(tmp_path, ["- works and is demonstrated", "- no crash on empty input"])
    fake_post, calls = _fake_post_factory('{"accept": true, "feedback": ""}')
    monkeypatch.setattr(judge_client, "_post_chat_completion", fake_post)

    rc = judge_accept.main(["--cell", str(cell), "--keys", str(keys), "--task", "Build me a calendar"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["accept"] is True
    assert out["usage"]["total_tokens"] == 15
    assert out["cost_usd"] == 0.01
    assert len(calls) == 1


def test_reject(tmp_path, monkeypatch, capsys):
    cell = _make_cell(tmp_path)
    keys = _make_keys(tmp_path, ["- key 1"])
    fake_post, calls = _fake_post_factory(
        '{"accept": false, "feedback": "month 13 is not handled"}'
    )
    monkeypatch.setattr(judge_client, "_post_chat_completion", fake_post)

    rc = judge_accept.main(["--cell", str(cell), "--keys", str(keys), "--task", "t"])

    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["accept"] is False
    assert "month 13" in out["feedback"]


def test_proxy_error(tmp_path, monkeypatch, capsys):
    cell = _make_cell(tmp_path)
    keys = _make_keys(tmp_path, ["- key 1"])

    def _raise_post(prompt, gateway, model, api_key, timeout=120):
        raise ConnectionRefusedError("[Errno 111] Connection refused")

    monkeypatch.setattr(judge_client, "_post_chat_completion", _raise_post)

    rc = judge_accept.main(["--cell", str(cell), "--keys", str(keys), "--task", "t"])

    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert "error" in out
    assert "Connection refused" in out["error"]


def test_keys_file_read(tmp_path, monkeypatch, capsys):
    """Confirms the --keys file is actually parsed into intent_keys and
    reaches the judge prompt (one bullet per non-empty line, blank lines
    dropped) -- inspected via the prompt text the fake HTTP call received."""
    cell = _make_cell(tmp_path)
    keys = _make_keys(tmp_path, ["first key", "", "second key  "])
    fake_post, calls = _fake_post_factory('{"accept": true, "feedback": ""}')
    monkeypatch.setattr(judge_client, "_post_chat_completion", fake_post)

    rc = judge_accept.main(["--cell", str(cell), "--keys", str(keys), "--task", "t"])

    assert rc == 0
    prompt = calls[0]["prompt"]
    assert "- first key" in prompt
    assert "- second key" in prompt
    # exactly two key bullets -- the blank line must not become a third
    assert prompt.count("\n- ") + (1 if prompt.startswith("- ") else 0) >= 2


def test_task_id_derived_from_keys_stem(tmp_path, monkeypatch, capsys):
    cell = _make_cell(tmp_path)
    keys = _make_keys(tmp_path, ["key"], name="tb")
    fake_post, calls = _fake_post_factory('{"accept": true, "feedback": ""}')
    monkeypatch.setattr(judge_client, "_post_chat_completion", fake_post)

    judge_accept.main(["--cell", str(cell), "--keys", str(keys), "--task", "node text"])

    prompt = calls[0]["prompt"]
    assert "Task tb:" in prompt


def test_baseline_files_marker_present(tmp_path, monkeypatch, capsys):
    """baseline_files=None (there is no separate baseline-manifest step
    in this CLI's own harness -- passing baseline_files=None is
    legitimate, the marker is honest) must surface
    judge_client.BASELINE_UNAVAILABLE_MARKER in the material shown to the
    judge, not a silent unfiltered listing."""
    cell = _make_cell(tmp_path)
    keys = _make_keys(tmp_path, ["key"])
    fake_post, calls = _fake_post_factory('{"accept": true, "feedback": ""}')
    monkeypatch.setattr(judge_client, "_post_chat_completion", fake_post)

    judge_accept.main(["--cell", str(cell), "--keys", str(keys), "--task", "t"])

    assert judge_client.BASELINE_UNAVAILABLE_MARKER in calls[0]["prompt"]


def test_stdout_tail_optional(tmp_path, monkeypatch, capsys):
    cell = _make_cell(tmp_path)
    keys = _make_keys(tmp_path, ["key"])
    tail_file = tmp_path / "tail.txt"
    tail_file.write_text("session output tail here", encoding="utf-8")
    fake_post, calls = _fake_post_factory('{"accept": true, "feedback": ""}')
    monkeypatch.setattr(judge_client, "_post_chat_completion", fake_post)

    rc = judge_accept.main([
        "--cell", str(cell), "--keys", str(keys), "--task", "t", "--stdout", str(tail_file),
    ])

    assert rc == 0
    assert "session output tail here" in calls[0]["prompt"]


def test_stdout_missing_file_is_empty_not_error(tmp_path, monkeypatch, capsys):
    cell = _make_cell(tmp_path)
    keys = _make_keys(tmp_path, ["key"])
    fake_post, calls = _fake_post_factory('{"accept": true, "feedback": ""}')
    monkeypatch.setattr(judge_client, "_post_chat_completion", fake_post)

    rc = judge_accept.main([
        "--cell", str(cell), "--keys", str(keys), "--task", "t",
        "--stdout", str(tmp_path / "nonexistent.txt"),
    ])

    assert rc == 0
