"""dispatch_gate.py -- PreToolUse hook for the Task/Agent tool that
checks the SHAPE of a dispatch before it goes out, enforcing two
CLAUDE.md rules in code rather than relying on discipline alone:
rule 11 (DoD-in-every-dispatch / dispatch-context-manifest) and rule 7
(the dispatch label starts with the worker's tier).

Contract (PreToolUse hook stdin JSON): {"tool_name": str,
"tool_input": {"subagent_type": str, "prompt": str,
"description": str}, "cwd": str, ...}. Only tool_name in
{"Task", "Agent"} is inspected -- any other tool passes silently
(exit 0, no output). Exit 2 with a message on stderr blocks the call;
exit 0 allows it. The hook is stateless: it never reads or writes any
file, it only inspects the payload it was given.

Checks:
 1. subagent_type == "builder": tool_input["prompt"] must contain a
    DoD marker (DOD_MARKERS_RE). None found -> BLOCK
    (BLOCK_MESSAGE_NO_DOD).
 2. subagent_type == "builder" AND the prompt shows a write indicator
    (WRITE_INDICATORS_RE) -- a conservative heuristic: block ONLY
    when a write indicator is present AND BOTH manifest markers
    (MANIFEST_GIVEN_RE and MANIFEST_OWNS_RE) are missing -> BLOCK
    (BLOCK_MESSAGE_NO_MANIFEST). No write indicator -> check 2 is
    skipped entirely (a read-only dispatch needs no manifest).
 3. ANY subagent_type (including critic/scout): tool_input["description"],
    IF PRESENT, must start with a leading token followed by a
    separator ([ :-]) -- LABEL_MODEL_PREFIX_RE below. This is a FORM
    check only, on purpose: this template has no fixed list of tier/
    model names (a deployment configures its own bindings in
    delegation.config.yaml), so the hook can only verify that the
    label carries *some* leading tag-like token, not that the token
    names a real tier. description absent from the payload -> check 3
    is skipped.
 4. critic/scout (any subagent_type != "builder") -- checks 1 and 2 do
    not apply to them; their own DoD shape is different (rule 11
    describes it in prose, not as a prompt-text pattern).

Priority when several checks fail at once: 1 -> 2 -> 3, the first one
found is the single stderr message (the hook blocks with one message,
not a list).

Fail-open on a payload that isn't valid JSON -- same principle as
every other hook in this file set: a hook that can't parse its input
must not block an unrelated tool call.
"""

import json
import re
import sys

DOD_MARKERS_RE = re.compile(
    r"DoD|acceptance criteria|критери[ия] приёмки|witness|"
    r"verification run|проверочн\w+ прогон",
    re.IGNORECASE,
)
# \b-bounded so a marker only matches as a whole word -- otherwise a
# short Cyrillic root like "правь" would also match as a substring
# inside unrelated longer words (e.g. "поправь", "исправь").
WRITE_INDICATORS_RE = re.compile(
    r"\bowns\b|\bwrite file\b|\bcreate file\b|\bedit file\b|\bmodify file\b|"
    r"\bзапиши\b|\bсоздай файл\b|\bправь\b|\bизмени файл\b",
    re.IGNORECASE,
)
MANIFEST_GIVEN_RE = re.compile(r"given|дано", re.IGNORECASE)
MANIFEST_OWNS_RE = re.compile(r"owns", re.IGNORECASE)
# Portable form check (see module docstring, check 3): a leading
# non-whitespace token followed by a separator. Deliberately NOT a
# fixed list of model/tier names -- this template doesn't know a
# deployment's actual bindings.
LABEL_MODEL_PREFIX_RE = re.compile(r"^\S+[ :-]")

BLOCK_MESSAGE_NO_DOD = (
    "A builder dispatch with no DoD does not go out (rule 11): add "
    "acceptance criteria and a verification run whose output becomes "
    "the witness."
)
BLOCK_MESSAGE_NO_MANIFEST = (
    "A writing dispatch with no context manifest (given/owns) does not "
    "go out (rule 11, dispatch-context-manifest rule)."
)
BLOCK_MESSAGE_NO_LABEL = (
    "The dispatch label starts with the worker's tier (rule 7): "
    "e.g. 'sonnet: ...'."
)


def decide(payload: dict) -> tuple[int, str]:
    """Pure decision logic, no I/O -- directly testable. Returns
    (exit_code, stderr_message); "" means "write nothing to stderr"."""
    tool_name = payload.get("tool_name")
    if tool_name not in ("Task", "Agent"):
        return 0, ""

    tool_input = payload.get("tool_input") or {}
    subagent_type = tool_input.get("subagent_type")
    prompt = tool_input.get("prompt") or ""
    description = tool_input.get("description")

    if subagent_type == "builder":
        if not DOD_MARKERS_RE.search(prompt):
            return 2, BLOCK_MESSAGE_NO_DOD

        if WRITE_INDICATORS_RE.search(prompt):
            has_manifest = bool(MANIFEST_GIVEN_RE.search(prompt)) and bool(
                MANIFEST_OWNS_RE.search(prompt)
            )
            if not has_manifest:
                return 2, BLOCK_MESSAGE_NO_MANIFEST

    if description is not None:
        if not LABEL_MODEL_PREFIX_RE.search(description):
            return 2, BLOCK_MESSAGE_NO_LABEL

    return 0, ""


def _reconfigure_stderr_utf8():
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    _reconfigure_stderr_utf8()

    # Read stdin as raw bytes and decode explicitly as UTF-8 rather
    # than through the text-mode sys.stdin.read(): the latter decodes
    # with the platform's locale encoding, which on some systems (e.g.
    # Windows with a non-UTF-8 code page) is NOT UTF-8 and would
    # mangle any non-ASCII payload before the regexes above ever see
    # it. errors="replace" keeps this fail-open on malformed bytes.
    raw_bytes = sys.stdin.buffer.read()
    raw = raw_bytes.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except Exception:
        # Unparseable input -- fail open, same principle as every
        # other hook in this file set.
        return 0

    exit_code, message = decide(payload)
    if exit_code == 2:
        sys.stderr.write(message + "\n")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
