# Judge Calibration Protocol

## Purpose

Shadow Evaluation verdicts are only as trustworthy as the LLM judge
producing them. The judge is itself a delegated worker and is
supervised the same way the architecture supervises everything else:
a cheap model does the routine work, the Lead escalates on evidence
(Rule #1: supervision must cost less than the savings it produces).

Origin: the first judge (a mid-tier model) passed most, but not all,
of a small calibration set and was adopted; its one miss was
misdiagnosed as strictness. Asking the judge to explain revealed it
had hallucinated a bug while tracing perfectly correct code. Prompt
tuning did not help; the judge was replaced with a stronger model,
which passed the full set. A point-in-time calibration on a small
synthetic set is a snapshot, not a guarantee.

## Roles

- **Judge** — the gateway alias passed to `shadow_eval.py
  --judge-model` (whichever model your delegation.config.yaml binds
  to this role). Rules on every replayed pair.
- **Chief judge** — the Lead-tier model working on the repository (or
  a higher authority than Lead, if your deployment has one). Rules
  only on escalations and audits.

## Rules

1. **Status changes require review.** A judge verdict that CHANGES a
   DELEGATION_TABLE.md row status is accepted only after the chief
   judge reviews the actual pairs. `--update-table` output is not
   self-certifying.

2. **Reviews grow the calibration set.** Every pair the chief judge
   reviews (rule 1 or rule 3) is appended to
   `gateway/judge_calibration.json` with the chief judge's label and
   rationale. The set thereby tracks the real traffic distribution
   instead of staying a synthetic snapshot.

3. **Random audit per run.** On every Shadow Evaluation run, the
   chief judge reviews 1–2 randomly chosen verdicts even when no
   status changed. Quiet wrong verdicts never surface otherwise —
   they only get caught when someone happens to spot-check a run that
   changed nothing downstream.

4. **Recalibrate on growth.** After every ~5 pairs added to the
   calibration set, re-run
   `python shadow_eval.py --calibrate judge_calibration.json
   --judge-model <your judge alias>` and record the agreement in
   CURRENT_CONTEXT.md.

5. **Escalate on evidence, not anxiety.** If agreement on the full
   set drops below 90%, diagnose before tuning: ask the judge to
   explain its mismatched verdicts (a one-off diagnostic prompt, not
   the production prompt). Only then decide between a prompt fix and
   a model upgrade. A stronger default judge is adopted only with a
   measured failure in hand, per Measure Before Optimizing.

6. **Judge is never a traffic source.** The judge alias must not be
   used as `--source-model`; `sample_requests()` additionally filters
   judge prompts by their first sentence (keep that sentence stable
   when editing JUDGE_SYSTEM_PROMPT).

7. **A new measuring instrument is calibrated with a known-tier
   control run before its verdicts are used.** Any new judge, golden
   set, exam, or metric first runs on a candidate whose tier is
   already known; saturation by a lower-tier control means the
   instrument does not discriminate at the top (a recurring failure
   pattern: an exam that saturates under a mid-tier candidate, or a
   pre-registered gap threshold that fires on its very first run).
   The instrument's own failure detector remains mandatory under the
   four-questions-per-mechanism rule (question (c)).
