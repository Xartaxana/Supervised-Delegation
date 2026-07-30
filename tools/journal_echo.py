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

Ceiling: at most MAX_WITNESS_LINES=5 visible (warn_soft/warn_loud/
warn_stale) lines per hook call, "+K more" on top -- the same
independent-axis ceiling pattern as MAX_TIER_LINES, guarding the same
head_text=None ("new_lines = the whole file") scenario. The track is
read lazily and at most ONCE per hook call (session_id is shared by
every line in one PostToolUse event).

This extension shares main()'s outer try/except AND has its own local
try/except around the collection call, so a failure inside the
witness cross-check can never take down TIER ECHO or the form-defect
check running alongside it in the same call.

WITNESS ECHO STALENESS (ported from HQ, this batch): a SECOND,
INDEPENDENT axis on top of the outcome lattice above -- a witness can
honestly cite a command whose LATEST run was green (outcome 5, silent
on that axis) and the session's track can STILL carry a code edit
LATER than that green run with no re-run since -- the same invariant
tools/dod_gate.py.evaluate() already enforces at SubagentStop ("the
last edit is before the last green run"), checked again here, at
write time, over the WHOLE session track (any agent_id, not just the
one filed on this journal line). Trigger: the track carries at least
one non-doc-only edit (`.claude/dod_track/<session_id>.json`'s "edits"
list, read the same lazy-once-per-call way as "runs") AND either no
green run exists at all, or the latest edit's ts is strictly later
than the latest green run's ts -- ("warn_stale", line_no, last_edit_ts,
last_green_ts_or_None), independent of (and additional to) whichever
of outcomes 3/4/5 above the SAME line also produced: a line can be
BOTH "matched, latest green" (silent on the command-match axis) AND
"warn_stale" (loud on the staleness axis) at the same time.

Doc-only edits (.md/.json/.jsonl, plus .gitignore/.gitattributes/
.editorconfig -- the SAME extension list tools/dod_gate.py's own
doc-only exemption uses, mirrored here as a local copy, not imported)
are EXCLUDED from "last edit" -- without this exemption, the Edit/
Write call that writes THIS accepted line into routing-log.jsonl
(itself a .jsonl file) would make itself its own "latest edit",
falsely staling every batched accepted line. An edit record with no
file_path (an old track, or a payload missing the field) is
conservatively treated as NOT doc-only (counted toward "last edit") --
missing information does not earn an exemption, the same fail-safe
default this whole file already applies elsewhere.

TS DRIFT ECHO at write time (ported from HQ, this batch): the third
independent echo layer, closing a gap discipline alone was carrying
(F-29): "ts is read from the system clock immediately before writing,
never narrated" is checked at COMMIT time by journal_validator (a
monotonicity + "not more than 10 minutes in the future" rule), but by
commit time an event is already legitimately old (D-0079 batch
cadence: events accumulate in session memory and are written to disk
in one block at the end of a stage, the commit can follow hours
later) -- drift-from-the-clock-right-now is not meaningfully
checkable at commit time at all. The one moment where "is this ts
fresh against the clock RIGHT NOW" is meaningful is the moment the
line lands on disk -- this hook's own invocation -- so this check
lives HERE, not in journal_validator. Two independent thresholds (own
engineering decision, same class as MAX_TIER_LINES/MAX_WITNESS_LINES):
TS_FUTURE_TOLERANCE_SECONDS=120 (2 minutes -- headroom for ordinary
process jitter between reading the clock and this hook actually
running, well under journal_validator's own 10-minute hard limit, so
this layer warns EARLIER and on SMALLER drift than the hard gate) and
TS_STALE_TOLERANCE_SECONDS=1800 (30 minutes -- headroom for a
LEGITIMATE batch: the ts is read once "immediately before writing the
BATCH", and the batch itself may have sat in session memory for a
while before the actual disk write; half an hour is the rough order
of magnitude of one work stage -- a drift LARGER than that suggests
the ts was not, in fact, read from the clock right before writing,
which is worth flagging even under batch discipline). Both thresholds
are strict (`>`, not `>=`) -- exactly at the boundary stays silent.
Ceiling: MAX_TS_DRIFT_LINES=5 lines per hook call, "+K more" on top --
the same class of ceiling as MAX_TIER_LINES/MAX_WITNESS_LINES, guarding
the same head_text=None ("new_lines = the whole file") scenario;
without it, one missed git init on a repo with a journal already
hundreds of lines long would blow additionalContext up to hundreds of
TS DRIFT lines in one call. Warn-only, always visible (no silent
"note" branch, unlike WITNESS ECHO).

Both new layers share the payload-scoped echo base with TIER ECHO/
WITNESS ECHO (see "PAYLOAD-SCOPED ECHO BASE" below) -- a line already
evaluated by an earlier hook call is never re-evaluated by a later
one, for staleness OR for ts drift.
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

