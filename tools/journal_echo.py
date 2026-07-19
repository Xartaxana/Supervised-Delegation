"""journal_echo.py -- PostToolUse hook that echo-validates the FRESH
(just-written-to-disk) state of logs/routing-log.jsonl immediately
after any tool call whose tool_input carries a path to this file. This
closes a class of defect: the pre-commit gate only sits on the COMMIT
path, so a session that never commits never meets the validator at
all. A journal defect is now visible to the coordinator at write time,
not only for the minority of sessions that reach a git commit.

Ported from HQ 2026-07-20.

REUSE BY IMPORT, not subprocess, not copy-paste (the same standard this
toolkit's other hooks hold each other to, see tools/tier_echo.py /
tools/dod_track.py, neither of which imports the other).
journal_validator.decide(staged_text, head_text, now) is the ONLY
function this hook calls on the validator: it already does exactly
what's needed -- new lines = the lines on disk beyond the HEAD prefix,
validate ONLY those, seeding state from HEAD the same way the
pre-commit gate does. Calling decide() as a whole, rather than pulling
its internals apart by hand, is the most direct form of reuse (not a
reimplementation of its insides). Side effect (deliberately wanted, not
just tolerated): append-only violations (editing an existing journal
line) are caught by this same call for free, since decide() already
does that check as its first step.

STANDALONE FALLBACK: "git unavailable / not a repo / an error" --
including the case where git WORKS but the file isn't on HEAD yet (a
new, never-committed journal) -- all of these reduce to one case:
head_text = None. journal_validator.decide(disk_text, None, now)
already behaves like a standalone run in that case: split_lines(None)
yields [], append-only passes vacuously against an empty head, and
validate_new_lines treats EVERY line on disk as "new". No separate
standalone function is needed here -- it's the same decide() call with
head_text=None, not a different logic branch.

TRIGGER: tool_input.file_path (extraction method: literally
`tool_input.get("file_path")`, with no additional filtering by
tool_name -- the trigger is defined purely by a path-tail match, not by
a list of edit tools). The tail is normalized for both separator styles
('/' and '\\\\') and compared component-wise against ("logs",
"routing-log.jsonl") -- not a substring check (otherwise
"xlogs/routing-log.jsonl" or "logs/not-routing-log.jsonl" would falsely
match).

REPO ROOT: parent.parent of file_path -- the directory that CONTAINS
logs/, regardless of where journal_echo.py itself and its calling hook
happen to sit; the root need not match the calling process's cwd (a
PostToolUse hook can run from any cwd) -- hence `git -C <root>`, not a
bare `git show` from the current directory.

Git call: `git -C <root> show HEAD:logs/routing-log.jsonl` -- success
gives stdout = the file's HEAD content, returncode 0; the file missing
on HEAD gives returncode 128 + "fatal: path ... does not exist in
'HEAD'"; a non-git directory gives returncode 128 + "fatal: not a git
repository"; a nonexistent directory gives returncode 128 + "fatal:
cannot change to ...". All error forms give a non-zero returncode --
the only branch the code needs: returncode == 0 -> use stdout as
head_text, otherwise -> head_text = None (see "STANDALONE FALLBACK"
above). One subprocess call, timeout=5s -- FileNotFoundError (the git
binary is missing) and subprocess.TimeoutExpired are caught by the same
block, also yielding None.

PERFORMANCE: the file is read from disk exactly ONCE (disk_text), git
is called exactly ONCE (_get_head_text), decide() itself does one
linear pass over the new lines. None of these operations repeat
anywhere on main()'s path.

OUTPUT: clean -> COMPLETE SILENCE (neither stdout nor stderr) -- don't
add noise to every clean write. Defects present -> a line of the form
"JOURNAL ECHO: N defect(s) in new lines: <msg1>; <msg2>; <msg3>[; +K
more]" (the first 3 validator messages joined with "; "; if there are
more than 3, "; +K more" is appended, K = N-3 -- see build_context())
goes out on BOTH channels, but with different dynamic-content handling:

 - stdout: JSON {"hookSpecificOutput": {"hookEventName": "PostToolUse",
   "additionalContext": "<string, RAW, non-ASCII left untouched>"}} --
   the channel confirmed to actually reach the coordinator (the same
   channel hygiene_gate.py uses). json.dumps(..., ensure_ascii=True)
   itself escapes any non-ASCII into safe \\uXXXX sequences on the
   wire; after json.loads() on the reader's side the text comes back
   readable -- so an ASCII-replace pass here would only degrade
   readability for no safety benefit.
 - stderr: plain text (NOT JSON, no \\u-escaping) -- a duplicate,
   written directly into this machine's console stream, where an
   ASCII-replace pass on the dynamic part is still required (some
   console codepages are not UTF-8).

In BOTH variants: the static English prefix/suffix ("JOURNAL ECHO: N
defect(s) in new lines: ", "; +K more") is a literal, never passed
through either sanitizer -- see build_context(). Sanitizing (in both
forms) applies ONLY to the dynamic part -- each inserted validator
message individually, BEFORE the join.

LOCAL COPIES of _raw_sanitize/_ascii_sanitize (not an import of
tier_echo -- every hook script in this toolkit is self-contained along
this dimension; the only explicit exception to self-containment in
this file is the journal_validator import, which is required by
design). MAX_MESSAGE_LEN=500 applies to EACH message item
INDIVIDUALLY (not to the final joined line), in BOTH variants --
larger than tier_echo's 80 (a validator message is typically longer
than a single model name), but still a finite ceiling -- an adversarial
guard against a giant field value ending up inside a violation message
via repr().

FAIL-OPEN (everywhere): any stdin-JSON parse failure, a non-dict
payload, a missing/non-string/non-journal file_path, a file that
doesn't open from disk -- all of these silently exit 0, neither channel
touched. One outer try/except around the whole of main() -- exit 0 on
ANY unexpected exception (the same principle as every hook in this
toolkit).
"""

