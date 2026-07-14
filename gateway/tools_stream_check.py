"""Isolation script: does the litellm proxy forward structured
tool_calls for a given model, in streaming and non-streaming mode?

Persistent (unlike a one-off session-scratchpad script) so future
sessions can re-run it without recreating it. Isolates the
groq-streaming tool-call break recorded in PI_HARNESS.md "Known gaps
and recipes" (verdict: NOT reproduced on litellm 1.90.2).
Named *_check.py, not *_test.py, to stay out of the pytest glob
(same convention as regression_runner.py).

Usage:
    python tools_stream_check.py --model builder --stream
    python tools_stream_check.py --model builder --no-stream
    python tools_stream_check.py --model scout --stream
    python tools_stream_check.py --model analyst --stream

Exit code 0 if a structured tool_call was observed, 1 otherwise.
Talks to the proxy at http://localhost:4000 (override with --base-url).
"""
import argparse
import json
import sys
import urllib.request
import urllib.error

ONE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city. You MUST use this tool to answer any question about weather.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                },
                "required": ["city"],
            },
        },
    }
]

MESSAGES = [
    {
        "role": "user",
        "content": (
            "What is the weather in Paris right now? You must use the "
            "get_weather tool to answer - do not answer from memory."
        ),
    }
]


def post(base_url: str, payload: dict, timeout: float) -> urllib.request.Request:
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


def run_nonstream(base_url: str, model: str, timeout: float) -> bool:
    payload = {
        "model": model,
        "messages": MESSAGES,
        "tools": ONE_TOOL,
        "tool_choice": "auto",
        "stream": False,
    }
    try:
        resp = post(base_url, payload, timeout)
        body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[non-stream] HTTP error {e.code}: {e.read().decode('utf-8', 'replace')}")
        return False
    except Exception as e:
        print(f"[non-stream] request failed: {e!r}")
        return False

    print(f"[non-stream] finish_reason: {body.get('choices', [{}])[0].get('finish_reason')}")
    msg = body.get("choices", [{}])[0].get("message", {})
    tool_calls = msg.get("tool_calls")
    if tool_calls:
        print(f"[non-stream] OK: structured tool_calls present: {json.dumps(tool_calls)}")
        return True
    print(f"[non-stream] FAIL: no tool_calls. message: {json.dumps(msg)[:500]}")
    return False


def run_stream(base_url: str, model: str, timeout: float) -> bool:
    payload = {
        "model": model,
        "messages": MESSAGES,
        "tools": ONE_TOOL,
        "tool_choice": "auto",
        "stream": True,
    }
    try:
        resp = post(base_url, payload, timeout)
    except urllib.error.HTTPError as e:
        print(f"[stream] HTTP error {e.code}: {e.read().decode('utf-8', 'replace')}")
        return False
    except Exception as e:
        print(f"[stream] request failed: {e!r}")
        return False

    raw_chunks = []
    tool_call_fragments = {}  # index -> {"name": str, "arguments": str}
    finish_reason = None
    saw_any_tool_delta = False

    for line_bytes in resp:
        line = line_bytes.decode("utf-8", "replace").strip()
        if not line:
            continue
        raw_chunks.append(line)
        if not line.startswith("data:"):
            continue
        data_str = line[len("data:"):].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices", [])
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        fr = choices[0].get("finish_reason")
        if fr:
            finish_reason = fr
        tc_deltas = delta.get("tool_calls")
        if tc_deltas:
            saw_any_tool_delta = True
            for tcd in tc_deltas:
                idx = tcd.get("index", 0)
                frag = tool_call_fragments.setdefault(idx, {"name": "", "arguments": ""})
                fn = tcd.get("function", {})
                if fn.get("name"):
                    frag["name"] += fn["name"]
                if fn.get("arguments"):
                    frag["arguments"] += fn["arguments"]

    print(f"[stream] finish_reason: {finish_reason}")
    print(f"[stream] saw tool_call deltas in stream: {saw_any_tool_delta}")
    if tool_call_fragments:
        print(f"[stream] OK: reassembled tool_calls: {json.dumps(tool_call_fragments)}")
        return True

    print(f"[stream] FAIL: no structured tool_call deltas reassembled.")
    print(f"[stream] raw chunk count: {len(raw_chunks)}")
    print("[stream] raw chunks (up to first 20):")
    for c in raw_chunks[:20]:
        print(f"    {c}")
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, help="Gateway alias, e.g. middle-groq, builder-groq, intern")
    ap.add_argument("--base-url", default="http://localhost:4000", help="Proxy base URL")
    ap.add_argument("--timeout", type=float, default=60.0)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stream", action="store_true", dest="stream")
    mode.add_argument("--no-stream", action="store_false", dest="stream")
    args = ap.parse_args()

    print(f"=== model={args.model} stream={args.stream} base_url={args.base_url} ===")
    if args.stream:
        ok = run_stream(args.base_url, args.model, args.timeout)
    else:
        ok = run_nonstream(args.base_url, args.model, args.timeout)

    print(f"=== RESULT: {'PASS (structured tool_call)' if ok else 'FAIL (no structured tool_call)'} ===")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
