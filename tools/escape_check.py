"""Fail-closed checker for the escape-allowlist -- hash-pins CLAUDE.md's
permanent escape/concession clauses to the decision-log section that
authorizes them, so a silent edit of the underlying decision (the
carrier text drifting away from its justification) is caught
mechanically instead of relying on someone noticing.

Style/contract mirrored from tools/critic_verdict_check.py: stdlib-only,
ASCII-only diagnostics (raw non-ASCII field VALUES are never interpolated
into output -- only field names, indices, ids and paths, and even those are
passed through _ascii_safe() before printing so a future non-ASCII id/path
still cannot break the ASCII guarantee), fail-closed on every path including
broken file encodings (no bare traceback).

Usage:
    python tools/escape_check.py
        Validate tools/escape_allowlist.json against the live working tree
        (a deployment's OWN copy of tools/escape_allowlist.template.json,
        edited with its own entries -- the template itself is never read
        by this script; see the template file's own instructions).
        Exit 0, stdout "ESCAPE ALLOWLIST OK: N entries" if every entry's
        three legs hold; exit 1 with one ASCII diagnostic line per violation
        (each line names the entry id and the broken leg) otherwise. No
        tools/escape_allowlist.json at all (a fresh checkout that has not
        set one up yet) is also a failure -- see the module's own
        fail-closed design: an unconfigured allowlist is not silently
        skipped.

    python tools/escape_check.py --hash D-XXXX
        Print the sha256 hex digest of decision section D-XXXX as found in
        DEFAULT_DECISION_FILE_REL below (a documented placeholder path --
        see that constant's own comment for the format mismatch with this
        toolkit's actual DECISIONS.md) and exit 0. Exit 1 if the section
        does not exist or is duplicated, or if the decision file cannot be
        read/decoded.

    Any other invocation (unknown flag, wrong argument count) is a usage
    error: exit 2, usage line on stderr, nothing is validated (fail-closed).

Section extraction algorithm (decision-log section format:
"## D-00NN" or "## D-00NN -- title"):
    1. The full decision file is read as bytes and utf-8-decoded; decoding
       failure is a fail-closed error (never surfaced as a raw traceback).
    2. CRLF and bare CR line endings are normalized to LF *before* any line
       scanning, so a CRLF checkout of the same file hashes identically to
       an LF one.
    3. A line "opens" section <decision_id> when it matches
       ``^## <decision_id>`` followed by end-of-line or a non-alphanumeric
       character (a word-boundary-style exact-id match): "## D-0056" and
       "## D-0056 -- title" both match; "## D-00561" does not (extra digit);
       "## D-0056b" does not either (extra letter) -- applied symmetrically
       to any alphanumeric continuation, so an accidental near-miss id
       never silently matches.
    4. Exactly one such line must exist in the file; zero is "not found",
       more than one is "duplicate" -- both fail-closed (no
       first-one-wins). This is checked by scanning the WHOLE file for the
       specific decision_id's opening pattern, not by matching against a
       generic "## " sweep.
    5. The section runs from that opening line (inclusive) up to but not
       including the next line matching ``^## `` (a generic ATX H2), or to
       end of file. ASSUMPTION: a D-section's BODY never contains a line
       starting with "## " itself (e.g. a quoted code block reproducing
       another "## " heading verbatim); this generic boundary would
       truncate the section early if it did.
    6. Trailing wholly-empty lines (line == "" after the CRLF/CR -> LF
       normalization and the line-array split) are trimmed off the END of
       the extracted section only -- a section with no trailing blank line
       (e.g. one that ends the file with no final newline) is left as-is.
    7. The sha256 digest is computed over the UTF-8 encoding of the
       remaining lines re-joined with "\n", INCLUDING the opening header
       line.

Two allowlist validation modes share this same extraction+hash routine (the
--hash CLI mode and leg (c) of the no-args validation mode), by design, so
the value a human pastes into escape_allowlist.json via --hash is guaranteed
to be exactly what the validator will later recompute and compare.

Leg (a) contract -- whitespace-folded substring match: leg (a) is a
LIVENESS detector for the escape clause in its carrier (has the clause
been deleted, or rewritten in substance?) -- it is NOT a text-integrity
check; integrity of the cited DECISION text is leg (c)'s job (the section
hash). Carriers are reflowable markdown; a byte-exact anchor spanning a
line-wrap point would break on every reflow with no substantive rule
change -- a false alarm by construction. Therefore, before the substring
containment check, BOTH the carrier's full decoded text and the entry's
carrier_anchor have every run of whitespace drawn from the set
{space, tab, CR, LF} collapsed to a single space (see _fold_whitespace());
containment is then checked on the folded strings. This folding is scoped
STRICTLY to leg (a) -- it is never applied to decision-section extraction
or hashing (legs (b)/(c) keep the original CRLF/CR->LF-only normalization
documented above): a decision section's exact wording, not just its
liveness, is exactly what the pinned hash exists to protect, so leg (c)
must stay sensitive to a whitespace-only reflow of the decision text even
though leg (a) is deliberately blind to the same class of change in the
carrier.
"""

