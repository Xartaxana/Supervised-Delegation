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

WITNESS ECHO at write time (this port's second extension): cross-checks
the `witness` field of a NEW `accepted`+agent=builder journal line
against the runs actually OBSERVED in the current session's own DoD
track (.claude/dod_track/<session_id>.json, written by
tools/dod_track.py -- read here only, by a LOCAL copy of its track-path
formula, never imported: the same hook self-containment principle this
file's module docstring already documents for _raw_sanitize/
_ascii_sanitize; journal_validator and tier_echo stay the only declared
import exceptions). Trigger: in the SAME new_lines/head_lines that TIER
ECHO already computes above, a line with event=="accepted",
agent=="builder", and a non-empty `witness` string.

Outcomes (per matching line):
 - notes contains "retroactive" -> silent (a retro-accepted witness is
   not comparable to the current session's own track by definition).
 - the current session's track is empty/unreadable (no file, empty
   file, broken JSON, not an object, "runs" missing/not a list) ->
   silent (nothing to compare against; not a violation).
 - the track is non-empty but NONE of its distinct normalized commands
   occur as a substring of the normalized witness text -> a soft
   warning (legitimate for a batch/cross-session/retro acceptance --
   verify manually).
 - a track command DOES occur in the witness text, and that command's
   LATEST run (by ts) was recorded "red" -> a loud warning naming the
   command and its last-red ts, once per such command.
 - a track command occurs in the witness text and its latest run was
   "green" -> complete silence on that line (same principle as TIER
   ECHO's "every measured model carries the word").
Normalization (for both the track command and the witness text, before
the substring check): every run of whitespace collapsed to one space
plus a strip -- so a witness text reflowed/wrapped differently from
the exact command still matches.

Ceiling: at most MAX_WITNESS_LINES=5 visible (warn_soft/warn_loud)
lines per hook call, "+K more" on top -- the same independent-axis
ceiling pattern as MAX_TIER_LINES, guarding the same head_text=None
("new_lines = the whole file") scenario. The track is read lazily and
at most ONCE per hook call (session_id is shared by every line in one
PostToolUse event).

This extension shares main()'s outer try/except AND has its own local
try/except around the collection call, so a failure inside the
witness cross-check can never take down TIER ECHO or the form-defect
check running alongside it in the same call.
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

# --- WITNESS ECHO at write time (this port's second extension) ---------
WITNESS_TRIGGER_EVENT = "accepted"
WITNESS_TRIGGER_AGENT = "builder"
# Ceiling on VISIBLE WITNESS ECHO lines per hook call -- independent
# axis from MAX_HEAD_MESSAGES (3) and MAX_TIER_LINES (5); same class of
# ceiling, same rationale (head_text=None makes new_lines the whole
# file -- an unbounded additionalContext otherwise).
MAX_WITNESS_LINES = 5
# Silent-note literals (never printed -- see build_witness_segment):
# returned from _collect_witness_events purely for testability of the
# outcome lattice.
NOTE_RETRO = "retro accepted - track incomparable"
NOTE_TRACK_EMPTY = "track empty/unreadable - witness incomparable"


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


def combine_context(violations: list, tier_events: list, witness_events: list = None,
                     fallback_marker: str = "", ascii_only: bool = False) -> str:
    """One JSON additionalContext can carry form defects, TIER ECHO
    lines, WITNESS ECHO lines, and (t-277/t-279, ported from HQ) a
    fallback-base marker, joined by "; ". FOUR INDEPENDENT segments --
    build_context(violations) (as a whole, its own "JOURNAL ECHO: N
    defect(s)..." header unchanged), build_tier_segment(tier_events),
    build_witness_segment(witness_events), and fallback_marker -- joined
    with "; ", only when non-empty. Any subset empty -> the result is
    just the remaining non-empty segments, the JSON is still printed as
    long as at least one segment is non-empty. All empty -> "" -- the
    caller (main()) treats an empty string as complete silence.

    fallback_marker -- a LITERAL (FALLBACK_MARKER_TEXT, see the
    "PAYLOAD-SCOPED ECHO BASE" section below), never sanitized (a
    static ASCII string, never third-party text -- same principle as
    build_context's static prefix). main() passes it as an empty
    string whenever TIER ECHO/WITNESS ECHO did NOT degrade to the
    HEAD-diff fallback on this particular hook call (see
    _resolve_echo_base) -- so its absence in the old 2-/3-positional
    call forms changes nothing.

    witness_events=None (default, NOT []) preserves the old 2-positional
    call form combine_context(violations, tier_events) byte-for-byte:
    a None witness_events segment is "" exactly like an empty list, so
    every existing call/test using the short form is unaffected.
    fallback_marker="" (default) is the same story -- it never adds a
    segment unless explicitly passed."""
    parts = []
    if violations:
        parts.append(build_context(violations, ascii_only))
    tier_segment = build_tier_segment(tier_events, ascii_only)
    if tier_segment:
        parts.append(tier_segment)
    witness_segment = build_witness_segment(witness_events or [], ascii_only)
    if witness_segment:
        parts.append(witness_segment)
    if fallback_marker:
        parts.append(fallback_marker)
    return "; ".join(parts)


# --- PAYLOAD-SCOPED ECHO BASE (t-277/t-279, ported from HQ) -------------
# ROOT CAUSE / FIX / EMPIRICAL BASIS: identical to HQ's tools/
# journal_echo.py (same section header there) -- TIER ECHO/WITNESS ECHO
# shared ONE base with VALIDATION (HEAD-diff, cumulative across every
# PostToolUse call since the last commit), so a session appending lines
# across several tool calls without committing between them re-echoed
# the SAME already-reported event on every later call. The fix: derive
# the "new lines" base from the CURRENT tool call's OWN payload
# (tool_response.originalFile, empirically confirmed on BOTH Edit's and
# Write's Zod output schemas in the installed claude-code binary -- the
# full file content immediately BEFORE this specific tool call, string
# or null). DEFERRAL (t-277/t-279, builder finding: the given context
# manifest expected an existing "no ts-drift layer" deferral note
# elsewhere in this module's docstring to preserve -- none was found on
# inspection; this line IS that deferral note, stated here since there
# wasn't a prior one): this port carries NO ts-drift layer and this
# task does NOT add one -- this section only affects TIER ECHO/WITNESS
# ECHO here, unlike HQ's tools/journal_echo.py where the identical base
# change also fixes a TS DRIFT correctness bug.
#
# FAIL-OPEN: tool_name outside {"Edit", "Write"}, a missing/malformed
# tool_response, an absent/wrongly-typed "originalFile" key, OR a
# recovered originalFile that disk_text does NOT extend as a strict
# append (a non-tail edit) -- ALL fall back to the SAME HEAD-diff
# computation this file used before this port (identical logic,
# unchanged) -- see _resolve_echo_base. The fallback is disclosed via
# FALLBACK_MARKER_TEXT, appended as combine_context's fourth segment --
# but ONLY when there is already something else to report (see main()):
# an otherwise-fully-clean call stays completely silent even in
# fallback, matching this file's pre-existing "no noise on a clean
# write" contract.
_ORIGINAL_FILE_UNAVAILABLE = object()
EDIT_LIKE_TOOL_NAMES = ("Edit", "Write")
FALLBACK_MARKER_TEXT = "echo base: HEAD-diff fallback"


def _extract_original_file(payload, tool_name):
    """tool_response.originalFile -- see the section docstring above.
    Returns _ORIGINAL_FILE_UNAVAILABLE when tool_name isn't Edit/Write,
    or tool_response isn't a dict, or the "originalFile" key is absent,
    or present with a type that's neither str nor None; "" when
    originalFile is None (a brand-new file); the string itself
    otherwise."""
    if tool_name not in EDIT_LIKE_TOOL_NAMES:
        return _ORIGINAL_FILE_UNAVAILABLE
    tool_response = payload.get("tool_response") if isinstance(payload, dict) else None
    if not isinstance(tool_response, dict):
        return _ORIGINAL_FILE_UNAVAILABLE
    if "originalFile" not in tool_response:
        return _ORIGINAL_FILE_UNAVAILABLE
    original_file = tool_response["originalFile"]
    if original_file is None:
        return ""
    if not isinstance(original_file, str):
        return _ORIGINAL_FILE_UNAVAILABLE
    return original_file


def _resolve_echo_base(payload, tool_name, staged_lines: list, head_lines: list):
    """Returns (echo_base_lines, echo_new_lines, used_fallback) -- the ONE
    base shared by TIER ECHO/WITNESS ECHO in this port (VALIDATION/
    JOURNAL ECHO stays on the separate, cumulative HEAD-diff base -- see
    main()). See the section docstring above for the primary/fallback
    logic (identical to HQ's tools/journal_echo.py)."""
    original_file = _extract_original_file(payload, tool_name)
    if original_file is not _ORIGINAL_FILE_UNAVAILABLE:
        base_lines = journal_validator.split_lines(original_file)
        op_ok, _ = journal_validator.check_append_only(staged_lines, base_lines)
        if op_ok:
            return base_lines, staged_lines[len(base_lines):], False
    append_ok, _ = journal_validator.check_append_only(staged_lines, head_lines)
    new_lines = staged_lines[len(head_lines):] if append_ok else []
    return head_lines, new_lines, True


# ---------------------------------------------------------------------
# WITNESS ECHO at write time (this port's second extension) -- pure logic
# ---------------------------------------------------------------------


def _normalize_ws(s) -> str:
    """Collapses every run of whitespace (space/tab/newline) into a
    single space, then strips. Applied to BOTH the track's command
    string and the witness text before the substring comparison (a
    witness reflowed across lines still matches the recorded command).
    A non-string input -> "" (a safe default that never matches
    anything by substring)."""
    if not isinstance(s, str):
        return ""
    return re.sub(r"\s+", " ", s).strip()


def _witness_track_path(cwd, session_id) -> Path:
    """.claude/dod_track/<session_id>.json under the calling session's
    cwd -- the SAME formula tools/dod_track.py uses for its own track
    file, reproduced locally (read-only) rather than imported: the
    hook self-containment principle this module's docstring already
    explains for _raw_sanitize/_ascii_sanitize. The track file's shape
    is a documented, stable contract between this toolkit's hooks, not
    an internal implementation detail of dod_track.py."""
    return Path(cwd or ".") / ".claude" / "dod_track" / f"{session_id}.json"


def _load_witness_runs(cwd, session_id):
    """Reads the current session's track "runs" list. Returns a list
    (possibly empty) on a successful read of a valid JSON object
    carrying a "runs" list field; None on ANY failure -- session_id not
    a non-empty string, no file, an empty/whitespace-only file, broken
    JSON, JSON not an object, or "runs" missing/not a list. The caller
    (_collect_witness_events) treats both None and an empty list the
    same way: "track empty/unreadable" -- there is nothing to compare
    the witness against either way."""
    if not isinstance(session_id, str) or not session_id:
        return None
    path = _witness_track_path(cwd, session_id)
    try:
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return None
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        runs = data.get("runs")
        if not isinstance(runs, list):
            return None
        return runs
    except Exception:
        return None


def _group_runs_by_normalized_command(runs: list) -> dict:
    """{normalized_command: [(ts, outcome), ...]} over EVERY run in the
    track, of ANY agent_id (a builder subagent's run lives in the same
    <session_id>.json as the main thread's -- agent_id is not filtered
    here at all). A run with no usable command string (missing/empty
    after normalization) is skipped -- nothing to compare. A non-dict
    run entry (a corrupted track) is skipped silently. Grouping by
    DISTINCT command, not by individual run, keeps the later substring
    check to one probe per distinct command rather than one per run
    (a track with many repeats of the same verification command is the
    common case)."""
    groups: dict = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        norm = _normalize_ws(run.get("command"))
        if not norm:
            continue
        groups.setdefault(norm, []).append((run.get("ts"), run.get("outcome")))
    return groups


def _last_by_ts(entries: list):
    """The (ts, outcome) entry with the MAX ts among entries (a list of
    (ts, outcome) pairs, the shape _group_runs_by_normalized_command
    produces). dod_track.py's ts values are fixed-width ISO with
    microseconds, so plain string sorting is equivalent to chronological
    sorting here -- cheaper than parsing a real datetime for this
    purpose. A non-string/missing ts sorts as "" (a safe minimum that
    never wins "latest" over a real timestamp, without breaking the
    sort of the rest)."""
    def key(e):
        ts = e[0]
        return ts if isinstance(ts, str) else ""
    return sorted(entries, key=key)[-1]


def _match_witness(witness: str, runs: list):
    """For every DISTINCT normalized track command occurring as a
    substring of the normalized witness text, looks up that command's
    LATEST (by ts) run -- a "red" latest run is a candidate for a loud
    warning (outcome is a secondary signal here: determine_outcome's
    own safe default is "red" on an ambiguous run, so a red/green split
    alone does not yet mean "the witness lies" -- hence a WARN, never a
    hard block). Returns (matched_any: bool, loud: list[(cmd, ts)]).
    matched_any=False means the track was non-empty but no command in
    it occurs in the witness text at all -- the soft-warning case (see
    _collect_witness_events).

    Performance: exactly one substring probe per DISTINCT command in
    the track (after grouping), not one per individual run -- a track
    with hundreds of repeats of the same verification command collapses
    to one "in" check, not hundreds."""
    norm_witness = _normalize_ws(witness)
    groups = _group_runs_by_normalized_command(runs)
    matched_any = False
    loud = []
    for cmd, entries in groups.items():
        if cmd in norm_witness:
            matched_any = True
            ts, outcome = _last_by_ts(entries)
            if outcome == "red":
                loud.append((cmd, ts))
    return matched_any, loud


def _collect_witness_events(new_lines: list, head_lines: list, payload: dict) -> list:
    """For each NEW line (the same new_lines TIER ECHO already uses
    above) with event=="accepted", agent=="builder", and a non-empty
    `witness` string -- the outcome lattice:

      1. notes contains "retroactive" -> ("note", line_no, NOTE_RETRO):
         a retro-accepted witness is not comparable to the CURRENT
         session's own track by definition -- silent.
      2. the current session's track is empty/unreadable (see
         _load_witness_runs) -> ("note", line_no, NOTE_TRACK_EMPTY) --
         silent, not an exception.
      3. no track command occurs in the witness (matched_any=False) ->
         ("warn_soft", line_no) -- legitimate for a batch/cross-session/
         retro acceptance (verify manually).
      4. a matching command whose LATEST run was red -> ("warn_loud",
         line_no, command, ts), one entry per such command.
      5. otherwise (matched, latest run green) -> nothing added --
         complete silence on that line (same principle as TIER ECHO's
         "every measured model carries the word").

    "note" events are NEVER printed (see build_witness_segment) --
    returned alongside warn events purely so the outcome lattice is
    directly testable.

    Fails open per line (same pattern as _collect_tier_events): any
    failure (malformed JSON, anything else) -- try/except around the
    body of ONE iteration, `continue` -- does not interrupt the rest
    of the new lines.

    The track is read LAZILY and AT MOST ONCE per hook call (session_id
    is shared across every line of one PostToolUse event) -- the same
    "read once" performance principle the module docstring documents
    for disk_text/git in main()."""
    events = []
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    runs_loaded = False
    runs_cache = None
    for idx, line in enumerate(new_lines):
        line_no = len(head_lines) + idx + 1
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                continue
            if obj.get("event") != WITNESS_TRIGGER_EVENT:
                continue
            if obj.get("agent") != WITNESS_TRIGGER_AGENT:
                continue
            witness = obj.get("witness")
            if not isinstance(witness, str) or not witness.strip():
                continue

            notes = obj.get("notes")
            if isinstance(notes, str) and "retroactive" in notes:
                events.append(("note", line_no, NOTE_RETRO))
                continue

            if not runs_loaded:
                runs_cache = _load_witness_runs(cwd, session_id)
                runs_loaded = True
            if not runs_cache:
                events.append(("note", line_no, NOTE_TRACK_EMPTY))
                continue

            matched_any, loud = _match_witness(witness, runs_cache)
            if not matched_any:
                events.append(("warn_soft", line_no))
            else:
                for cmd, ts in loud:
                    events.append(("warn_loud", line_no, cmd, ts))
        except Exception:
            continue
    return events


def _format_witness_line(event: tuple, ascii_only: bool) -> str:
    """Static ASCII prefix "WITNESS ECHO: line N ..." plus dynamic
    content (command name, ts) run through the channel's sanitizer --
    same principle as _format_tier_line. ts from the track is dynamic
    too (a third-party JSON file's field value, not a literal of this
    module) and is sanitized symmetrically with cmd -- the "every
    dynamic part is sanitized" invariant this file already applies to
    _format_tier_line/_format_measured. In practice dod_track's
    _now_iso() output is always clean ASCII with no control chars, so
    sanitizing it here is a no-op in the ordinary case -- it exists to
    close the adversarial edge (a corrupted/foreign track with control
    chars or a giant ts value)."""
    sanitize = _ascii_sanitize if ascii_only else _raw_sanitize
    kind = event[0]
    line_no = event[1]
    if kind == "warn_loud":
        _, _, cmd, ts = event
        return (f"WITNESS ECHO: line {line_no} contradiction - command "
                f"'{sanitize(cmd)}' recorded RED in session track (last red at {sanitize(str(ts))})")
    # warn_soft
    return (f"WITNESS ECHO: line {line_no} witness command(s) not observed in "
            "session track (batch/cross-session/retro acceptance legitimate - verify manually)")


def build_witness_segment(witness_events: list, ascii_only: bool = False) -> str:
    """Assembles the WITNESS ECHO part of additionalContext -- ONLY
    from "warn_loud"/"warn_soft" events ("note" events are silent by
    definition, see _collect_witness_events); ceiling MAX_WITNESS_LINES
    (=5, boundary-tested at 5/6), same "+K more" pattern as
    build_tier_segment. An empty visible-events list -> "" (the caller
    treats an empty string as "no segment", same principle as
    build_tier_segment)."""
    warn_events = [e for e in witness_events if e[0] in ("warn_loud", "warn_soft")]
    if not warn_events:
        return ""
    head = warn_events[:MAX_WITNESS_LINES]
    rest = len(warn_events) - len(head)
    body = "; ".join(_format_witness_line(e, ascii_only) for e in head)
    if rest > 0:
        body += f"; +{rest} more"
    return body


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

        # VALIDATION -- the cumulative HEAD-diff base, unchanged by
        # t-277/t-279: historical uncommitted lines' FORM still needs
        # catching before commit regardless of which specific tool call
        # is running now.
        _, violations = journal_validator.decide(disk_text, head_text, now)

        # ECHO LAYERS (TIER ECHO/WITNESS ECHO, t-277/t-279): ONE
        # payload-scoped base shared by both collectors (see
        # _resolve_echo_base/the "PAYLOAD-SCOPED ECHO BASE" section
        # above) -- replaces the old HEAD-diff base these two layers
        # used to share with VALIDATION (root cause: that base is
        # cumulative between commits, so every call re-echoed every
        # uncommitted line, not just the one THIS call added).
        staged_lines = journal_validator.split_lines(disk_text)
        head_lines = journal_validator.split_lines(head_text)
        tool_name = payload.get("tool_name")
        echo_base_lines, echo_new_lines, used_fallback = _resolve_echo_base(
            payload, tool_name, staged_lines, head_lines)

        tier_events = _collect_tier_events(echo_new_lines, echo_base_lines)

        # WITNESS ECHO at write time (this port's second extension --
        # see the module docstring): the SAME payload-scoped base as
        # TIER ECHO above. A second, outer try/except here (on top of
        # the per-line one inside _collect_witness_events itself) means
        # a failure in this cross-check can never take down JOURNAL
        # ECHO/TIER ECHO.
        try:
            witness_events = _collect_witness_events(echo_new_lines, echo_base_lines, payload)
        except Exception:
            witness_events = []
        # "note" events (retro / empty track) never make a line visible
        # -- only warn_loud/warn_soft trigger printing.
        witness_visible = any(e[0] != "note" for e in witness_events)

        if not violations and not tier_events and not witness_visible:
            return 0

        # Fallback marker (t-277/t-279): visible ONLY when we're already
        # printing something else -- an otherwise fully clean call stays
        # silent even in fallback (see the section docstring above).
        fallback_marker = FALLBACK_MARKER_TEXT if used_fallback else ""

        context_for_stdout = combine_context(violations, tier_events, witness_events,
                                              fallback_marker, ascii_only=False)
        context_for_stderr = combine_context(violations, tier_events, witness_events,
                                              fallback_marker, ascii_only=True)

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
