# Adoption Ledger — <project/deployment name>

Template shipped by the toolkit itself; filled in by the onboarding
skill's "Ledger" step (`.claude/skills/onboarding/SKILL.md`), right
after the intake inventory step. One row per kit MECHANISM, named by
the kit's own NOMENCLATURE (its CLAUDE.md rules, PROCESS docs, tool
names) — not by an enumeration of whichever files a given install
happened to copy. That choice is deliberate (see the greenfield note
below): a nomenclature-based ledger can be drafted straight from
reading the toolkit's own docs, independently of exactly when the file
copy itself lands, instead of waiting on it.

## Statuses

- **adopt** — the mechanism is installed and used as shipped (cosmetic
  adaptation — naming, paths — doesn't change the status).
- **native-equivalent** — the FUNCTION the mechanism serves is already
  closed by something the host already has; the kit's own version is
  NOT installed for that surface, and the correspondence is written
  down here so the substitution doesn't silently drift out of sync
  with either side losing track of it.
- **deferred(\<trigger\>)** — the mechanism has no installed
  prerequisite yet (see the onboarding skill's PREREQUISITES table);
  name the exact condition that reopens the row — a fixed defect, a
  contour change, a volume threshold crossed — never a bare "later".
- **rejected** — the mechanism actively conflicts with something the
  host needs, by Rule #1 (its cost/friction outweighs what it buys
  here); name the conflict.

A mechanism is installed WITHOUT its own prerequisite by construction
never: either the whole bundle (mechanism + prerequisite) goes in
together as **adopt**, or the row is **native-equivalent** /
**deferred(\<trigger\>)** instead. A blank status is not a legal state
— every row resolves to one of the four above, even when the answer is
the default "nothing to compare against here, adopt."

## Example row — native-equivalent (anonymized precedent)

| Kit mechanism | Status | Basis / trigger |
|---|---|---|
| Leaf-routing judge acceptance (rule 13) | native-equivalent | the host's own deterministic pipeline gates already close the leaf-class acceptance function for one class of content, on a rule-triggered path; installing the kit's judge machinery on top of that surface would open a second acceptance path for a function the host already closes — correspondence recorded here; re-open only if the host retires those gates |

(The row above generalizes a real adoption precedent without naming
the source deployment: a host running its own AI-agents-on-rules
pipeline had already closed one class of leaf-work acceptance natively
before this kit was ever considered for it.)

## Greenfield note

An empty intake inventory — every host FUNCTION row blank (acceptance /
journal-accounting / escalation / isolation / calibration, per the
onboarding skill's step 0) — means every mechanism's status is
**adopt**: there is nothing at the host to compare against, so the
bundles install whole. The ledger is still created in this case, even
though every row says the same thing: it exists as the place a project
that starts greenfield and later grows its OWN mechanisms records a
future native-equivalent/deferred, instead of that divergence living
only as undocumented drift discovered by accident. Because the form
here is nomenclature-based rather than file-listing-based, this
greenfield ledger can be written before, or independent of, the actual
template copy landing on disk — straight from this template plus the
toolkit's own docs.

## Kit snapshot revision (D-0091)

Kit snapshot revision: `<commit/tag of the toolkit snapshot this
ledger was last reconciled against>` — written at install, updated by
every upgrade batch in the same move. An upgrade re-inventories by
the REVISION DELTA (role-file CONTENT included, not just `model:`
frontmatter); ledger completeness is checked against the CURRENT
template's row nomenclature — a template row missing from this ledger
entirely is part of the delta. No recorded revision = pre-versioning
install: record the current kit revision and treat the whole ledger
as the delta, once.

## Rows

One row per mechanism the onboarding skill's PREREQUISITES table lists
(plus any host-specific mechanism your own intake inventory surfaces —
add rows freely, never remove the standard ones without recording why):

| Kit mechanism | Status | Basis / trigger |
|---|---|---|
| Routing policy (CLAUDE.md core rules, Role != tier, Lead degradation, command hygiene) | | |
| Role profiles (`.claude/agents/{scout,builder,critic}.md`) | | |
| Model binding (`delegation.config.yaml`) | | |
| Routing journal + validator (`logs/routing-log.jsonl`, `tools/journal_validator.py` pre-commit, `tools/journal_echo.py` PostToolUse) | | |
| Mechanism gate + symmetry map (`tools/mechanism_gate.py`, `.githooks/commit-msg`, `docs/SIBLING_MAP.md`) | | |
| Tier verification / SessionStart (`tools/session_context.py`, `tools/tier_echo.py`) | | |
| Wiring integrity check (SessionStart wiring check reconciling this ledger's adopt rows against actual hooksPath/settings — D-0092) | | |
| Dispatch gate / critic snapshot (`tools/dispatch_gate.py`, `tools/critic_snapshot.py`) | | |
| Hygiene gate (`tools/hygiene_gate.py`) | | |
| DoD track / gate (`tools/dod_track.py`, `tools/dod_gate.py`) | | |
| Main gate / Stop hook (`tools/main_gate.py`) | | |
| Calibration / usage tooling (`tools/calibration_counts.py`, `tools/savings_report.py`, `tools/usage_report.py`, `tools/preflight_quota.py`) | | |
| Permission audit (`tools/permission_audit.py`) | | |
| Non-Claude worker guard (`tools/pi_run_guard.py`, `gateway/PI_HARNESS.md`) | | |
| Leaf-routing judge acceptance (rule 13, `gateway/judge_calibration.json`, `PROCESS/JUDGE_CALIBRATION_PROTOCOL.md`) | | |
| Skills (`.claude/skills/*`) | | |
| PROCESS docs (`PROCESS/*.md`) | | |
| Boot sequence / decision log / delegation table (`BOOT.md`, `DECISIONS.md`, `DELEGATION_TABLE.md`) | | |
| Gateway / api-keys contour (`gateway/*.py`, `gateway/*.template.yaml`) | | |

Rows with no plausible native-equivalent and no missing prerequisite
default to **adopt** — leaving the status blank is not itself a
decision; "nothing to compare against here" IS the adopt decision, and
gets written down as one.

This ledger is a first-class artifact, not scratch notes: once a
deployment runs its own calibration process, drift between this
ledger's rows and the mechanisms actually live on disk is that
process's job to catch, the same way `DELEGATION_TABLE.md` statuses
are audited — see this toolkit's `PROCESS/WEEKLY_CALIBRATION_PROTOCOL.md`
if you've adopted it.
