# Delegation Table

A living document. Every row starts as an estimate and is refined by
your own usage as you delegate (see CLAUDE.md rules 1-11 for how a
task gets routed; this table is what "which tier" resolves against).

Status values (four-state model):

- `estimated` — expert prior, not yet measured;
- `provisionally_validated` — confirmed on a small sample or a short
  window of real use;
- `production_validated` — confirmed on real traffic with sufficient
  volume and task-level cost tracked; only this status should justify
  routing real traffic without a second look;
- `rejected` — delegation attempted and found harmful (wrong results,
  or costlier once retries are counted).

Cost = typical Lead token spend on this task type if Lead did it
itself. Value = how much frontier intelligence actually improves the
result for this task type.

| Task type | Cost (Lead) | Value of Lead | Delegate to | Status |
|---|---|---|---|---|
| Strategic planning, architecture | High | Very high | Lead only | estimated |
| Research, hard debugging | High | Very high | Lead only | estimated |
| Decomposition, spec writing, acceptance | High | Very high | Lead only | estimated |
| Repo search, file reading, context gathering | Medium | Low | scout | estimated |
| Implementation to a written spec, tests | High | Medium | builder | estimated |
| Code review, unclear-bug debugging | High | High | critic | estimated |
| Routine code generation | High | Medium | builder | estimated |
| Summarization | Medium | Medium | scout | estimated |
| Data extraction, format conversion | Medium | Low | scout | estimated |
| Classification, tagging | Low | Low | scout | estimated |
| Dispatch of an already-scoped task to a tier | Low | Low | a deterministic rule, not an LLM call | estimated |

Flat delegation (CLAUDE.md): workers never spawn workers. Parallelism
means Lead launches several workers with independent specs; a worker
that finds its task decomposable escalates back rather than splitting
the task itself.

## Update Rules

1. A row changes status only with evidence attached — a calibration
   run or a production incident — never on a hunch.
2. New task types are added as they appear in your own routing
   journal.
3. Track your biggest suspected cost driver as its own row (for
   example, "re-explaining known context") and retire the row once
   you've addressed it.
4. Compare TOTAL task cost, including retry loops, not per-request
   cost: a cheaper model needing ten retries can cost more than a
   frontier model needing one.
