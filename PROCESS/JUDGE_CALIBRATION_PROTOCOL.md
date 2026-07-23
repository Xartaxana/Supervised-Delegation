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
   judge reviews the actual pairs. `--record-evidence` output is not
   self-certifying: no code path writes table status cells directly,
   only the weekly calibration process does, citing the evidence
   lines this run appended.

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

8. **Subscription judge-subagent procedure (leaf-routing rule 13).**
   CLAUDE.md's rule 13 recognizes a SECOND legitimate judge form
   alongside the gateway alias above: a subscription judge-subagent,
   used when there is no live proxy to call through. Calibrating it
   follows rule 7's own logic, made concrete: dispatch the subagent
   carrying `JUDGE_SYSTEM_PROMPT` (gateway/shadow_eval.py) delivered
   VERBATIM — the literal system prompt string, never paraphrased and
   never re-derived from memory ("act as an impartial judge...") —
   against every pair in gateway/judge_calibration.json, and score its
   verdicts against each pair's `verdict` key. The bar is FULL
   agreement on the set (13/13 at this file's current count) before
   the subagent is trusted for any real `accepted(basis: "judge")`; a
   single miss disqualifies it — rule 5 ("escalate on evidence") still
   governs the diagnosis, not a lowered bar. Record the run: date, the
   subagent's bound model, the agreement score, and an explicit
   confirmation that the delivered prompt was character-for-character
   `JUDGE_SYSTEM_PROMPT` — a drifted prompt invalidates the run even at
   13/13, because it calibrated a different judge than the one that
   will actually run leaf acceptances. This record is the "equivalence
   point" CLAUDE.md's rule 13 requires before either judge form is
   trusted; a leaf-class `accepted(basis: "judge")` with no such record
   on file is a self-certification finding (calibration check 20,
   PROCESS/WEEKLY_CALIBRATION_PROTOCOL.md). Re-run the same procedure
   whenever `JUDGE_SYSTEM_PROMPT` changes, the calibration set grows
   (rule 2), or a different model is bound to the subagent.

   The 13-pair set itself is NOT duplicated into a second file here:
   gateway/judge_calibration.json already IS the generic, ship-safe
   form this protocol calibrates against — labeled prompt/response
   pairs with a `verdict` key, placeholder model aliases only
   ("lead-gemini" / "intern" / "middle-groq"), no operator- or
   deployment-private detail. A parallel `JUDGE_PAIRS_SET.md` would
   only risk drifting out of sync with the one file `shadow_eval.py`'s
   own `--calibrate` flag actually reads; this protocol points at that
   file by path instead of copying its content.