# --- WITNESS ECHO STALENESS (ported from HQ, this batch) ---------------
# Mirror of tools/dod_gate.py.DOC_ONLY_EXTENSIONS/DOC_ONLY_DOTFILES --
# the SAME list, a LOCAL copy (not an import -- dod_gate.py stays
# outside this hook's self-containment boundary, the same principle the
# module docstring already applies to _raw_sanitize/_ascii_sanitize).
# A divergence between this list and dod_gate.py's own is its own class
# of pair defect (fix the class, not the instance) -- editing either
# list edits both in the same move.
DOC_ONLY_EXTENSIONS = {".md", ".json", ".jsonl"}
DOC_ONLY_DOTFILES = {".gitignore", ".gitattributes", ".editorconfig"}


def _is_doc_only_edit_path(file_path) -> bool:
    """Mirror of tools/dod_gate.py._is_doc_only_file -- the same logic:
    an unknown/empty/non-string file_path -> False (conservatively NOT
    doc-only -- missing information does not earn an exemption from
    "code edit", the same fail-safe principle dod_gate/dod_track
    already apply for their own doc-only/scratchpad exemptions); a
    dotfile in DOC_ONLY_DOTFILES -> True; otherwise the extension
    (case-insensitive) in DOC_ONLY_EXTENSIONS. .jsonl is in this list --
    covers BOTH logs/routing-log.jsonl itself (the very Edit/Write call
    writing THIS accepted line would otherwise stale itself) and any
    other .jsonl anywhere in the repo, with no separate, narrower
    "journal-specific" criterion needed."""
    if not isinstance(file_path, str) or not file_path:
        return False
    path = Path(file_path)
    if path.name.lower() in DOC_ONLY_DOTFILES:
        return True
    return path.suffix.lower() in DOC_ONLY_EXTENSIONS


# --- TS DRIFT ECHO at write time (ported from HQ, this batch) ----------
# See the module docstring, "TS DRIFT ECHO at write time", for the full
# motivation and threshold rationale.
TS_FUTURE_TOLERANCE_SECONDS = 120
TS_STALE_TOLERANCE_SECONDS = 1800
# Ceiling on VISIBLE TS DRIFT ECHO lines per hook call -- symmetric with
# MAX_TIER_LINES/MAX_WITNESS_LINES above (the same three-collector
# class, the same head_text=None/new-lines-is-the-whole-file risk).
MAX_TS_DRIFT_LINES = 5


def _detect_ts_drift(ts, now: "datetime.datetime"):
    """Returns ("future", delta_seconds) | ("stale", delta_seconds) |
    None for one `ts` field value. Parsing is REUSED
    (journal_validator.parse_ts), not duplicated -- the same
    ISO-without-timezone format the validator already parses for its
    own rule 10. An unparseable/missing ts -> None -- fail-open: ts
    FORM is already caught separately as a form defect by
    journal_validator/JOURNAL ECHO, this layer doesn't duplicate that
    diagnosis.

    `now` is the same naive local datetime.datetime.now() as the
    journal's own ts convention (ISO, local time, no timezone) -- both
    sides of the comparison are naive, an aware/naive conflict is not
    possible.

    Thresholds are strict (`>`), not (`>=`) -- exactly at the boundary
    stays silent, symmetric for both future and stale."""
    parsed = journal_validator.parse_ts(ts) if isinstance(ts, str) else None
    if parsed is None:
        return None
    delta = (parsed - now).total_seconds()
    if delta > TS_FUTURE_TOLERANCE_SECONDS:
        return ("future", delta)
    stale_delta = -delta
    if stale_delta > TS_STALE_TOLERANCE_SECONDS:
        return ("stale", stale_delta)
    return None


def _collect_ts_drift_events(new_lines: list, head_lines: list, now: "datetime.datetime") -> list:
    """For EVERY new line (the same new_lines/head_lines TIER ECHO/
    WITNESS ECHO already use) with a parseable `ts` field --
    _detect_ts_drift. Per-line (not deduplicated by ts value -- several
    lines of one batch sharing an identical ts each produce their OWN
    independent result). Returns a list of (line_no, kind,
    delta_seconds).

    Fails open per line (the same pattern as _collect_tier_events/
    _collect_witness_events): a broken line's JSON -- try/except with
    `continue`, does not interrupt parsing the rest, does not crash the
    hook."""
    events = []
    for idx, line in enumerate(new_lines):
        line_no = len(head_lines) + idx + 1
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                continue
            result = _detect_ts_drift(obj.get("ts"), now)
            if result is None:
                continue
            kind, delta = result
            events.append((line_no, kind, delta))
        except Exception:
            continue
    return events


