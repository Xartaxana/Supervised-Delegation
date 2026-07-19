"""tier_echo.py -- SubagentStop hook that prints the coordinator the
ACTUAL model(s) a finished subagent ran on, measured from its own jsonl
transcript -- a response to the class "a tier requirement with no code
cross-check" (code guarantees the ENCOUNTER with a measurement; the
session/coordinator still judges any discrepancy). This hook never
blocks anything and never argues with the session -- it just places the
measured fact in front of it.

Ported from HQ 2026-07-20.

SubagentStop PAYLOAD CONTRACT (verified empirically against the
installed harness): base fields (session_id, transcript_path, cwd,
prompt_id?) + hook_event_name="SubagentStop", stop_hook_active: bool,
agent_id, agent_transcript_path, agent_type, last_assistant_message?,
background_tasks?. `agent_transcript_path` is a REQUIRED field of the
SubagentStop event specifically -- so the finished subagent's
transcript path is built deterministically straight from
payload["agent_transcript_path"]; no guessing by mtime/glob is needed
or done.

IMPORTANT EMPIRICAL CAVEAT: the confirmed payload schema carries
neither a "model"/"model_id" field (the requested model) nor a
"description" field (the dispatch label's tier prefix, "haiku: ..." /
"opus: ..."). Both are fields of the Task/Agent tool's INPUT
(PreToolUse), not confirmed as part of the SubagentStop event. This
hook nonetheless supports comparing against payload["description"] IF
it carries a tier-prefixed label in the dispatch-label convention
("<word>: ..."): if the field is absent, the MISMATCH branch is simply
skipped (the measured part still prints, unflagged). On the real,
empirically-confirmed payload this branch will practically never fire
(the field isn't there) -- the code supports it anyway because the
comparison is cheap and harmless when the field is absent, and it costs
nothing to leave the door open for a harness that does add it.

OUTPUT CHANNEL: `hookSpecificOutput.additionalContext` on stdout, exit
0 -- the channel confirmed to actually reach the coordinator (bare
stderr at exit 0 is swallowed by the harness). The response shape
mirrors hygiene_gate.py's, adjusted for this hook's own event name:

  {"hookSpecificOutput": {"hookEventName": "SubagentStop",
                           "additionalContext": "<TIER ECHO line...>"}}

The previous stderr write is KEPT in addition (not replaced): it does
not hurt (nobody reads it, but it doesn't corrupt anything either), and
keeping it lowers the regression risk for any external consumer that
may already be parsing this hook's stderr. Both writes only happen when
counts is non-empty (main() step 4 below); on every silent branch
(malformed payload, no transcript_path, file didn't open, empty/all-
synthetic transcript) neither channel is touched at all.

main() logic:
 1. Byte-safe stdin read -> JSON. A parse failure (or payload not a
    dict) -> silent exit 0.
 2. agent_transcript_path = payload.get("agent_transcript_path") --
    must be a non-empty string; otherwise (missing, empty, not a
    string) -> silent exit 0 (guessing by another method is
    deliberately not done).
 3. Read the jsonl file line by line (byte-safe, errors="replace" --
    survives invalid UTF-8 BYTES in the file itself, not just stdin);
    a missing/unopenable file -> silent exit 0. Malformed JSON lines
    are skipped without breaking the rest of the parse. For each line
    with type=="assistant" and a valid string message.model, NOT in
    SKIP_MODELS ("<synthetic>" -- harness-internal stop-sequence lines,
    not a real subagent turn) -- count a turn for that model (order of
    first appearance in the transcript = output order). A non-string/
    empty/synthetic model does not count as a turn (does not break the
    parse either).
 4. No counted model at all (empty/all-non-assistant transcript) ->
    silent exit 0 -- nothing to report.
 5. Builds the line "TIER ECHO (measured): <model>=<turns>[, ...]"; if
    the payload carries "description" in the dispatch-label convention
    ("<word>: ...", word being exactly one of haiku/sonnet/opus/fable)
    AND no measured model-string contains that word as a
    case-insensitive substring -- appends " MISMATCH vs declared
    '<word>'". The line goes through an ASCII sanitizer -- a local
    copy, not a shared import, following this file's own
    self-containment (every hook script in this toolkit is
    self-contained, with no cross-file imports of its own).
 6. Prints the line to stderr (kept, see "OUTPUT CHANNEL" above) AND
    prints the JSON {"hookSpecificOutput": {"hookEventName":
    "SubagentStop", "additionalContext": "<same line>"}} as one line
    on stdout -- the channel that actually reaches the coordinator.
    Exit 0 always. Any unexpected exception anywhere in main() is
    caught by ONE boundary and turned into a silent exit 0 (fail-open,
    the same principle as every hook in this toolkit)."""

