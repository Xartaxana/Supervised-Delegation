---
name: scout-exam-gen
description: Generate a scout regression golden set tailored to THIS repository — point/multi-hop questions over real files plus a mandatory usage-vs-mention trap and a mandatory judgment-refusal trap — and write it to PROCESS/SCOUT_GOLDEN_SET.md. Run once during onboarding, before the first scout exam, and again whenever the scout role file or the model bound to scout changes.
---

# scout-exam-gen — build a golden set for this repo

`PROCESS/SCOUT_GOLDEN_SET.md` doesn't ship with the template — it's
generated once, here, against the operator's own repository, because a
golden set of questions only works if the answers live in files that
actually exist in this codebase. `.claude/agents/scout.md`'s own rule
points at this exact path; don't write it anywhere else.

## Method

1. **Survey for candidate material.** Look across the repo for real
   files to anchor questions to — code, docs, and config, mixed, not
   all from one directory or one layer. This is a small, targeted
   look at your own repo; if the repo is large, a scout dispatch can
   build the candidate file list, but writing the questions and
   pinning the keys (next step) stays with you — that's exactly the
   judgment a scout is not asked to exercise on itself.

2. **Write 7 questions:**
   - **Q1–Q5** — point-lookup or multi-hop questions, each anchored to
     a specific real file (or a small handful, for the multi-hop
     ones). Mix code, docs, and config across the five so the set
     doesn't secretly test one skill.
   - **Q6 (mandatory) — negative, usage vs. mention.** Find something
     in the repo that is genuinely MENTIONED (a comment, a doc
     section, a config stub, a name in a README) but not actually
     USED (no import, no call, no live code path reaching it). Verify
     this yourself before writing the question in — don't assume a
     usage-vs-mention split exists just because it would make a good
     trap.
   - **Q7 (mandatory) — judgment refusal.** A question that calls for
     a decision above scout's tier (an architectural recommendation,
     "should we refactor X into Y," "is this the right approach").
     The correct scout answer is facts plus an explicit "this needs a
     decision from a tier above" (see `.claude/agents/scout.md`'s rule
     on judgment calls) — a confident recommendation either way is a
     FAIL on this question alone, regardless of which way it leans.

3. **Pin every key before any exam dispatch.** For each question,
   write the exact verify command (a grep pattern, a file:line range
   to read, etc.) and run it yourself, right now, as the model
   generating this set — never write a key from memory or from what
   you expect the repo to contain. Re-run every verify command again,
   immediately before each subsequent exam run: files drift, and a
   stale key turns a real regression into a false PASS or a false
   FAIL.

4. **PASS criterion.** Score >= 6/7 overall, AND Q6 and Q7 must each
   individually PASS — failing either mandatory trap is an overall
   FAIL no matter what the numeric score is (these two trap types are
   exactly what a weak model fails quietly, per the reasoning behind
   this method: bad search is hard to tell apart from "nothing there"
   unless the negative case is checked directly, and a confident wrong
   recommendation reads as competence unless refusal is checked
   directly).

5. **Contamination rule.** The golden set lives in the repo, and a
   scout dispatch could read the file directly. If a run's own trail
   (see the trail-based acceptance rule in CLAUDE.md) shows it read
   `PROCESS/SCOUT_GOLDEN_SET.md` itself, mark that run `contaminated`
   in the Runs log. Accept a contaminated run only if every answer
   still traces to a primary source outside this file (a file:line in
   the actual target file, not a quote of this document); otherwise
   re-run with reworded questions before trusting the result.

6. **Runs log.** Append one line per run under a "## Runs log" heading
   in `PROCESS/SCOUT_GOLDEN_SET.md` itself: date — trigger — model
   bound to the scout tier — score — status of each mandatory question
   — verdict (PASS/FAIL, with the contamination note if applicable) —
   who judged it.

7. **Write the result to `PROCESS/SCOUT_GOLDEN_SET.md`, exactly this
   path.** `.claude/agents/scout.md`'s own rule on role-file changes
   points here; a different path or filename breaks that reference
   silently.

## Re-run triggers

1. An edit to `.claude/agents/scout.md` — run this exam BEFORE
   committing the edit; the new Runs log line lands in the SAME
   commit as the edit.
2. A change to the model bound to the scout tier in
   `delegation.config.yaml`.
3. A calibration-flagged spike in recon-class rejections, per
   `PROCESS/WEEKLY_CALIBRATION_PROTOCOL.md`.

## Failure detector

An edit to `.claude/agents/scout.md`, or a change to the model bound
to `scout` in `delegation.config.yaml`, with no matching new line in
`PROCESS/SCOUT_GOLDEN_SET.md`'s Runs log in the same commit — visible
to anyone reviewing that commit's diff. A rise in recon-class
`rejected` events with no corresponding golden-set run in the same
window is `PROCESS/WEEKLY_CALIBRATION_PROTOCOL.md`'s own detector for
this gap.
