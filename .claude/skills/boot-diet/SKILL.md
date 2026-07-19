---
name: boot-diet
description: Reaction procedure for a boot-budget breach (>100KB total, or >10% growth in a session) — archival unrolling of boot files, confirming trimmed versions haven't diverged from their full counterparts, and that archiving was done correctly; deep cuts to operational homes require an explicit operator decision. Run this when a breach is flagged by session-handoff's check 4, by the Boot Report, or by an in-session measurement.
---

# boot-diet — reacting to a boot-budget breach

Boot context is a paid resource (CLAUDE.md, boot-context-is-expensive
rule): every session reads it. This skill is the sole owner of the
reaction order to a breach; session-handoff's check 4 triggers it and
does not duplicate its steps. The order is strict: cheap, reversible
moves (archiving, deduplication) BEFORE expensive, irreversible ones
(cutting into operational homes). In a FRESH session, this skill runs
only AFTER the Boot Report has been delivered and the operator has
given the word to proceed — a flagged breach is a line for the Boot
Report's Next Required Action, not a self-authorizing trigger to start
the diet before anyone has seen the report.

## Steps

1. **Measure and diagnose.** Total size of CLAUDE.md plus every file
   BOOT.md's sequence lists (bytes, per file). Compare against a
   baseline — the last handoff report, the notes field of the last
   `calibrated` event, or the commit of the previous diet. Name WHICH
   file grew and by how much: treat the cause of the growth, not the
   symptom.
2. **Archival unrolling** of boot files that have an archival home:
   - CURRENT_CONTEXT.md → docs/task_reports/ (boot-context-is-expensive
     rule; this pairing ships with the template);
   - any other boot file your deployment has since given an archival
     home — check docs/SIBLING_MAP.md for the pairing (the template
     ships with only the CURRENT_CONTEXT.md pairing above; if you
     later split, say, DELEGATION_TABLE.md's evidence log into its own
     file, that pairing belongs on its own SIBLING_MAP axis, added in
     the same commit that created the home);
   - a new boot file with an archival home is added to this list in
     the same commit that created the home.
   Do a targeted pass keyed on closure markers (DONE / CLOSED / LANDED
   / FOLDED / RETRACTED): every closed piece moves out with its full
   text, leaving a one-line pointer in its place. Do not declare
   "mechanical cuts exhausted" while live, unarchived closures remain
   — a diet that flags renewed breach as exhaustion when an archival
   pass would still recover real budget is a known failure mode of
   this step, not evidence the step is done.
3. **Archiving was done correctly** (evidence is never deleted, only
   moved): the move is VERBATIM — the commit diff shows a move, not a
   rewrite; a pointer remains where every moved piece used to live; if
   you keep an archive index (e.g. docs/task_reports/README.md), this
   step is the one that updates it; open remainders of an otherwise-
   closed item are NOT swept into the archive along with the closed
   part (a class of its own: don't let live loose ends ride along with
   a closed item's archival).
4. **"Trimmed ↔ full" pairs haven't diverged.** The template ships
   with NO such pair by default: DECISIONS.md is a single file, and
   there is no separate full-length ARCHITECTURE document. If your
   deployment hasn't created a trimmed/full split, say so explicitly
   in the report ("n/a: no trimmed/full pairs yet") — don't skip this
   step silently. If you HAVE created one (for example, you split a
   short boot-loaded core out of a longer rationale document): confirm
   entry counts / ordering still match between the two sides; if the
   full side was edited more recently than the core (compare `git log
   -1` on both), review the full side's diff since the core's last
   edit and answer "does the core need this too"; and confirm every
   path in the pair still exists — the same liveness check your
   weekly calibration runs on a schedule, done here as a point check
   triggered by the breach.
5. **Deduplicate ownership:** content living in two boot files at
   once gets a single owner plus a pointer from the other (a common
   failure mode: an "Archive" section left inside CURRENT_CONTEXT.md
   that duplicates an index that already lives in
   docs/task_reports/README.md).
6. **Re-measure.** Budget restored → go to step 7. Still breached →
   put "deep cuts" (operational homes: CLAUDE.md, SYSTEM_PROMPT.md,
   and any other file your BOOT.md sequence loads every session) into
   CURRENT_CONTEXT.md's queue as an explicit operator decision — this
   is not yours to do unilaterally. Rationale for the caution: policy
   that isn't auto-loaded gets silently skipped (CLAUDE.md's opening
   rationale, the cheapest-tier-default rule) — don't cut an
   operational home on your own judgment.
7. **Report and commit:** before/after per file, what was moved,
   verdicts on the pairs (step 4), remaining queue. A commit touching
   mechanism paths needs CLAUDE.md rule 10's axis block, or an
   explicit line "axes: not a mechanism (<reason>)".

## Detectors

Skipping this skill itself on a breach is caught by session-handoff's
check 4 (it measures on every close-out) and by the measurement line
in the next session's Boot Report. Substantive drift of a trimmed core
away from its full counterpart, once you've created such a pair, is a
boot-recovery failure the same way a broken BOOT.md path is — your
Zero Context Recovery discipline (a fresh session must resume from the
repo alone, see BOOT.md) catches it, not this skill. Growth of a live
file between calibrations is what your weekly calibration's
growth-tracking check watches (check 10 in this template's numbering).