import hashlib
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
ALLOWLIST_PATH = os.path.join(SCRIPT_DIR, "escape_allowlist.json")
# FORMAT MISMATCH FLAGGED, NOT SILENTLY PAPERED OVER: this default
# assumes a VERBOSE decision log with "## D-NNNN[ -- title]" section
# headers (the reference implementation's docs/DECISIONS_FULL.md
# convention) -- this toolkit's OWN decision log (DECISIONS.md, see
# that file) is intentionally a terse ONE-LINE-PER-DECISION index
# ("- D-NNNN -- <operative statement>."), which extract_decision_section()
# below cannot section-extract from at all (no "## " headers exist
# there). Every allowlist entry names its OWN decision_file, so this
# constant only matters for the --hash CLI convenience shortcut's
# default target -- it is left pointing at the reference convention's
# path as a documented placeholder, not asserted to work against this
# toolkit's actual DECISIONS.md out of the box. A deployment adopting
# this mechanism for real needs a verbose, section-headed decision
# document of its own (either growing one alongside DECISIONS.md, or
# repurposing DECISIONS.md's format) -- an architectural choice this
# port does not make on the deployment's behalf.
DEFAULT_DECISION_FILE_REL = os.path.join("docs", "DECISIONS_FULL.md")
DEFAULT_DECISION_FILE_ABS = os.path.join(REPO_ROOT, DEFAULT_DECISION_FILE_REL)

REQUIRED_FIELDS = (
    "id",
    "carrier_file",
    "carrier_anchor",
    "decision_id",
    "decision_file",
    "section_sha256",
    "affirmed",
)
OPTIONAL_FIELDS = ("note",)
ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

