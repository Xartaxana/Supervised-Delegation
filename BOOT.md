# Boot Sequence

The repository is the only source of truth.

Note: CLAUDE.md (the routing policy) auto-loads into every session on
its own — that auto-load is NOT a boot. Full state recovery is this
sequence, run on the operator's request ("restore context from
BOOT.md" or equivalent).

When starting a new session:

1. Read README.md.
2. Read SYSTEM_PROMPT.md.
3. Read DECISIONS.md.
4. Read DELEGATION_TABLE.md.
5. Read CURRENT_CONTEXT.md.

After loading these documents, produce a Boot Report per
PROCESS/BOOT_REPORT_PROTOCOL.md (the template and its rules live
there):

- summarize the current state;
- name the current milestone;
- name the next task;
- then STOP and wait for the operator's explicit confirmation.

Boot recovery is not work authorization: do not start the next task
(reading further files for implementation, writing code) until the
operator confirms.

If repository content conflicts with chat history, the repository
always wins.