import json
import re
import sys
from pathlib import Path

KNOWN_TIER_WORDS = ("haiku", "sonnet", "opus", "fable")

# Harness-internal stop-sequence transcript lines carry
# model=="<synthetic>" -- not a real subagent turn, meaningless for the
# echo and would distort the turn count/model list.
SKIP_MODELS = {"<synthetic>"}


def _ascii_sanitize(s: str, max_len: int = 80) -> str:
    """Local copy of the console-safety pattern used across this
    toolkit's hooks: any externally-sourced value gets its control
    chars stripped, non-ASCII replaced, and length capped. A copy, not
    an import -- see the module docstring on self-containment."""
    s = str(s).strip()
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    s = s.encode("ascii", "replace").decode("ascii")
    return s[:max_len]


def _extract_agent_transcript_path(payload: dict):
    """agent_transcript_path -- a field of the SubagentStop event (see
    module docstring for the confirmed schema). Returns None if the
    field is missing, empty, or not a string -- the caller then exits
    silently, without trying to guess the path another way (the
    contract deliberately forbids that)."""
    value = payload.get("agent_transcript_path")
    return value if isinstance(value, str) and value else None


def _extract_declared_tier(payload: dict):
    """Looks for a dispatch-label tier prefix in payload["description"]
    (the "haiku: ..." / "sonnet: ..." / "opus: ..." / "fable: ..."
    convention). Returns the word in lowercase only if the prefix up to
    the first colon matches exactly one of KNOWN_TIER_WORDS -- otherwise
    None (no colon at all, a prefix like "opus2" -- undeterminable, the
    MISMATCH branch is skipped entirely)."""
    description = payload.get("description")
    if not isinstance(description, str) or not description:
        return None
    if ":" not in description:
        return None
    prefix = description.split(":", 1)[0].strip().lower()
    if prefix in KNOWN_TIER_WORDS:
        return prefix
    return None


def iter_transcript_models(path: str):
    """Yields one model string per assistant turn of the transcript, in
    file order. Byte-safe line-by-line read (errors="replace" -- the
    file may contain invalid UTF-8 bytes; this does not break the read,
    it substitutes them); malformed JSON lines are skipped silently
    without interrupting the rest of the parse. Line shape: top-level
    "type"=="assistant", "message"."model", with the same SKIP_MODELS
    filter for synthetic lines. Lines with message.model missing/not a
    string/synthetic are skipped (not counted as a turn), without
    breaking the parse.

    Honest counting semantics: "=N" in the final line is a count of
    assistant-type JSONL lines in THIS transcript, not deduplicated by
    an API-level request id the way a cost-accounting report would --
    the set of models encountered plus a rough line count is enough for
    an echo to the coordinator; cost-grade deduplication is not needed
    here."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            model = message.get("model")
            if isinstance(model, str) and model and model not in SKIP_MODELS:
                yield model


def count_models(models) -> dict:
    """Counts turns per model, PRESERVING first-appearance order
    (a plain Python 3.7+ dict, insertion order), for a deterministic
    build_line() output."""
    counts = {}
    for model in models:
        counts[model] = counts.get(model, 0) + 1
    return counts


def build_line(counts: dict, declared_tier) -> str:
    """Assembles the final line from the counted models (in counts
    order, i.e. first appearance) plus, if applicable, a MISMATCH
    suffix (only when declared_tier is set AND no measured model
    contains it as a case-insensitive substring)."""
    parts = [
        f"{_ascii_sanitize(model)}={count}" for model, count in counts.items()
    ]
    line = "TIER ECHO (measured): " + ", ".join(parts)

    if declared_tier:
        matched = any(declared_tier in model.lower() for model in counts)
        if not matched:
            line += f" MISMATCH vs declared '{_ascii_sanitize(declared_tier)}'"

    return line


def main() -> int:
    try:
        raw_bytes = sys.stdin.buffer.read()
        raw = raw_bytes.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            return 0
        if not isinstance(payload, dict):
            return 0

        transcript_path = _extract_agent_transcript_path(payload)
        if not transcript_path:
            return 0

        try:
            models = list(iter_transcript_models(transcript_path))
        except OSError:
            return 0

        counts = count_models(models)
        if not counts:
            return 0

        declared_tier = _extract_declared_tier(payload)
        line = build_line(counts, declared_tier)

        sys.stderr.write(line + "\n")

        output = {
            "hookSpecificOutput": {
                "hookEventName": "SubagentStop",
                "additionalContext": line,
            }
        }
        sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
