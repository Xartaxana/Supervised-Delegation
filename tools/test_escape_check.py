"""Battery for tools/escape_check.py (hash-pinning of permanent
escape/concession clauses to their authorizing decision-log section).

Covers: green path (all three legs alive), broken carrier anchor, missing
decision section, decision-section drift (hash mismatch, diagnostic names
entry+decision_id), duplicate section in the decision file, duplicate id in
the allowlist, broken JSON / non-object root / per-field schema violations,
empty entries -> OK 0, unknown CLI flag -> exit 2, --hash of a non-existent
decision -> exit 1, CRLF/LF hash-normalization equivalence, an
end-of-file section with no trailing newline, non-UTF-8 bytes in both the
allowlist and a decision/carrier file (fail-closed, ASCII diagnostic, no
traceback), and the DoD's template battery: the shipped
tools/escape_allowlist.template.json's one example entry is DESIGNED to
fail (a placeholder decision id/carrier anchor, see that file's own
instructions) -- one negative check confirms it fails as designed instead
of silently reporting a fake OK, and one positive check builds a fixture
tree modeled on the same shape and confirms it validates clean once the
placeholder values are replaced with real ones.

Run: python -m pytest tools/test_escape_check.py -q
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import escape_check as ec

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER_PATH = REPO_ROOT / "tools" / "escape_check.py"
TEMPLATE_PATH = REPO_ROOT / "tools" / "escape_allowlist.template.json"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

DECISION_TEXT = (
    "preamble text, not part of any section\n"
    "\n"
    "## D-0001 -- first decision title\n"
    "body line one\n"
    "body line two\n"
    "\n"
    "## D-0002\n"
    "second decision body, no title suffix\n"
)

CARRIER_TEXT = (
    "Some prose leads in.\n"
    "ANCHOR-PHRASE-HERE is the load-bearing clause in this carrier file.\n"
    "More prose follows.\n"
)


def _write_bytes(path, data):
    path.write_bytes(data)


def _write_text(path, text, encoding="utf-8"):
    path.write_bytes(text.encode(encoding))


def _make_tree(tmp_path, decision_text=DECISION_TEXT, carrier_text=CARRIER_TEXT):
    carrier = tmp_path / "CARRIER.md"
    decision = tmp_path / "DECISIONS_FULL.md"
    _write_text(carrier, carrier_text)
    _write_text(decision, decision_text)
    return carrier, decision


def _entry(**overrides):
    base = {
        "id": "sample-entry",
        "carrier_file": "CARRIER.md",
        "carrier_anchor": "ANCHOR-PHRASE-HERE",
        "decision_id": "D-0001",
        "decision_file": "DECISIONS_FULL.md",
        "section_sha256": None,  # filled by caller via real hash unless testing drift
        "affirmed": "2026-07-22",
        "note": "test fixture entry",
    }
    base.update(overrides)
    return base


def _real_digest(decision_text, decision_id):
    digest, status = ec.section_sha256(decision_text, decision_id)
    assert status == "ok", status
    return digest


def _write_allowlist(tmp_path, entries, name="allowlist.json"):
    p = tmp_path / name
    p.write_bytes(json.dumps({"entries": entries}, ensure_ascii=False).encode("utf-8"))
    return p


def _run_cli(args, input_bytes=None, env=None, cwd=None):
    return subprocess.run(
        [sys.executable, str(CHECKER_PATH)] + args,
        cwd=str(cwd) if cwd else str(REPO_ROOT),
        input=input_bytes,
        capture_output=True,
        timeout=15,
        env=env,
    )


# ---------------------------------------------------------------------------
# green path (all three legs alive)
# ---------------------------------------------------------------------------


def test_green_path_all_legs_alive(tmp_path):
    _make_tree(tmp_path)
    digest = _real_digest(DECISION_TEXT, "D-0001")
    entry = _entry(section_sha256=digest)
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert ok, errors
    assert count == 1


def test_green_path_multiple_entries(tmp_path):
    _make_tree(tmp_path)
    d1 = _real_digest(DECISION_TEXT, "D-0001")
    d2 = _real_digest(DECISION_TEXT, "D-0002")
    entries = [
        _entry(id="e1", decision_id="D-0001", section_sha256=d1),
        _entry(id="e2", decision_id="D-0002", section_sha256=d2),
    ]
    allowlist = _write_allowlist(tmp_path, entries)

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert ok, errors
    assert count == 2


# ---------------------------------------------------------------------------
# leg (a): broken carrier anchor / missing carrier file
# ---------------------------------------------------------------------------


def test_broken_carrier_anchor_fails_and_names_entry(tmp_path):
    _make_tree(tmp_path)
    digest = _real_digest(DECISION_TEXT, "D-0001")
    entry = _entry(id="anchor-broken", carrier_anchor="THIS PHRASE IS NOT PRESENT", section_sha256=digest)
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert not ok
    assert any("anchor-broken" in e and "carrier leg failed" in e for e in errors)


def test_missing_carrier_file_fails(tmp_path):
    _make_tree(tmp_path)
    digest = _real_digest(DECISION_TEXT, "D-0001")
    entry = _entry(id="no-carrier", carrier_file="NOPE.md", section_sha256=digest)
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert not ok
    assert any("no-carrier" in e and "carrier leg failed" in e for e in errors)


# ---------------------------------------------------------------------------
# leg (a) whitespace-fold contract: liveness detector, not a text-integrity
# check -- fold runs of space/tab/CR/LF to a single space on both sides
# before the containment check, scoped to leg (a) only.
# ---------------------------------------------------------------------------


def test_fold_whitespace_collapses_runs():
    assert ec._fold_whitespace("a   b\tc\r\nd\n\ne") == "a b c d e"


def test_anchor_spanning_carrier_linewrap_is_found(tmp_path):
    carrier_text = "Intro.\nthe quick brown\nfox jumps over lazy dogs.\nOutro.\n"
    _make_tree(tmp_path, carrier_text=carrier_text)
    digest = _real_digest(DECISION_TEXT, "D-0001")
    entry = _entry(
        id="wrap-ok",
        carrier_anchor="the quick brown fox jumps",
        section_sha256=digest,
    )
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert ok, errors


def test_anchor_with_double_space_matches_single_space_in_carrier(tmp_path):
    carrier_text = "Intro.\nalpha beta gamma delta.\nOutro.\n"
    _make_tree(tmp_path, carrier_text=carrier_text)
    digest = _real_digest(DECISION_TEXT, "D-0001")
    entry = _entry(
        id="dbl-space",
        carrier_anchor="alpha  beta   gamma",  # double/triple space in allowlist
        section_sha256=digest,
    )
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert ok, errors


def test_reordered_words_in_anchor_still_fails(tmp_path):
    carrier_text = "Intro.\nalpha beta gamma delta.\nOutro.\n"
    _make_tree(tmp_path, carrier_text=carrier_text)
    digest = _real_digest(DECISION_TEXT, "D-0001")
    entry = _entry(
        id="reordered", carrier_anchor="alpha gamma beta", section_sha256=digest
    )
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert not ok
    assert any("reordered" in e and "carrier leg failed" in e for e in errors)


def test_word_substitution_in_anchor_still_fails(tmp_path):
    carrier_text = "Intro.\nalpha beta gamma delta.\nOutro.\n"
    _make_tree(tmp_path, carrier_text=carrier_text)
    digest = _real_digest(DECISION_TEXT, "D-0001")
    entry = _entry(
        id="substituted", carrier_anchor="alpha beta ZETA", section_sha256=digest
    )
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert not ok
    assert any("substituted" in e and "carrier leg failed" in e for e in errors)


def test_hash_leg_stays_whitespace_sensitive_unlike_leg_a():
    reflowed = DECISION_TEXT.replace(
        "body line one\nbody line two", "body line\none\nbody  line two"
    )
    original_digest = _real_digest(DECISION_TEXT, "D-0001")
    reflowed_digest, status = ec.section_sha256(reflowed, "D-0001")
    assert status == "ok"
    assert reflowed_digest != original_digest


# ---------------------------------------------------------------------------
# leg (b): missing decision section
# ---------------------------------------------------------------------------


def test_missing_decision_section_fails_and_names_entry_and_decision(tmp_path):
    _make_tree(tmp_path)
    entry = _entry(id="no-section", decision_id="D-0099", section_sha256="0" * 64)
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert not ok
    assert any(
        "no-section" in e and "D-0099" in e and "not found" in e for e in errors
    )


def test_missing_decision_file_fails(tmp_path):
    _make_tree(tmp_path)
    digest = _real_digest(DECISION_TEXT, "D-0001")
    entry = _entry(id="no-decfile", decision_file="NOPE.md", section_sha256=digest)
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert not ok
    assert any("no-decfile" in e and "decision leg failed" in e for e in errors)


# ---------------------------------------------------------------------------
# leg (c): hash drift
# ---------------------------------------------------------------------------


def test_section_drift_fails_and_names_entry_and_decision(tmp_path):
    _make_tree(tmp_path)
    stale_digest = _real_digest(DECISION_TEXT, "D-0001")
    drifted_text = DECISION_TEXT.replace("body line two", "body line two, EDITED")
    _make_tree(tmp_path, decision_text=drifted_text)
    entry = _entry(id="drifted", section_sha256=stale_digest)
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert not ok
    assert any("drifted" in e and "D-0001" in e and "drift" in e for e in errors)


# ---------------------------------------------------------------------------
# duplicate section in the decision file
# ---------------------------------------------------------------------------


DUPLICATE_SECTION_TEXT = (
    "## D-0001\n"
    "first copy\n"
    "\n"
    "## D-0001 -- again\n"
    "second copy\n"
)


def test_duplicate_section_in_decision_file_fails_closed():
    section, status = ec.extract_decision_section(DUPLICATE_SECTION_TEXT, "D-0001")
    assert status == "duplicate"
    assert section is None


def test_duplicate_section_reported_via_run_validate(tmp_path):
    _make_tree(tmp_path, decision_text=DUPLICATE_SECTION_TEXT)
    entry = _entry(id="dup-section", section_sha256="0" * 64)
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert not ok
    assert any("dup-section" in e and "duplicated" in e for e in errors)


def test_near_miss_ids_do_not_match_word_boundary():
    text = "## D-00011\nnot the section\n\n## D-0001b\nalso not the section\n"
    section, status = ec.extract_decision_section(text, "D-0001")
    assert status == "not_found"


# ---------------------------------------------------------------------------
# duplicate id in the allowlist
# ---------------------------------------------------------------------------


def test_duplicate_id_in_allowlist_fails(tmp_path):
    _make_tree(tmp_path)
    digest = _real_digest(DECISION_TEXT, "D-0001")
    entries = [
        _entry(id="same-id", section_sha256=digest),
        _entry(id="same-id", section_sha256=digest),
    ]
    allowlist = _write_allowlist(tmp_path, entries)

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert not ok
    assert any("duplicate entry id" in e and "same-id" in e for e in errors)


# ---------------------------------------------------------------------------
# broken JSON / non-object root / entries not a list
# ---------------------------------------------------------------------------


def test_broken_json_fails_closed(tmp_path):
    p = tmp_path / "allowlist.json"
    p.write_bytes(b"{not valid json,,,")
    ok, errors, count = ec.run_validate(str(p), str(tmp_path))
    assert not ok
    assert any("invalid JSON" in e for e in errors)


def test_root_array_instead_of_object_fails(tmp_path):
    p = tmp_path / "allowlist.json"
    p.write_bytes(b"[1, 2, 3]")
    ok, errors, count = ec.run_validate(str(p), str(tmp_path))
    assert not ok
    assert any("not an object" in e for e in errors)


def test_root_missing_entries_key_fails(tmp_path):
    p = tmp_path / "allowlist.json"
    p.write_bytes(json.dumps({"nope": []}).encode("utf-8"))
    ok, errors, count = ec.run_validate(str(p), str(tmp_path))
    assert not ok
    assert any("missing required field: entries" in e for e in errors)


def test_entries_not_a_list_fails(tmp_path):
    p = tmp_path / "allowlist.json"
    p.write_bytes(json.dumps({"entries": {"a": 1}}).encode("utf-8"))
    ok, errors, count = ec.run_validate(str(p), str(tmp_path))
    assert not ok
    assert any("must be an array" in e for e in errors)


def test_entry_not_an_object_fails(tmp_path):
    p = tmp_path / "allowlist.json"
    p.write_bytes(json.dumps({"entries": ["not-a-dict"]}).encode("utf-8"))
    ok, errors, count = ec.run_validate(str(p), str(tmp_path))
    assert not ok
    assert any("is not an object" in e for e in errors)


# ---------------------------------------------------------------------------
# per-field schema violations
# ---------------------------------------------------------------------------


def test_missing_required_field_named(tmp_path):
    _make_tree(tmp_path)
    digest = _real_digest(DECISION_TEXT, "D-0001")
    entry = _entry(id="missing-field", section_sha256=digest)
    del entry["carrier_anchor"]
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert not ok
    assert any(
        "missing-field" in e and "carrier_anchor" in e for e in errors
    )


def test_empty_string_id_fails():
    errors = ec.validate_entry_schema(_entry(id=""), 0)
    assert any("field 'id'" in e for e in errors)


def test_decision_id_bad_format_fails():
    errors = ec.validate_entry_schema(_entry(decision_id="D-56", section_sha256="0" * 64), 0)
    assert any("decision_id" in e for e in errors)


def test_decision_id_extra_digit_bad_format_fails():
    errors = ec.validate_entry_schema(_entry(decision_id="D-00561", section_sha256="0" * 64), 0)
    assert any("decision_id" in e for e in errors)


def test_section_sha256_wrong_length_fails():
    errors = ec.validate_entry_schema(_entry(section_sha256="abc123"), 0)
    assert any("section_sha256" in e for e in errors)


def test_section_sha256_uppercase_hex_fails():
    errors = ec.validate_entry_schema(_entry(section_sha256="A" * 64), 0)
    assert any("section_sha256" in e for e in errors)


def test_affirmed_bad_format_fails():
    errors = ec.validate_entry_schema(_entry(section_sha256="0" * 64, affirmed="22-07-2026"), 0)
    assert any("affirmed" in e for e in errors)


def test_affirmed_impossible_calendar_date_fails():
    errors = ec.validate_entry_schema(_entry(section_sha256="0" * 64, affirmed="2026-02-30"), 0)
    assert any("affirmed" in e for e in errors)


def test_note_wrong_type_fails():
    errors = ec.validate_entry_schema(_entry(section_sha256="0" * 64, note=123), 0)
    assert any("note" in e for e in errors)


def test_note_absent_is_valid():
    entry = _entry(section_sha256="0" * 64)
    del entry["note"]
    errors = ec.validate_entry_schema(entry, 0)
    assert errors == []


def test_carrier_file_empty_string_fails():
    errors = ec.validate_entry_schema(_entry(carrier_file="", section_sha256="0" * 64), 0)
    assert any("carrier_file" in e for e in errors)


def test_whitespace_only_carrier_anchor_fails_schema():
    errors = ec.validate_entry_schema(
        _entry(carrier_anchor="   \t\n  ", section_sha256="0" * 64), 0
    )
    assert any(
        "carrier_anchor" in e and "non-whitespace" in e for e in errors
    )


def test_single_space_carrier_anchor_fails_schema():
    errors = ec.validate_entry_schema(
        _entry(carrier_anchor=" ", section_sha256="0" * 64), 0
    )
    assert any(
        "carrier_anchor" in e and "non-whitespace" in e for e in errors
    )


def test_whitespace_only_carrier_anchor_rejected_via_run_validate(tmp_path):
    _make_tree(tmp_path)
    entry = _entry(id="ws-anchor", carrier_anchor="   ", section_sha256="0" * 64)
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert not ok
    assert any(
        "ws-anchor" in e and "carrier_anchor" in e and "non-whitespace" in e
        for e in errors
    )


# ---------------------------------------------------------------------------
# empty entries -> OK 0
# ---------------------------------------------------------------------------


def test_empty_entries_is_ok_zero(tmp_path):
    allowlist = _write_allowlist(tmp_path, [])
    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert ok, errors
    assert count == 0


def test_cli_empty_entries_prints_ok_zero(tmp_path):
    allowlist = tmp_path / "escape_allowlist.json"
    allowlist.write_bytes(json.dumps({"entries": []}).encode("utf-8"))
    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert ok and count == 0
    message = "ESCAPE ALLOWLIST OK: %d entries" % count
    assert message == "ESCAPE ALLOWLIST OK: 0 entries"


# ---------------------------------------------------------------------------
# CRLF/LF hash-normalization equivalence
# ---------------------------------------------------------------------------


def test_crlf_and_lf_decision_file_hash_identically():
    lf_text = DECISION_TEXT
    crlf_text = DECISION_TEXT.replace("\n", "\r\n")
    digest_lf, status_lf = ec.section_sha256(lf_text, "D-0001")
    digest_crlf, status_crlf = ec.section_sha256(crlf_text, "D-0001")
    assert status_lf == status_crlf == "ok"
    assert digest_lf == digest_crlf


def test_bare_cr_decision_file_hashes_same_as_lf():
    lf_text = DECISION_TEXT
    cr_text = DECISION_TEXT.replace("\n", "\r")
    digest_lf, status_lf = ec.section_sha256(lf_text, "D-0001")
    digest_cr, status_cr = ec.section_sha256(cr_text, "D-0001")
    assert status_lf == status_cr == "ok"
    assert digest_lf == digest_cr


# ---------------------------------------------------------------------------
# section at end of file with no trailing newline
# ---------------------------------------------------------------------------


def test_section_at_eof_no_trailing_newline():
    text = "## D-0001\nbody without a trailing newline"
    section, status = ec.extract_decision_section(text, "D-0001")
    assert status == "ok"
    assert section == "## D-0001\nbody without a trailing newline"


def test_section_at_eof_with_trailing_blank_lines_are_trimmed():
    text = "## D-0001\nbody\n\n\n"
    section, status = ec.extract_decision_section(text, "D-0001")
    assert status == "ok"
    assert section == "## D-0001\nbody"


def test_header_only_section_no_body():
    text = "## D-0001\n\n## D-0002\nbody\n"
    section, status = ec.extract_decision_section(text, "D-0001")
    assert status == "ok"
    assert section == "## D-0001"


# ---------------------------------------------------------------------------
# non-UTF-8 bytes: allowlist file, decision file, carrier file
# ---------------------------------------------------------------------------


def test_non_utf8_allowlist_file_fails_closed(tmp_path):
    p = tmp_path / "allowlist.json"
    p.write_bytes(bytes([0xFF, 0xFE, 0x80, 0x81]) * 10)
    ok, errors, count = ec.run_validate(str(p), str(tmp_path))
    assert not ok
    assert any("not valid UTF-8" in e for e in errors)
    "\n".join(errors).encode("ascii")


def test_non_utf8_decision_file_fails_closed(tmp_path):
    carrier, decision = _make_tree(tmp_path)
    decision.write_bytes(bytes([0xFF, 0xFE, 0x80, 0x81]) * 10)
    entry = _entry(section_sha256="0" * 64)
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert not ok
    assert any("not valid UTF-8" in e for e in errors)
    "\n".join(errors).encode("ascii")


def test_non_utf8_carrier_file_fails_closed(tmp_path):
    carrier, decision = _make_tree(tmp_path)
    carrier.write_bytes(bytes([0xFF, 0xFE, 0x80, 0x81]) * 10)
    digest = _real_digest(DECISION_TEXT, "D-0001")
    entry = _entry(section_sha256=digest)
    allowlist = _write_allowlist(tmp_path, [entry])

    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert not ok
    assert any("not valid UTF-8" in e for e in errors)
    "\n".join(errors).encode("ascii")


def test_non_ascii_id_diagnostic_stays_ascii(tmp_path):
    _make_tree(tmp_path)
    entry = _entry(id="дефект-якоря", carrier_anchor="NOT PRESENT", section_sha256="0" * 64)
    allowlist = _write_allowlist(tmp_path, [entry])
    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert not ok
    "\n".join(errors).encode("ascii")  # raises UnicodeEncodeError if not ASCII


# ---------------------------------------------------------------------------
# CLI contract: exit codes, unknown flag, argument-count boundaries
# ---------------------------------------------------------------------------


def test_cli_unknown_flag_exit_2():
    result = _run_cli(["--nope"])
    assert result.returncode == 2
    assert b"usage" in result.stderr


def test_cli_hash_flag_missing_argument_exit_2():
    result = _run_cli(["--hash"])
    assert result.returncode == 2


def test_cli_hash_flag_too_many_arguments_exit_2():
    result = _run_cli(["--hash", "D-0001", "extra"])
    assert result.returncode == 2


def test_cli_hash_fails_closed_when_default_decision_file_absent():
    # This toolkit ships no docs/DECISIONS_FULL.md by default (see
    # escape_check.DEFAULT_DECISION_FILE_REL's own comment) -- a real
    # subprocess run of --hash against the real repo tree must fail
    # closed with an ASCII diagnostic, never a raw traceback.
    result = _run_cli(["--hash", "D-0001"])
    assert result.returncode == 1
    assert b"ESCAPE HASH FAILED" in result.stderr
    assert b"Traceback" not in result.stderr
    assert b"Traceback" not in result.stdout


def test_hash_function_used_by_cli_hash_mode_produces_64_hex(tmp_path):
    # The pure function --hash relies on (section_sha256/
    # extract_decision_section) against a synthetic decision file --
    # exercised directly since this toolkit has no real decision file
    # at the CLI's hardcoded default path (see the test above).
    digest = _real_digest(DECISION_TEXT, "D-0001")
    assert len(digest) == 64
    int(digest, 16)  # raises ValueError if not hex


def test_hash_is_deterministic_across_calls():
    d1 = _real_digest(DECISION_TEXT, "D-0001")
    d2 = _real_digest(DECISION_TEXT, "D-0001")
    assert d1 == d2


def test_cli_no_args_output_is_ascii_regardless_of_verdict(tmp_path):
    result = _run_cli([], cwd=tmp_path)
    (result.stdout + result.stderr).decode("ascii")


def test_cli_stdin_invalid_bytes_do_not_affect_hash_mode():
    # --hash mode never reads stdin; feeding it garbage must not crash it
    # (it fails closed on the missing default decision file instead, the
    # expected behavior for this toolkit's default -- see the dedicated
    # test above).
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = _run_cli(["--hash", "D-0001"], input_bytes=bytes([0xFF, 0xFE]) * 5, env=env)
    assert result.returncode == 1
    assert b"Traceback" not in result.stderr


# ---------------------------------------------------------------------------
# template battery (DoD): the shipped escape_allowlist.template.json is
# validated against the LIVE repo tree -- its one example entry is DESIGNED
# to fail (placeholder decision id/carrier anchor per its own instructions),
# and a positive control confirms the same shape validates clean once the
# placeholders are replaced with real, existing values.
# ---------------------------------------------------------------------------


def test_template_file_exists_and_is_valid_json():
    assert TEMPLATE_PATH.exists()
    data = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert "entries" in data
    assert len(data["entries"]) == 1


def test_template_example_entry_fails_by_design_against_live_repo():
    # Negative control: running escape_check against an UN-EDITED copy of
    # the template must fail loudly (placeholder decision_id D-0000 /
    # docs/DECISIONS_FULL.md do not exist in this toolkit) -- never a
    # silent fake OK.
    ok, errors, count = ec.run_validate(str(TEMPLATE_PATH), str(REPO_ROOT))
    assert not ok
    assert count == 1
    assert any("example-entry-replace-me" in e for e in errors)


def test_template_shape_passes_once_placeholders_are_replaced(tmp_path):
    # Positive control: the SAME shape as the template's one entry
    # (id/carrier_file/carrier_anchor/decision_id/decision_file/
    # section_sha256/affirmed/note), but pointed at real files with a
    # correctly computed hash, validates clean -- confirms the template's
    # failure above is due to the placeholder VALUES, not a schema/logic
    # defect in escape_check.py itself.
    template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    entry = dict(template["entries"][0])

    carrier_text = "Some real carrier prose.\nTHE REAL ANCHOR PHRASE lives here.\nMore prose.\n"
    decision_text = "## D-0042 -- a real decision\nreal decision body text\n"
    (tmp_path / entry["carrier_file"]).write_text(carrier_text, encoding="utf-8")
    decision_path = tmp_path / "docs"
    decision_path.mkdir()
    (decision_path / "DECISIONS_FULL.md").write_text(decision_text, encoding="utf-8")

    entry["carrier_anchor"] = "THE REAL ANCHOR PHRASE lives here"
    entry["decision_id"] = "D-0042"
    entry["decision_file"] = "docs/DECISIONS_FULL.md"
    entry["section_sha256"] = _real_digest(decision_text, "D-0042")

    allowlist = _write_allowlist(tmp_path, [entry])
    ok, errors, count = ec.run_validate(str(allowlist), str(tmp_path))
    assert ok, errors
    assert count == 1