import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import journal_validator  # noqa: E402
import tier_echo  # noqa: E402  -- TIER ECHO at write time (this port's extension):
# imports iter_transcript_models/count_models (the measurement, with its
# synthetic-line filter already built in) AND KNOWN_TIER_WORDS (the
# shared tier-word vocabulary), reused BY IMPORT, not copy-paste, the
# same principle as the journal_validator import above. journal_echo.py
# and tier_echo.py are DIFFERENT hooks (PostToolUse vs SubagentStop),
# but this cross-hook import is a deliberate, sanctioned exception to
# the general hook self-containment principle, alongside
# journal_validator.

JOURNAL_TAIL = ("logs", "routing-log.jsonl")
GIT_TIMEOUT_SECONDS = 5
MAX_MESSAGE_LEN = 500
MAX_HEAD_MESSAGES = 3

# --- TIER ECHO at write time (this port's extension) --------------------
# Trigger: a NEW journal line with an event in TIER_TRIGGER_EVENTS AND a
# worker_ref shaped like "agent:<id>" (id = [a-z0-9-]+, the WHOLE
# string -- fullmatch, not a prefix) -- only then is it worth looking
# for the subagent's transcript (a worker_ref like cli:.../retro:...
# does not reference a subagent file at all -- skipped without warning,
# see _collect_tier_events).
TIER_TRIGGER_EVENTS = {"delegated", "accepted", "rejected", "escalated"}
AGENT_WORKER_REF_RE = re.compile(r"^agent:([a-z0-9-]+)$")
# Ceiling on TIER ECHO lines per hook call -- independent of
# MAX_HEAD_MESSAGES (that one caps form-defect messages at 3; this one
# caps tier lines at 5, an independent axis).
MAX_TIER_LINES = 5


def _raw_sanitize(s: str, max_len: int = MAX_MESSAGE_LEN) -> str:
    """Control chars stripped and length capped at the same ceiling as
    _ascii_sanitize, but WITHOUT the ASCII replacement -- non-ASCII
    content (e.g. a validator message quoting a non-Latin field value)
    is left as-is. Used for the JSON additionalContext (the channel to
    the coordinator): json.dumps(ensure_ascii=True) itself escapes
    non-ASCII into safe \\uXXXX sequences on the wire, and after
    json.loads() on the reader's side the text comes back readable --
    an ASCII-replace pass here would be pure, needless degradation. It
    is needed only where text goes RAW (not JSON-escaped) into a
    console stream that may not be UTF-8, see _ascii_sanitize."""
    s = str(s).strip()
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    return s[:max_len]


def _ascii_sanitize(s: str, max_len: int = MAX_MESSAGE_LEN) -> str:
    """Local copy of the tools/tier_echo.py._ascii_sanitize approach
    (same principle: strip control chars, replace non-ASCII, cap
    length) -- a copy, not an import, see the module docstring. Used
    ONLY for the stderr duplicate (plain text, not JSON-escaped --
    written directly into this machine's console stream)."""
    s = str(s).strip()
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    s = s.encode("ascii", "replace").decode("ascii")
    return s[:max_len]


