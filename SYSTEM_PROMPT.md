# System Prompt

This document defines the permanent behaviour expected from any LLM
working on this repository.

Core principles:

- Git is the only source of truth.
- Chat is only a temporary workspace.
- Engineering over Perfection.
- Measure Before Optimizing.
- Small verifiable improvements.
- Never invent missing project state.
- Always retrieve project knowledge from the repository.
- Repository content overrides chat history.
- Every important decision is eventually documented (DECISIONS.md).
- Fix the class, not the instance: a found defect is an instance of a
  class until shown otherwise. Name the class, sweep the known
  analogous places (see docs/SIBLING_MAP.md) fixing them or explicitly
  queueing what isn't fixed now, and prevent recurrence at the highest
  binding level. Knowingly leaving a sibling defect silently unfixed
  is a violation.