def _format_ts_drift_line(event: tuple) -> str:
    """Static ASCII literal + minimal dynamic content -- the same
    principle _format_tier_line/_format_witness_line already apply in
    this file. "line {N}" distinguishes several events of one batch
    sharing an identical ts when joined with "; " -- the same local
    pattern TIER ECHO/WITNESS ECHO already carry ("line N"). The only
    dynamic content here is integers (line_no, rounded drift seconds) --
    ASCII by construction, no sanitizer needed (unlike
    _format_witness_line, which interpolates real third-party track
    text)."""
    line_no, kind, delta = event
    seconds = int(round(abs(delta)))
    if kind == "future":
        return (f"TS DRIFT: line {line_no} event ts is {seconds}s in the FUTURE "
                 "(F-29: ts must be read from the system clock immediately before writing)")
    return (f"TS DRIFT: line {line_no} event ts is {seconds}s STALE "
            "(D-0079: batch ts must still be read from the system clock right "
            "before writing the batch, not carried over from an earlier check)")


def build_ts_drift_segment(ts_drift_events: list, ascii_only: bool = False) -> str:
    """Assembles the TS DRIFT part of additionalContext -- joined with
    "; ", ceiling MAX_TS_DRIFT_LINES=5 lines per call with a "+K more"
    tail on top (the same pattern as build_tier_segment/
    build_witness_segment). ascii_only is accepted for signature
    uniformity with the other build_* functions and combine_context, but
    is actually a no-op here -- _format_ts_drift_line never inserts
    anything but integers, so there is no non-ASCII content to sanitize
    in either mode.

    An empty ts_drift_events -> "" (the caller treats an empty string as
    "no segment", same principle as the other build_* functions)."""
    if not ts_drift_events:
        return ""
    head = ts_drift_events[:MAX_TS_DRIFT_LINES]
    rest = len(ts_drift_events) - len(head)
    body = "; ".join(_format_ts_drift_line(ev) for ev in head)
    if rest > 0:
        body += f"; +{rest} more"
    return body


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
                     ts_drift_events: list = None, escalation_events: list = None,
                     fallback_marker: str = "", ascii_only: bool = False) -> str:
    """One JSON additionalContext can carry form defects, TIER ECHO
    lines, WITNESS ECHO lines, TS DRIFT lines, ESCALATION lines (ported
    from HQ, batch B6), and a fallback-base marker, joined by "; ". SIX
    INDEPENDENT segments -- build_context(violations) (as a whole, its
    own "JOURNAL ECHO: N defect(s)..." header unchanged),
    build_tier_segment(tier_events), build_witness_segment(
    witness_events), build_ts_drift_segment(ts_drift_events),
    build_escalation_segment(escalation_events), and fallback_marker --
    joined with "; ", only when non-empty. Any subset empty -> the
    result is just the remaining non-empty segments, the JSON is still
    printed as long as at least one segment is non-empty. All empty ->
    "" -- the caller (main()) treats an empty string as complete
    silence.

    fallback_marker -- a LITERAL (FALLBACK_MARKER_TEXT, see the
    "PAYLOAD-SCOPED ECHO BASE" section below), never sanitized (a
    static ASCII string, never third-party text -- same principle as
    build_context's static prefix). main() passes it as an empty
    string whenever TIER ECHO/WITNESS ECHO/TS DRIFT/ESCALATION did NOT
    degrade to the HEAD-diff fallback on this particular hook call (see
    _resolve_echo_base) -- so its absence in the old 2-/3-/4-positional
    call forms changes nothing.

    witness_events=None / ts_drift_events=None / escalation_events=None
    (default, NOT []) preserve every older call form (combine_context(
    violations, tier_events) through the 4-positional combine_context(
    violations, tier_events, witness_events, ts_drift_events))
    byte-for-byte: a None segment is "" exactly like an empty list, so
    every existing call/test using a shorter form is unaffected.
    escalation_events is added as a NEW 5th positional parameter
    (BEFORE fallback_marker, which shifts to 6th place) -- no existing
    call site in this repo passes fallback_marker positionally (only
    2-4 positional arguments, or a keyword call -- grepped across every
    test_journal_echo*.py before this edit), so the shift breaks
    nothing except main() below, which this same task updates.
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
    ts_drift_segment = build_ts_drift_segment(ts_drift_events or [], ascii_only)
    if ts_drift_segment:
        parts.append(ts_drift_segment)
    escalation_segment = build_escalation_segment(escalation_events or [], ascii_only)
    if escalation_segment:
        parts.append(escalation_segment)
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


def _load_witness_edits(cwd, session_id):
    """Reads the current session's track "edits" list (WITNESS ECHO
    STALENESS, ported from HQ) -- structurally mirrors
    _load_witness_runs above (its OWN independent disk read, not a
    shared internal helper with it -- the same hook self-containment
    preference the module docstring already explains for the local
    _raw_sanitize/_ascii_sanitize copies: every track reader in this
    file is self-sufficient about reading, the only thing they share is
    the path formula, _witness_track_path). Returns a list (possibly
    empty) on a successful read; None on ANY failure (the same full set
    of failure modes as _load_witness_runs). The caller (_detect_staleness)
    treats both None and [] the same way: "no edits in the track to
    compare against"."""
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
        edits = data.get("edits")
        if not isinstance(edits, list):
            return None
        return edits
    except Exception:
        return None