def _extract_file_path(payload: dict):
    """tool_input.file_path -- literally
    (`tool_input = payload.get("tool_input") or {}`; `.get("file_path")`),
    with no extra tool_name filter (see the module docstring,
    "TRIGGER")."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    return file_path if isinstance(file_path, str) and file_path else None


def _is_journal_path(file_path: str) -> bool:
    """Normalized path tail == ("logs", "routing-log.jsonl"),
    component-wise (not a substring check) -- matches both path
    separator styles ('/' and '\\\\')."""
    normalized = file_path.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p]
    return len(parts) >= 2 and tuple(parts[-2:]) == JOURNAL_TAIL


def _repo_root(file_path: str) -> Path:
    """Parent of the parent of file_path -- the directory containing
    logs/ (see the module docstring, literally)."""
    return Path(file_path).resolve().parent.parent


def _get_head_text(root: Path):
    """git -C <root> show HEAD:logs/routing-log.jsonl -- ONE call,
    timeout ~5s. Returns stdout when returncode==0, otherwise None (see
    the module docstring for the empirics of all three error forms --
    not a repo, the file isn't on HEAD, the directory doesn't exist --
    returncode is always non-zero; FileNotFoundError/TimeoutExpired --
    also None)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "show", "HEAD:logs/routing-log.jsonl"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _projects_root() -> Path:
    """The root directory under which finished subagents' transcripts
    live (expanduser'd). A separate function (not an inline
    Path.home()) EXCLUSIVELY so it can be monkeypatched in tests, the
    same testability pattern as _get_head_text/subprocess.run above:
    the module-level function is swapped out, this machine's real
    Path.home() never participates in tests."""
    return Path.home() / ".claude" / "projects"


def _find_agent_transcript(agent_id: str):
    """Globs <projects_root>/*/*/subagents/agent-<id>.jsonl (two
    wildcard levels -- project slug, session id -- matching the real
    on-disk layout for finished-subagent transcripts). The FIRST match
    (an agent id is unique machine-wide -- ordering of the glob doesn't
    matter). Not found / any glob error (permissions, a broken path) --
    None -- the caller then silently skips the line (no measurement, no
    verdict; not a warning). This is the flat layout specifically; a
    workflow-style tool's deeper nesting
    (subagents/workflows/wf_*/agent-*.jsonl) is a known, documented
    neighbor this does not cover."""
    try:
        matches = list(_projects_root().glob(f"*/*/subagents/agent-{agent_id}.jsonl"))
    except Exception:
        return None
    return str(matches[0]) if matches else None


def _extract_declared_word(model):
    """The first (in tier_echo.KNOWN_TIER_WORDS order -- haiku/sonnet/
    opus/fable) tier word occurring as a case-insensitive SUBSTRING of
    the journal line's `model` field. This is NOT the same as
    tier_echo._extract_declared_tier (which requires a strict
    "word:" prefix in a dispatch description) -- here the source is the
    free-text `model` field (a self-declared tier, free-form by
    design), compared the same way tier_echo.build_line compares
    (`declared_tier in model.lower()`).

    None if model isn't a string/is empty, or if NO known word occurs
    as a substring -- the same fail-open logic as elsewhere: with no
    recognizable declared tier, neither MISMATCH nor the informational
    branch applies (both depend on an identified tier word from the
    model field) -- the line is silently skipped, the same way "no
    transcript found" is. Practically safe: `model` is already a
    REQUIRED field for every event in TIER_TRIGGER_EVENTS in
    journal_validator (MODEL_REQUIRED_EVENTS) -- its absence/invalidity
    is already caught as a separate form defect regardless of this
    branch."""
    if not isinstance(model, str) or not model:
        return None
    model_lower = model.lower()
    for word in tier_echo.KNOWN_TIER_WORDS:
        if word in model_lower:
            return word
    return None