_DECISION_ID_RE = re.compile(r"^D-\d{4}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _ascii_safe(value):
    """Return an ASCII-only representation of value for use in diagnostics.

    Plain ASCII strings pass through unchanged; anything else (or a
    non-string) is rendered via the ascii() builtin, which backslash-escapes
    every non-ASCII codepoint (unlike repr(), which in Python 3 leaves
    printable non-ASCII characters untouched -- repr() alone is NOT
    ASCII-safe here) -- guaranteeing the caller's output stays ASCII
    regardless of what a malformed/adversarial allowlist entry contains.
    """
    if isinstance(value, str) and value.isascii():
        return value
    return ascii(value)


def _normalize_newlines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


_WHITESPACE_RUN_RE = re.compile(r"[ \t\r\n]+")


def _fold_whitespace(text):
    """Collapse every run of space/tab/CR/LF into a single space.

    Leg (a) ONLY (see module docstring "Leg (a) contract"). Never used for
    decision-section extraction or hashing (legs (b)/(c)), which keep the
    original CRLF/CR->LF-only _normalize_newlines().
    """
    return _WHITESPACE_RUN_RE.sub(" ", text)


def read_text_file(path):
    """Read path as UTF-8 text. Returns (text, None) or (None, error_str).

    Never raises: OSError and UnicodeDecodeError are both converted into an
    ASCII error string (fail-closed, no traceback leak).
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return None, "cannot read file %s: %s" % (_ascii_safe(path), _ascii_safe(str(exc)))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "file %s is not valid UTF-8" % _ascii_safe(path)
    return text, None


def extract_decision_section(text, decision_id):
    """Return (section_text, status) where status is one of:
    "ok", "not_found", "duplicate". section_text is None unless status=="ok".
    """
    normalized = _normalize_newlines(text)
    pattern = re.compile(r"^## " + re.escape(decision_id) + r"(?![A-Za-z0-9])")
    lines = normalized.split("\n")
    matches = [i for i, line in enumerate(lines) if pattern.match(line)]

    if not matches:
        return None, "not_found"
    if len(matches) > 1:
        return None, "duplicate"

    start = matches[0]
    end = start + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1

    section_lines = lines[start:end]
    while section_lines and section_lines[-1] == "":
        section_lines.pop()

    return "\n".join(section_lines), "ok"


def section_sha256(text, decision_id):
    """Return (digest_hex, status); digest_hex is None unless status=="ok"."""
    section_text, status = extract_decision_section(text, decision_id)
    if status != "ok":
        return None, status
    digest = hashlib.sha256(section_text.encode("utf-8")).hexdigest()
    return digest, "ok"


# ---------------------------------------------------------------------------
# allowlist schema validation
# ---------------------------------------------------------------------------


def validate_root(root):
    """Return (errors, entries_or_None). entries is None iff a root-level
    error makes further per-entry validation meaningless."""
    errors = []
    if not isinstance(root, dict):
        errors.append(
            "allowlist root is not an object (type: %s)" % type(root).__name__
        )
        return errors, None
    if "entries" not in root:
        errors.append("missing required field: entries")
        return errors, None
    entries = root["entries"]
    if not isinstance(entries, list):
        errors.append("field 'entries' must be an array")
        return errors, None
    return errors, entries


def validate_entry_schema(entry, index):
    """Return a list of ASCII violation strings for one raw entry (index in
    the entries array). Empty list means the entry is schema-valid and safe
    to pass to check_entry_legs()."""
    errors = []
    if not isinstance(entry, dict):
        errors.append(
            "entries[%d] is not an object (type: %s)" % (index, type(entry).__name__)
        )
        return errors

    entry_ref = entry.get("id")
    ref = _ascii_safe(entry_ref) if isinstance(entry_ref, str) else ("index %d" % index)

    for field in REQUIRED_FIELDS:
        if field not in entry:
            errors.append(
                "entry %s: missing required field: %s" % (ref, field)
            )

    def _is_nonempty_str(v):
        return isinstance(v, str) and len(v) > 0

    if "id" in entry and not _is_nonempty_str(entry.get("id")):
        errors.append("entry %s: field 'id' must be a non-empty string" % ref)
    if "carrier_file" in entry and not _is_nonempty_str(entry.get("carrier_file")):
        errors.append("entry %s: field 'carrier_file' must be a non-empty string" % ref)
    if "carrier_anchor" in entry:
        anchor = entry.get("carrier_anchor")
        if not _is_nonempty_str(anchor):
            errors.append("entry %s: field 'carrier_anchor' must be a non-empty string" % ref)
        elif _fold_whitespace(anchor).strip() == "":
            # A whitespace-only anchor still passes _is_nonempty_str()
            # (len > 0) but folds to "" / " ", which is a substring of
            # EVERY carrier text -- leg (a) would then be vacuously true
            # (a liveness check that can never fail). Rejected at schema
            # validation, before leg (a) ever runs.
            errors.append(
                "entry %s: field 'carrier_anchor' must contain non-whitespace" % ref
            )
    if "decision_file" in entry and not _is_nonempty_str(entry.get("decision_file")):
        errors.append("entry %s: field 'decision_file' must be a non-empty string" % ref)

    if "decision_id" in entry:
        did = entry.get("decision_id")
        if not isinstance(did, str) or not _DECISION_ID_RE.match(did):
            errors.append(
                "entry %s: field 'decision_id' must match D-NNNN (4 digits)" % ref
            )

    if "section_sha256" in entry:
        sh = entry.get("section_sha256")
        if not isinstance(sh, str) or not _SHA256_RE.match(sh):
            errors.append(
                "entry %s: field 'section_sha256' must be 64 lowercase hex characters" % ref
            )

    if "affirmed" in entry:
        af = entry.get("affirmed")
        valid_date = False
        if isinstance(af, str) and _DATE_RE.match(af):
            import datetime

            try:
                datetime.date(int(af[0:4]), int(af[5:7]), int(af[8:10]))
                valid_date = True
            except ValueError:
                valid_date = False
        if not valid_date:
            errors.append(
                "entry %s: field 'affirmed' must be a YYYY-MM-DD calendar date" % ref
            )

    if "note" in entry and entry.get("note") is not None:
        if not isinstance(entry.get("note"), str):
            errors.append("entry %s: field 'note' must be a string" % ref)

    return errors


def check_entry_legs(entry, repo_root):
    """Run the three validation legs for one schema-valid entry. Returns a
    list of ASCII violation strings (empty means all three legs hold)."""
    errors = []
    entry_id = _ascii_safe(entry["id"])
    decision_id = _ascii_safe(entry["decision_id"])

    # leg (a): carrier alive
    carrier_path = os.path.join(repo_root, entry["carrier_file"])
    carrier_text, err = read_text_file(carrier_path)
    if carrier_text is None:
        errors.append(
            "entry %s: carrier leg failed: %s" % (entry_id, err)
        )
    elif _fold_whitespace(entry["carrier_anchor"]) not in _fold_whitespace(carrier_text):
        errors.append(
            "entry %s: carrier leg failed: anchor not found in %s"
            % (entry_id, _ascii_safe(entry["carrier_file"]))
        )

    # legs (b)+(c): decision section exists and hash matches
    decision_path = os.path.join(repo_root, entry["decision_file"])
    decision_text, err = read_text_file(decision_path)
    if decision_text is None:
        errors.append(
            "entry %s: decision leg failed: %s" % (entry_id, err)
        )
        return errors

    digest, status = section_sha256(decision_text, entry["decision_id"])
    if status == "not_found":
        errors.append(
            "entry %s: decision leg failed: section %s not found in %s"
            % (entry_id, decision_id, _ascii_safe(entry["decision_file"]))
        )
    elif status == "duplicate":
        errors.append(
            "entry %s: decision leg failed: section %s duplicated in %s"
            % (entry_id, decision_id, _ascii_safe(entry["decision_file"]))
        )
    elif digest != entry["section_sha256"]:
        errors.append(
            "entry %s: hash leg failed: section %s in %s has drifted "
            "from the pinned sha256 (recompute with --hash %s and "
            "re-affirm if the drift is intentional)"
            % (entry_id, decision_id, _ascii_safe(entry["decision_file"]), decision_id)
        )

    return errors


def run_validate(allowlist_path, repo_root):
    """Return (ok, errors, entry_count)."""
    text, err = read_text_file(allowlist_path)
    if text is None:
        return False, ["allowlist: %s" % err], 0

    try:
        root = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, ["allowlist: invalid JSON: %s" % _ascii_safe(str(exc))], 0

    root_errors, entries = validate_root(root)
    if entries is None:
        return False, ["allowlist: %s" % e for e in root_errors], 0

    all_errors = ["allowlist: %s" % e for e in root_errors]

    valid_entries = []
    seen_ids = []
    for idx, entry in enumerate(entries):
        entry_errors = validate_entry_schema(entry, idx)
        if entry_errors:
            all_errors.extend(entry_errors)
            continue
        valid_entries.append(entry)
        seen_ids.append(entry["id"])

    dup_ids = sorted({i for i in seen_ids if seen_ids.count(i) > 1})
    for dup in dup_ids:
        all_errors.append("duplicate entry id in allowlist: %s" % _ascii_safe(dup))

    for entry in valid_entries:
        all_errors.extend(check_entry_legs(entry, repo_root))

    if all_errors:
        return False, all_errors, len(entries)
    return True, [], len(entries)


def main(argv):
    args = argv[1:]

    if len(args) == 0:
        ok, errors, count = run_validate(ALLOWLIST_PATH, REPO_ROOT)
        if not ok:
            sys.stderr.write("ESCAPE ALLOWLIST INVALID:\n")
            for e in errors:
                sys.stderr.write("  - %s\n" % e)
            return 1
        sys.stdout.write("ESCAPE ALLOWLIST OK: %d entries\n" % count)
        return 0

    if len(args) == 2 and args[0] == "--hash":
        decision_id = args[1]
        text, err = read_text_file(DEFAULT_DECISION_FILE_ABS)
        if text is None:
            sys.stderr.write("ESCAPE HASH FAILED: %s\n" % err)
            return 1
        digest, status = section_sha256(text, decision_id)
        if status == "not_found":
            sys.stderr.write(
                "ESCAPE HASH FAILED: section %s not found in %s\n"
                % (_ascii_safe(decision_id), DEFAULT_DECISION_FILE_REL)
            )
            return 1
        if status == "duplicate":
            sys.stderr.write(
                "ESCAPE HASH FAILED: section %s duplicated in %s\n"
                % (_ascii_safe(decision_id), DEFAULT_DECISION_FILE_REL)
            )
            return 1
        sys.stdout.write("%s\n" % digest)
        return 0

    sys.stderr.write(
        "usage: escape_check.py [--hash D-XXXX]\n"
        "  (no args)   validate tools/escape_allowlist.json against the live tree\n"
        "  --hash ID   print sha256 of decision section ID in this repo's decision log\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