def _last_edit_ts(edits: list):
    """The max ts among edits-records counted as a "code edit" (WITNESS
    ECHO STALENESS) -- the same lexicographic-==-chronological
    convention _last_by_ts already applies to runs (dod_track's ts
    values are fixed-width ISO with microseconds).

    Records whose file_path is doc-only (_is_doc_only_edit_path -- the
    mirror of tools/dod_gate.py._is_doc_only_file, the same extension
    list) are EXCLUDED from the max -- without this filter, the very
    Edit/Write call writing THIS accepted line into routing-log.jsonl
    (itself a .jsonl file, hence doc-only) would make itself the
    "latest edit", falsely staling every batched accepted line. A
    record with no file_path (None/non-string -- an old track, or a
    payload missing the field) is CONSERVATIVELY treated as NOT
    doc-only (_is_doc_only_edit_path(None) == False) -- counted as a
    code edit; missing information does not earn an exemption, the same
    fail-safe principle as the rest of this file.

    Records with a non-string "ts" (a corrupted third-party track entry)
    are also skipped -- a defensive default, does not break the max
    computation over the rest. Non-dict elements are skipped too. An
    empty/all-doc-only/all-broken edits list -> None (nothing to
    compare, see _detect_staleness)."""
    values = [e.get("ts") for e in edits
              if isinstance(e, dict) and isinstance(e.get("ts"), str)
              and not _is_doc_only_edit_path(e.get("file_path"))]
    return max(values) if values else None


def _last_green_ts(runs: list):
    """The max ts among runs-records with outcome=="green" (WITNESS
    ECHO STALENESS) -- the same defense against corrupted records as
    _last_edit_ts above. No green run at all (only red, or an entirely
    empty runs list) -> None."""
    values = [r.get("ts") for r in runs
              if isinstance(r, dict) and r.get("outcome") == "green"
              and isinstance(r.get("ts"), str)]
    return max(values) if values else None


