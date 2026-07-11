---
name: next-task
description: Take the highest-priority task and drive it through the routing policy — verify your tier, choose from CURRENT_CONTEXT, dispatch independent parts to workers in the background, accept by evidence, commit as you go. Run this on "do the next task," "continue by priority," "next task." Invoking this command counts as work authorization (it satisfies the Boot Report protocol's STOP).
---

# /next-task — the highest-priority task

The command sets the ORDER of steps. The rules live only in CLAUDE.md
(auto-loaded); where the two disagree, CLAUDE.md wins — rules are not
duplicated here.

0. **Tier check** (tier-verification-at-entry rule; role-vs-tier
   acceptance matrix, CLAUDE.md "Role ≠ tier"). Check your actual
   model against the top tier bound to Lead. Lower → work by the
   matrix: coordinate and dispatch; accept only tiers below your own
   (an equal tier only through critic input); Lead-class work
   (mechanisms, the decision log, statuses, gates, accepting an
   equal/higher tier) — copy as explicit lines into the full Lead's
   queue in CURRENT_CONTEXT.md; log the degradation window if one
   applies (CLAUDE.md "Lead degradation": `lead_degraded` /
   `lead_restored`). The command runs from any tier — running it does
   not make the session a Lead.
1. **Context.** If this session hasn't booted yet — run BOOT.md and
   produce the Boot Report first; invoking this command counts as the
   operator's explicit confirmation (rule 4 of the Boot Report
   protocol is satisfied) — don't wait for a separate "yes".
2. **Priority.** (a) A Current Task is assigned in CURRENT_CONTEXT.md
   (single-current-task rule) → that one first. (b) Otherwise — the
   first EXECUTABLE item in the Queue section, honoring any
   recommendations recorded there. (c) Items that need an operator
   decision (signing off a gate, choosing a direction) are not yours
   to take — note it in the report and take the next one. Name your
   choice in the first message of the turn.
3. **Dispatch downward — immediately, in the background**
   (background-by-default rule). Hand off any part of the task that
   maps to a worker tier BEFORE starting your own part: each dispatch
   gets a spec with a DoD (DoD-in-every-dispatch rule); get the
   task_id by re-reading the journal's tail (no-silent-reuse rule);
   parallel dispatches declare which paths they own; write the
   `delegated` event before launching.
4. **While workers run** — do the coordinator's own part yourself;
   between acceptances you may close small, point items from the
   queue (self-reading follows CLAUDE.md rule 1, with a
   `dispatch_skipped` event).
5. **Accept each worker's result as it completes** — in the form its
   tier requires (CLAUDE.md rules 1-3: trail / witness / critic input)
   and per the matrix from step 0. A rejection → `rejected` with a
   `failure_class`; two failures on the same tier → escalate (CLAUDE.md
   rule 6).
6. **Commit as you go:** one conceptual change per commit; mechanism
   paths → CLAUDE.md rule 10 plus the axis block (axis-enumeration
   rule); editing a tier's role file when it has a golden set → run
   the set before the commit (exam-before-shipping-a-worker-change
   rule).
7. **End of turn:** a short report (done / queued / waiting on the
   operator), CURRENT_CONTEXT updated. Don't start a task beyond the
   one you chose (single-current-task rule); exception: queue items
   explicitly marked "on next touch," if that touch happened this
   turn.

Failure detector (CLAUDE.md rule 10, clause (c)): this command only
orders existing mechanisms — its output (journal events, commits,
CURRENT_CONTEXT) is what your weekly calibration's journal-completeness
and commit-discipline checks audit (see PROCESS/WEEKLY_CALIBRATION_PROTOCOL.md
checks 1/3/6/13); picking a task against priority shows up in the next
session's Boot Report as a mismatch between the recorded Current Task
and the actual commits (single-current-task rule).