def _collect_tier_events(new_lines: list, head_lines: list) -> list:
    """For each NEW line (the same new_lines that main() computes for
    decide(), see the module docstring) with an event in
    TIER_TRIGGER_EVENTS and a worker_ref shaped like "agent:<id>" --
    looks up the subagent's transcript, measures its models
    (tier_echo.iter_transcript_models + count_models, synthetic filter
    included), compares against the declared tier word from the model
    field. Returns a list of tuples (line_no, kind, declared_word,
    counts) -- kind in ("mismatch", "info"); a "full match" (every
    measured model carries the word) adds nothing (complete silence on
    that line). line_no uses the SAME formula as
    journal_validator.validate_new_lines (len(head_lines)+idx+1) -- the
    same line numbers form defects use in their own messages.

    Fails open per line: any failure (malformed JSON, a glob error, a
    transcript read failure, anything) -- a try/except around the body
    of ONE iteration, `continue` -- does not interrupt parsing the rest
    of the new lines, does not crash the hook (main()'s outer boundary
    is a second, coarser net)."""
    events = []
    for idx, line in enumerate(new_lines):
        line_no = len(head_lines) + idx + 1
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                continue
            event = obj.get("event")
            if event not in TIER_TRIGGER_EVENTS:
                continue
            worker_ref = obj.get("worker_ref")
            if not isinstance(worker_ref, str):
                continue
            m = AGENT_WORKER_REF_RE.match(worker_ref)
            if not m:
                continue
            agent_id = m.group(1)
            transcript_path = _find_agent_transcript(agent_id)
            if not transcript_path:
                continue
            models = list(tier_echo.iter_transcript_models(transcript_path))
            counts = tier_echo.count_models(models)
            if not counts:
                continue
            declared_word = _extract_declared_word(obj.get("model"))
            if declared_word is None:
                continue
            matched = [declared_word in mdl.lower() for mdl in counts]
            if not any(matched):
                events.append((line_no, "mismatch", declared_word, counts))
            elif not all(matched):
                events.append((line_no, "info", declared_word, counts))
            # else: every measured model carries the word -- complete silence on this line.
        except Exception:
            continue
    return events


def _format_measured(counts: dict, ascii_only: bool) -> str:
    """"<model>=<count>[, ...]" -- the same shape as
    tier_echo.build_line, but the sanitizer is chosen by channel (raw
    for stdout, ascii for stderr), same principle as build_context
    below."""
    sanitize = _ascii_sanitize if ascii_only else _raw_sanitize
    return ", ".join(f"{sanitize(model)}={count}" for model, count in counts.items())


def _format_tier_line(event: tuple, ascii_only: bool) -> str:
    """Literal formats:
      MISMATCH: "TIER ECHO: line N model='<declared>' vs measured
                 <model>=<count>[, ...] MISMATCH"
      informational: "TIER ECHO: line N measured <model>=<count>[, ...]"
    The literal's static parts are NOT sanitized (same principle as
    build_context); only the dynamic parts are sanitized (declared_word
    is always one of the 4 ASCII tier words, so sanitizing it is a
    no-op here but applied for uniformity; the measured model names are
    real transcript text, sanitizing them is required, same risk as
    tier_echo.build_line)."""
    line_no, kind, declared_word, counts = event
    sanitize = _ascii_sanitize if ascii_only else _raw_sanitize
    measured = _format_measured(counts, ascii_only)
    if kind == "mismatch":
        return f"TIER ECHO: line {line_no} model='{sanitize(declared_word)}' vs measured {measured} MISMATCH"
    return f"TIER ECHO: line {line_no} measured {measured}"


def build_tier_segment(tier_events: list, ascii_only: bool = False) -> str:
    """Assembles the TIER ECHO part of additionalContext from
    tier_events (at most MAX_TIER_LINES=5 lines per call, "+K more" on
    top -- the same pattern as build_context for form defects, an
    independent ceiling). An empty tier_events -> "" (an empty string,
    not None -- the caller checks its truthiness the same way it checks
    the violations list)."""
    if not tier_events:
        return ""
    head = tier_events[:MAX_TIER_LINES]
    rest = len(tier_events) - len(head)
    body = "; ".join(_format_tier_line(ev, ascii_only) for ev in head)
    if rest > 0:
        body += f"; +{rest} more"
    return body


