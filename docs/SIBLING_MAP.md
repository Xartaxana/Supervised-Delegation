# Sibling Map — symmetry axes

The tool behind "fix the class, not the instance" (SYSTEM_PROMPT.md).
Closing a defect walks paired places BY THIS MAP, not by scanning the
repository. Keep the map small; it is loaded only when closing a
defect, not at boot.

Working procedure:

1. Name the axis (axes) the defect sits on — see the list below.
2. Check and fix the paired spots on that axis, one at a time. What
   isn't fixed now goes EXPLICITLY into the queue (CURRENT_CONTEXT.md)
   or a log.
3. Class wider than the map, or no matching axis found → dispatch
   scout (or your cheapest recon tier) with a concrete question.
   Lead/critic do not re-read the repository by hand.
4. A new symmetry appears (a new deployment, a new paired document, a
   new code/accounting pair) → add an axis here in the SAME commit.

critic checks class-wide completeness of a fix BY THE MAP: "are the
axes this defect sits on covered" — a bounded question against the
diff and the map, not against the whole codebase.

## Axis 1 — Policy <-> role files <-> journal (this deployment)

| Component | Path |
|---|---|
| Routing policy | CLAUDE.md |
| Role profiles | .claude/agents/{scout,builder,critic}.md |
| Model binding | delegation.config.yaml |
| Routing journal | logs/routing-log.jsonl |

A rule change in CLAUDE.md that touches a role's duties (what it must
report, verify, or must not do) is checked against that role's profile
in the same commit. A binding change in delegation.config.yaml never
changes rule text — CLAUDE.md and the profiles speak in function names
only (scout/builder/critic/lead), never in model names.

## Keeping the map up to date

This map has exactly one owner: whoever is running Lead-tier work in
this deployment. A new symmetry noticed but not fixed now is recorded
here as a queued axis, not left to be rediscovered later. An axis
whose symmetry has disappeared (a paired file deleted, a mechanism
retired) is removed in the same commit that removes the symmetry.