def _detect_staleness(runs: list, edits: list):
    """WITNESS ECHO STALENESS (ported from HQ, this batch): "the track's
    latest green run is dated AFTER the track's latest code edit" -- the
    SAME invariant tools/dod_gate.py.evaluate() already enforces at
    SubagentStop, checked again here at write time, over the whole
    session track (any agent_id -- not just the one filed on this
    journal line). See the module docstring, "WITNESS ECHO STALENESS",
    for the full comparison against dod_gate.py and what is deliberately
    NOT ported from it (per-agent filtering, the consecutive_blocks
    safeguard -- both are dod_gate.py's own acceptance-blocking POLICY,
    out of scope here).

    Returns None (silent -- the invariant holds, OR there is nothing to
    compare) | (last_edit_ts, last_green_ts_or_None) (violated --
    warn_stale, see _collect_witness_events).

    No edit at all in the track (last_edit_ts is None -- edits is
    empty/all-doc-only/all-broken/None) -> None with no further check --
    literally nothing to compare (the same "no data, no verdict"
    principle as the rest of this file).

    At least one edit: a violation is EITHER no green run at all in the
    track (last_green_ts is None) OR the latest edit strictly later than
    the latest green run (last_edit_ts > last_green_ts, a plain string
    comparison -- lexicographic ISO-with-microseconds, the same trick
    _last_by_ts already uses). An edit at EXACTLY the same ts as a green
    run (the boundary, in practice unreachable -- microsecond
    resolution makes a real collision vanishingly unlikely, but the
    strict `>` stays silent on equality anyway, symmetric with
    _detect_ts_drift elsewhere in this file) is NOT a violation -- a
    green run is not considered stale relative to an edit that happened
    no later than it."""
    last_edit_ts = _last_edit_ts(edits)
    if last_edit_ts is None:
        return None
    last_green_ts = _last_green_ts(runs)
    if last_green_ts is None or last_edit_ts > last_green_ts:
        return (last_edit_ts, last_green_ts)
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
      6. (WITNESS ECHO STALENESS, ported from HQ, INDEPENDENT of 1-5,
         see _detect_staleness): the track is non-empty (outcome 2 did
         not fire) AND carries at least one edit AND (no green run at
         all, OR the latest edit is LATER than the latest green run) ->
         ADDITIONALLY ("warn_stale", line_no, last_edit_ts,
         last_green_ts_or_None) -- orthogonal to outcomes 3/4: the
         SPECIFIC command cited in the witness can honestly match its
         own latest green run (outcome 5, silent on THAT axis) while
         the track as a whole still carries a LATER edit with no
         re-run since -- both axes print INDEPENDENTLY for one line
         when both fire.

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
    for disk_text/git in main(). WITNESS ECHO STALENESS adds a SECOND,
    independent lazy-once cache for edits (_load_witness_edits) -- its
    own cache, not shared internal state with the runs cache (mirrors
    _load_witness_edits not being a shared helper with _load_witness_runs,
    see that function's own docstring)."""
    events = []
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    runs_loaded = False
    runs_cache = None
    edits_loaded = False
    edits_cache = None
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

            # WITNESS ECHO STALENESS (outcome 6 above): computed IN
            # PARALLEL with the command matching below, not instead of
            # it.
            if not edits_loaded:
                edits_cache = _load_witness_edits(cwd, session_id)
                edits_loaded = True
            staleness = _detect_staleness(runs_cache, edits_cache or [])
            if staleness is not None:
                last_edit_ts, last_green_ts = staleness
                events.append(("warn_stale", line_no, last_edit_ts, last_green_ts))

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
    chars or a giant ts value).

    "warn_stale" (WITNESS ECHO STALENESS, ported from HQ): the track's
    ts values (last_edit_ts, and, if present, last_green_ts) are the
    SAME kind of third-party dynamic content as cmd/ts on warn_loud
    above, sanitized the same way. last_green_ts may be None (no green
    run at all in the track -- see _detect_staleness) -- rendered as
    the literal "none" (NOT sanitized -- a static ASCII literal of this
    module, not a value out of the track)."""
    sanitize = _ascii_sanitize if ascii_only else _raw_sanitize
    kind = event[0]
    line_no = event[1]
    if kind == "warn_loud":
        _, _, cmd, ts = event
        return (f"WITNESS ECHO: line {line_no} contradiction - command "
                f"'{sanitize(cmd)}' recorded RED in session track (last red at {sanitize(str(ts))})")
    if kind == "warn_stale":
        _, _, last_edit_ts, last_green_ts = event
        green_part = sanitize(str(last_green_ts)) if last_green_ts is not None else "none"
        return (f"WITNESS ECHO: line {line_no} track staleness - last code edit at "
                f"{sanitize(str(last_edit_ts))} is after the last green run (last green: "
                f"{green_part}) - witness not confirmed by a green run after the last edit")
    # warn_soft
    return (f"WITNESS ECHO: line {line_no} witness command(s) not observed in "
            "session track (batch/cross-session/retro acceptance legitimate - verify manually)")


def build_witness_segment(witness_events: list, ascii_only: bool = False) -> str:
    """Assembles the WITNESS ECHO part of additionalContext -- ONLY
    from "warn_loud"/"warn_soft"/"warn_stale" events ("warn_stale"
    ported from HQ alongside the other two -- "note" events are silent
    by definition, see _collect_witness_events); ceiling
    MAX_WITNESS_LINES (=5, boundary-tested at 5/6), same "+K more"
    pattern as build_tier_segment -- ONE shared ceiling across all
    visible kinds together (not a separate per-kind limit: one journal
    line can already produce several events of different kinds, see
    _collect_witness_events outcome 6, and this is not a NEW limit --
    MAX_WITNESS_LINES predates the staleness axis, only the list of
    kinds it counts is extended here). An empty visible-events list ->
    "" (the caller treats an empty string as "no segment", same
    principle as build_tier_segment)."""
    warn_events = [e for e in witness_events if e[0] in ("warn_loud", "warn_soft", "warn_stale")]
    if not warn_events:
        return ""
    head = warn_events[:MAX_WITNESS_LINES]
    rest = len(warn_events) - len(head)
    body = "; ".join(_format_witness_line(e, ascii_only) for e in head)
    if rest > 0:
        body += f"; +{rest} more"
    return body


# --- ESCALATION ECHO at write time (ported from HQ, batch B6, task 2:
# R6-escalation machine guard on the write path, workstream 3 / Phase 4
# D-0098) --------------------------------------------------------------
# GAP (R6, CLAUDE.md "Routing rules", rule 6 here): "two `rejected`
# events with the same task_id on the same tier make escalation
# mandatory" was held ONLY by discipline on the write path -- the ONLY
# existing detector was the WEEKLY CALIBRATION at HQ (a journal-shaped
# check reading logs/routing-log.jsonl AFTER the fact, not at the
# moment the third same-tier retry actually gets written). This layer
# is a WARN, NOT a block (promotion to a hard block is a LATER step per
# the code-gates-execution clause, explicitly NOT this task -- NON-GOALS
# leave tools/journal_validator.py untouched): the same pattern TS
# DRIFT ECHO above already applies for the F-29-equivalent case (warn
# at write time; a hard gate is a separate, coarser instrument, not
# engaged here).
#
# DETECTOR REGISTRATION (four-questions-per-mechanism rule, clause c):
# this layer's OWN failure detector is the HOST's weekly R6-escalation
# calibration check (CLAUDE.md rule 6 / PROCESS/WEEKLY_CALIBRATION_PROTOCOL.md
# at HQ) -- a journal-shaped audit finding a same-tier third retry with
# no `escalated` event anywhere above it is exactly the case this WARN
# layer already flags at write time; a systematic miss here (a WARN
# that should have fired and didn't) would surface there as a case the
# calibration still had to catch post-hoc.
#
# TWO FORMS (identical logic to HQ's tools/journal_echo.py -- same
# section header there, ported verbatim):
#  1. a new `delegated` line with a numeric `attempt` >= 3: if there is
#     NO `escalated` event with the same task_id anywhere above (base
#     history + already-processed lines of THIS same batch) -> WARN.
#  2. a new `delegated` line with NO `attempt` field at all, but whose
#     task_id already has >=2 `rejected` events above sharing the SAME
#     model, with no `escalated` event AFTER the second such rejected
#     -> the same WARN ("a retry that forgot to carry `attempt`").
#
# ONE SHARED DETECTOR (_escalation_group_unsatisfied): both forms
# reduce to ONE check -- grouping a task_id's known `rejected` events
# by model, ANY group of size >=2 with no `escalated` event recorded
# AFTER the SECOND (by line position) entry of that group is a
# violation. This naturally implements both legal exceptions (boundary
# tests on both sides -- see tools/test_journal_echo_escalation.py):
#  - "attempt>=3 with an escalated event already above" -- an
#    escalated event AFTER the group's second rejected clears it;
#  - "attempt=2" -- below the >=3 threshold, form 1 never triggers
#    (and form 2 doesn't either -- `attempt` IS present, just <3);
#  - "attempt>=3 with rejected events on DIFFERENT tiers" -- if the
#    task_id's rejected models never repeat (each occurs <2 times), NO
#    group ever reaches size 2 -- vacuously "nothing to warn about"
#    (the same mechanism silences form 2 too: without a matching model
#    pair its own trigger condition never finds a size->=2 group).
#
# EXCLUDED TRIGGERS (not retries -- CLAUDE.md's Routing log section,
# THREE legitimate forms of a REPEAT `delegated` on an open task):
# agent=="critic" (a critic entry) AND notes carrying
# "replaces_worker:<handle>" (journal_validator.extract_replaces_worker
# -- REUSED, the same formula the validator already applies for its
# own no-silent-reuse check, not hand-duplicated) -- neither is a
# retry, both forms of this layer skip such lines outright (see
# _check_delegated_retry).
#
# SOURCE OF "ABOVE" (spec: "consume ONLY the payload-scoped new lines
# as the TRIGGER; reading the file's history for CONTEXT is fine"):
# base_lines (payload-scoped -- see _resolve_echo_base; the primary
# path yields the FULL disk content immediately BEFORE this specific
# tool call, not just committed HEAD) PLUS the lines of THIS SAME batch
# already processed (new_lines[:idx]) -- ONE linear pass
# (_collect_escalation_events), a per-task_id state accumulated as it
# goes; a `delegated` line is checked against state accumulated
# STRICTLY BEFORE it (a `delegated` line itself never writes into
# state -- the update-vs-check order is irrelevant for it, but LATER
# lines of the SAME batch can still reference it if it happens to be
# `rejected`/`escalated`). "pos" is a plain, monotonically increasing
# integer line index of the single pass (base_lines, then new_lines) --
# comparing with ">" for "escalated AFTER the second rejected" needs no
# date parsing.
#
# NEVER BLOCKS (spec, literally: "exit 0, no permissionDecision"): this
# layer never changes main()'s exit code -- the WARN goes out on the
# SAME additionalContext/stderr channels as TIER/WITNESS/TS DRIFT (this
# file never prints permissionDecision at all -- see the module
# docstring, "OUTPUT").
#
# Fails open per line (the same pattern as _collect_tier_events/
# _collect_ts_drift_events): a broken line's JSON, a non-dict line --
# try/except with `continue` per line, does not interrupt parsing the
# rest of the batch, does not crash the hook.
MAX_ESCALATION_LINES = 5  # the same class of ceiling as MAX_TIER_LINES/
# MAX_WITNESS_LINES/MAX_TS_DRIFT_LINES above -- own engineering
# decision, the same number 5, the same motive (a standalone/large
# batch with no ceiling -> unbounded additionalContext on one hook
# call). Boundary-tested at 5/6 -- see
# tools/test_journal_echo_escalation.py.


def _escalation_group_unsatisfied(rejected: list, escalated: list) -> bool:
    """True -- for this task_id there IS at least one model-group of
    `rejected` events of size >=2 with no `escalated` event recorded
    AFTER the second (by position) entry of that group (see the section
    above for the full rationale -- the ONE shared detector for both
    forms of this layer's spec, implementing both legal exceptions
    "escalated above"/"rejected on different tiers" for free).

    rejected -- [(pos, model), ...], escalated -- [pos, ...] (positions
    are the integer line index of the single pass in
    _collect_escalation_events, monotonically increasing). A model that
    isn't a string (a broken/missing rejected.model) groups under its
    actual value as a dict key (including None) -- a defensive default,
    two records sharing the same "broken" value still form a group (does
    not crash the check); in practice `model` is a REQUIRED field on
    `rejected` (journal_validator), this layer does not rely on the form
    of the lines above being valid."""
    by_model: dict = {}
    for pos, model in rejected:
        by_model.setdefault(model, []).append(pos)
    for positions in by_model.values():
        if len(positions) >= 2:
            second_pos = sorted(positions)[1]
            if not any(epos > second_pos for epos in escalated):
                return True
    return False


def _check_delegated_retry(obj: dict, state: dict):
    """For ONE `delegated` line (obj -- an already-parsed dict), decides
    whether it triggers either of the two forms of this layer's spec,
    and if so, whether the detector (_escalation_group_unsatisfied) is
    violated for its task_id against the accumulated state (see
    _collect_escalation_events). Returns
    (trigger, task_id, attempt_display) | None.

    Excluded triggers (see the section above): agent=="critic" -> None
    immediately; notes carrying "replaces_worker:<handle>"
    (journal_validator.extract_replaces_worker(...) is not None) ->
    None immediately -- neither is a retry, regardless of attempt/
    task_id.

    task_id missing/not a string/empty -> None (nothing to check, the
    same fail-open principle as the rest of this file).

    `attempt` -- a number (int/float, WITHOUT bool -- isinstance(x,
    bool) is True for the literals True/False in Python, a defensive
    guard: bool is NOT the same thing as a numeric `attempt`, even
    though it's technically an int subclass). Form 1 (attempt>=3):
    trigger="attempt". Form 2 (`attempt` is ABSENT --
    obj.get("attempt") is None -- AND the task_id already has >=2
    `rejected` events accumulated, at least potentially from one
    model-group -- the final filter is below): trigger="no_attempt".
    Neither -> None (including attempt=1, attempt=2, a non-numeric
    attempt value other than None -- the spec's explicit legal cases).

    Final filter: _escalation_group_unsatisfied(rejected, escalated)
    False -> None (legitimate, see the section above). True -> a WARN
    tuple; attempt_display is the declared `attempt` for form 1, OR
    len(rejected)+1 for form 2 (an estimate of "which attempt number
    this delegated line effectively IS, since the field itself was
    forgotten" -- own engineering decision, the spec gives a literal
    "attempt N" template only for form 1, without pinning a number for
    form 2; documented here, flagged for Lead review)."""
    if obj.get("agent") == "critic":
        return None
    if journal_validator.extract_replaces_worker(obj.get("notes")) is not None:
        return None
    task_id = obj.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return None
    attempt = obj.get("attempt")
    is_attempt_number = isinstance(attempt, (int, float)) and not isinstance(attempt, bool)
    task_state = state.get(task_id, {"rejected": [], "escalated": []})
    rejected = task_state["rejected"]
    escalated = task_state["escalated"]
    if is_attempt_number and attempt >= 3:
        trigger = "attempt"
    elif attempt is None and len(rejected) >= 2:
        trigger = "no_attempt"
    else:
        return None
    if not _escalation_group_unsatisfied(rejected, escalated):
        return None
    attempt_display = attempt if trigger == "attempt" else len(rejected) + 1
    return (trigger, task_id, attempt_display)


def _collect_escalation_events(new_lines: list, base_lines: list) -> list:
    """One linear pass over base_lines (history -- CONTEXT, the spec
    explicitly allows this) then new_lines (the payload-scoped TRIGGER
    -- the check only runs on lines from here, per the spec: "consume
    ONLY the payload-scoped new lines"). Builds per-task_id state
    {"rejected": [(pos, model)], "escalated": [pos]} as it goes
    (_absorb) and, on EVERY `delegated` line FROM new_lines, checks it
    against state accumulated STRICTLY BEFORE it
    (_check_delegated_retry) -- only THEN (not before) is that same
    line itself absorbed into state, in case it is itself
    rejected/escalated (a `delegated` line never is, but later lines of
    THIS SAME batch may reference it).

    line_no uses the SAME formula as TIER ECHO/WITNESS ECHO/TS DRIFT
    ECHO (len(base_lines)+idx+1) -- consistent line numbers across the
    whole file.

    Fails open per line (the same pattern as _collect_tier_events/
    _collect_ts_drift_events): a broken line's JSON -- try/except with
    `continue`, does not interrupt parsing the rest of the batch."""
    events = []
    state: dict = {}

    def _touch(task_id):
        return state.setdefault(task_id, {"rejected": [], "escalated": []})

    def _absorb(obj, pos):
        event = obj.get("event")
        task_id = obj.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return
        if event == "rejected":
            _touch(task_id)["rejected"].append((pos, obj.get("model")))
        elif event == "escalated":
            _touch(task_id)["escalated"].append(pos)

    pos = 0
    for line in base_lines:
        pos += 1
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                _absorb(obj, pos)
        except Exception:
            continue

    for idx, line in enumerate(new_lines):
        pos += 1
        line_no = len(base_lines) + idx + 1
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                continue
            if obj.get("event") == "delegated":
                warn = _check_delegated_retry(obj, state)
                if warn is not None:
                    trigger, task_id, attempt_display = warn
                    events.append((line_no, trigger, task_id, attempt_display))
            _absorb(obj, pos)
        except Exception:
            continue
    return events


def _format_escalation_line(event: tuple, ascii_only: bool) -> str:
    """"R6-ЗЕРКАЛО: line N attempt A без escalated по task_id T - после
    двух rejected одного яруса эскалация обязательна" -- the spec (B6,
    written at HQ) gives this text as a literal (Russian, matching the
    R6 rule's own wording in CLAUDE.md's Russian text) -- kept verbatim
    here rather than translated, the SAME choice HQ's tools/journal_echo.py
    made for the identical layer (a literal is a literal, not a
    paraphrase target); "line N" is added ON TOP of the literal quote,
    by analogy with every other formatter in this file (TIER ECHO/
    WITNESS ECHO/TS DRIFT ECHO all carry "line N" -- distinguishing
    batch lines when joined with "; "); the task_id VALUE is
    substituted after "по task_id" (the spec names task_id as part of
    the message without a separate placeholder for its value -- a
    warning with no concrete task_id would be practically useless to
    the coordinator, the same principle WITNESS ECHO already applies
    inserting cmd/ts, TIER ECHO inserting measured; own decision,
    documented, flagged for Lead review). The spec's em dash ("—")
    becomes a plain ASCII hyphen here (the same choice NOTE_RETRO/
    NOTE_TRACK_EMPTY already made for a different literal elsewhere in
    this file). task_id is dynamic third-party JSON content, sanitized
    PER CHANNEL (raw for stdout, ascii for stderr), the same principle
    _format_witness_line applies to cmd/ts."""
    sanitize = _ascii_sanitize if ascii_only else _raw_sanitize
    line_no, _trigger, task_id, attempt_display = event
    return (f"R6-ЗЕРКАЛО: line {line_no} attempt {attempt_display} без escalated "
            f"по task_id {sanitize(str(task_id))} - после двух rejected одного "
            "яруса эскалация обязательна")


def build_escalation_segment(escalation_events: list, ascii_only: bool = False) -> str:
    """Assembles the ESCALATION part of additionalContext -- the SAME
    pattern as build_tier_segment/build_ts_drift_segment (ceiling
    MAX_ESCALATION_LINES=5 lines per call, "+K more" on top). An empty
    escalation_events -> "" (the caller treats an empty string as "no
    segment", same principle as the other build_* functions)."""
    if not escalation_events:
        return ""
    head = escalation_events[:MAX_ESCALATION_LINES]
    rest = len(escalation_events) - len(head)
    body = "; ".join(_format_escalation_line(ev, ascii_only) for ev in head)
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
        # -- only warn_loud/warn_soft/warn_stale trigger printing.
        witness_visible = any(e[0] != "note" for e in witness_events)

        # TS DRIFT ECHO at write time (ported from HQ, this batch): the
        # SAME payload-scoped base as TIER ECHO/WITNESS ECHO above --
        # `now` is the SAME variable already computed above for
        # decide()/_get_head_text, not recomputed. Warn-only, always
        # visible (no "note" branch, unlike WITNESS ECHO).
        try:
            ts_drift_events = _collect_ts_drift_events(echo_new_lines, echo_base_lines, now)
        except Exception:
            ts_drift_events = []

        # ESCALATION ECHO (ported from HQ, batch B6, task 2 -- R6-
        # escalation machine guard): the SAME payload-scoped base as
        # TIER/WITNESS/TS DRIFT above (see the "ESCALATION ECHO at write
        # time" section for how base_lines is used as history for
        # CONTEXT while the trigger stays on echo_new_lines). Fails open
        # as a second layer on top of the per-line try/except already
        # inside _collect_escalation_events itself -- the same pattern
        # as WITNESS ECHO/TS DRIFT ECHO above.
        try:
            escalation_events = _collect_escalation_events(echo_new_lines, echo_base_lines)
        except Exception:
            escalation_events = []

        if (not violations and not tier_events and not witness_visible
                and not ts_drift_events and not escalation_events):
            return 0

        # Fallback marker (t-277/t-279): visible ONLY when we're already
        # printing something else -- an otherwise fully clean call stays
        # silent even in fallback (see the section docstring above).
        fallback_marker = FALLBACK_MARKER_TEXT if used_fallback else ""

        context_for_stdout = combine_context(violations, tier_events, witness_events, ts_drift_events,
                                              escalation_events, fallback_marker, ascii_only=False)
        context_for_stderr = combine_context(violations, tier_events, witness_events, ts_drift_events,
                                              escalation_events, fallback_marker, ascii_only=True)

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