def build_context(violations: list, ascii_only: bool = False) -> str:
    """"JOURNAL ECHO: N defect(s) in new lines: <first 3 messages>[; +K
    more]" (the literal). The static English prefix/suffix is never
    passed through a sanitizer (in either mode -- see the module
    docstring, "OUTPUT").

    ascii_only=False (the default -- used for the JSON
    additionalContext, the channel to the coordinator): each message
    item goes through _raw_sanitize (control chars stripped, length
    capped, but non-ASCII content stays readable -- json.dumps(
    ensure_ascii=True) itself escapes non-ASCII on the wire, the reader
    sees readable text after json.loads(); an ASCII-replace pass here
    would be needless degradation).

    ascii_only=True (used ONLY for the stderr duplicate, plain text not
    JSON-escaped, this machine's console stream): each message item
    goes through _ascii_sanitize (non-ASCII -> '?')."""
    n = len(violations)
    sanitize = _ascii_sanitize if ascii_only else _raw_sanitize
    head = [sanitize(v) for v in violations[:MAX_HEAD_MESSAGES]]
    rest = n - len(head)
    body = "; ".join(head)
    if rest > 0:
        body += f"; +{rest} more"
    return f"JOURNAL ECHO: {n} defect(s) in new lines: {body}"


def combine_context(violations: list, tier_events: list, ascii_only: bool = False) -> str:
    """One JSON additionalContext can carry both form defects and TIER
    ECHO lines (joined by "; "). Two INDEPENDENT segments --
    build_context(violations) (as a whole, its own "JOURNAL ECHO: N
    defect(s)..." header unchanged) and build_tier_segment(tier_events)
    -- joined with "; ", only when non-empty. No form defects but tier
    lines present -> the result is just the tier segment, the JSON is
    still printed. Both empty -> "" -- the caller (main()) treats an
    empty string as complete silence (the same truthiness check that
    used to be `if not violations`)."""
    parts = []
    if violations:
        parts.append(build_context(violations, ascii_only))
    tier_segment = build_tier_segment(tier_events, ascii_only)
    if tier_segment:
        parts.append(tier_segment)
    return "; ".join(parts)


def _reconfigure_streams_utf8():
    """The static text (see build_context) goes on BOTH channels --
    without an explicit reconfigure, this machine's default stdout/
    stderr encoding may not be UTF-8, and a subprocess smoke can hit a
    UnicodeDecodeError on the reading parent's side otherwise. The same
    pattern as tools/hygiene_gate.py._reconfigure_stdout_utf8 and
    tools/dod_track.py._reconfigure_stderr_utf8 -- here BOTH channels
    need it (this hook writes to both), a copy, not an import (see the
    module docstring)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    _reconfigure_streams_utf8()
    try:
        raw_bytes = sys.stdin.buffer.read()
        raw = raw_bytes.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            return 0
        if not isinstance(payload, dict):
            return 0

        file_path = _extract_file_path(payload)
        if not file_path or not _is_journal_path(file_path):
            return 0

        try:
            disk_text = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0

        root = _repo_root(file_path)
        now = datetime.datetime.now()
        head_text = _get_head_text(root)

        _, violations = journal_validator.decide(disk_text, head_text, now)

        # TIER ECHO at write time (this port's extension): the same
        # "new lines" that decide() validates internally -- computed
        # INDEPENDENTLY via the same public split_lines/
        # check_append_only (decide() does not expose new_lines
        # itself). append-only NOT holding -- staged doesn't start with
        # HEAD as a prefix -- means "new lines" can't be determined by
        # a slice (staged may mix edited/deleted old lines) -- tier
        # events are not counted at all in that case (new_lines = []),
        # that defect (append-only) is already covered separately via
        # violations.
        staged_lines = journal_validator.split_lines(disk_text)
        head_lines = journal_validator.split_lines(head_text)
        append_ok, _ = journal_validator.check_append_only(staged_lines, head_lines)
        new_lines = staged_lines[len(head_lines):] if append_ok else []
        tier_events = _collect_tier_events(new_lines, head_lines)

        if not violations and not tier_events:
            return 0

        context_for_stdout = combine_context(violations, tier_events, ascii_only=False)
        context_for_stderr = combine_context(violations, tier_events, ascii_only=True)

        sys.stderr.write(context_for_stderr + "\n")
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": context_for_stdout,
            }
        }
        # ensure_ascii=True: the coordinator receives UTF-8-safe JSON --
        # non-ASCII is escaped to \uXXXX on the wire (json.dumps does
        # this itself), the reader recovers readable text via
        # json.loads(). This makes the standard call safe even without
        # a stream reconfigure -- the reconfigure is kept regardless, as
        # protection for the stderr channel.
        sys.stdout.write(json.dumps(output, ensure_ascii=True) + "\n")
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
