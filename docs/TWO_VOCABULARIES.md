# Two Vocabularies: Functions and Grades (D-0062)

The routing policy and the accounting layer name tiers differently,
and the difference is load-bearing. CLAUDE.md's rules speak ONLY the
FUNCTION vocabulary (scout / builder / critic / judge / Lead); grades
(intern / junior / middle / senior — a price/capability ladder) belong
to models and accounting, never to a rule.

| Function (canonical policy name) | Duty | Subscription-channel default | API-channel default |
|---|---|---|---|
| **scout** — recon | search/read, digest with a trail | Haiku subagent | Haiku alias (D-0085 reference ladder, ~$1/$5 per MTok) |
| **builder** — implementation to a written spec | code/tests, witness | Sonnet subagent | Sonnet alias (D-0085, ~$3/$15 per MTok; Opus-as-builder rejected by Rule #1 evidence) |
| **critic** — review | verdict with a trail | Opus subagent | Opus alias (D-0085, ~$5/$25 per MTok) |
| **judge** — leaf acceptance (rule 13) + equivalence verdicts | verdict vs pinned intent keys; calibrated instance only | subscription judge-subagent, pinned prompt (rule 13's second judge form) | `judge-sonnet` alias (D-0085 calibration home; needs `drop_params: true` — Sonnet rejects `temperature=0`) |
| **Lead** — decomposition, specs, graph acceptance | coordinator; full authority only on the Lead tier | Fable session | Fable alias (D-0085, ~$10/$50 per MTok) |

Function names classify WORK and carry duties (trail, witness, flat
delegation); every routing rule references functions and none
references a grade — a deployment supplies the function-to-model
binding, the rule text never changes. Rebinding any function to a
different model, on either channel, requires a new evidence line, not
a silent edit (D-0085).
