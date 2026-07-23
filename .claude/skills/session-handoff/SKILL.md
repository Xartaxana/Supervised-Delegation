---
name: session-handoff
description: Handoff check before closing a session — everything is committed and pushed across every repo this session touched, the routing journal is closed out, CURRENT_CONTEXT reflects live state, and the boot path exists and stays within budget. Run this at Session End (PROCESS/SESSION_PROTOCOL.md) or when the operator asks to "check the handoff."
---

# session-handoff — checking a session's handoff

Goal: the next session must be able to recover from the files alone,
without this chat. The check is symmetric to boot: whatever Boot
reads in the morning, handoff verifies in the evening.

## Steps

1. **Git, every repo this session touched** (this one, and any other
   repo under the same routing deployment you edited this session):
   `git status --short` is empty — commit anything uncommitted right
   now; `git log origin/<default-branch>..<default-branch> --oneline`
   is empty — otherwise `git push`. Don't sweep unfamiliar untracked
   files into a commit silently — figure out what they are first.
2. **Journal closed** (logs/routing-log.jsonl): no `delegated` without
   a paired `accepted`/`rejected`; no `lead_degraded` without a
   `lead_restored` — a degradation that survives the session must be
   the journal's last event (CLAUDE.md, "Lead degradation"). This
   session's entries were written with an Edit tool (command hygiene,
   point 5).
   2a. **Durable persistence of accepted deliverables.** For every
   `accepted(builder)` event of THIS session, the entity the witness
   names (a test, a function, a file — read out of the `witness` or
   `notes` field) exists in the COMMITTED HEAD (`git show HEAD:<path>`
   or a grep against HEAD), not merely in the working tree: a wide
   batch commit or a checkout/reset can wash out an uncommitted diff
   between acceptance and the actual commit, leaving a witness that was
   real at acceptance time with nothing behind it now (the
   deliverable-drift class). Check this BEFORE pushing; a divergence
   found gets fixed the same evening — re-commit from the working tree
   if the diff still exists, or an honest `defect_found` (`ref` pointing
   at the original `accepted`) if it doesn't — never a silent re-run
   passed off as if nothing happened. The weekly net for this check is
   its own calibration entry (PROCESS/WEEKLY_CALIBRATION_PROTOCOL.md).
3. **CURRENT_CONTEXT.md**: closed work is archived out (docs/task_
   reports/, or wherever your deployment keeps closed-work detail —
   boot-context-is-expensive rule), the queue is current. Handoff
   test: "is everything only I know now written down in a file?" —
   PROCESS/SESSION_PROTOCOL.md's rule that load-bearing knowledge
   doesn't stay only in chat.
4. **Boot budget**: measure lines/bytes for CLAUDE.md plus every file
   BOOT.md's sequence lists. Compare against the last measurement (the
   notes field of the last `calibrated` event, or the last handoff
   report). Flags: >10% growth in a single session with no explained
   cause; total size >100KB. A breach → run the boot-diet skill that
   same evening (it is the sole owner of the reaction order: archival
   unrolling and checking trimmed↔full pairs come first; deep cuts to
   operational homes need an explicit operator decision).
5. **Boot chain is alive**: every file BOOT.md's sequence names still
   exists (a rename that doesn't update BOOT.md breaks the next
   session's boot).
6. **Outcome**: a short report against points 1-5 (OK/FAIL and what
   was done). A FAIL gets fixed BEFORE the session closes, not written
   down as "later." The final commit and push are the session's last
   action.

Detector for skipping this check: the next session's Boot Report
(BOOT_REPORT_PROTOCOL.md, rule 6): a dirty tree or a divergence from
origin at start means the previous session closed without running
handoff — that gets recorded as a finding, not silently absorbed.
