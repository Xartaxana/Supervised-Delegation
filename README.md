# Supervised Delegation

Supervised Delegation is policy-driven supervised delegation across
model tiers: the policy IS the router. A written routing policy
(CLAUDE.md) sends each piece of work to the right tier — recon,
implementation, review, coordination — and acceptance is
evidence-gated: every accepted result carries a trail (what was
checked), a witness (the actual verification output), or a critic
verdict, never a self-certified "looks fine."

The method's rationale and evidence base — the white paper, the
decision log, and the findings each rule grew from — live in the
development repository:
[Operating-System-for-LLMs](https://github.com/Xartaxana/Operating-System-for-LLMs).

## Quick Start (10 minutes)

1. Install the template — see INSTALL.md for the two paths (a new
   project from scratch, or layering onto an existing one).
2. Answer one onboarding question: **"Working on a Claude Code
   subscription, or on a set of API keys from different providers?"**
   (Answering "both" is valid too.) Your answer picks the contour and
   nothing else changes.
3. From there: the onboarding flow binds models to roles in
   `delegation.config.yaml`, runs an entrance exam on each bound
   model, and produces your first Boot Report. A failed exam doesn't
   block you — you get a plain warning and a choice: swap the model,
   or keep it anyway (exam failures land in your decision log, not
   silent).
4. After that, delegation runs itself. `delegation.config.yaml` holds
   the model bindings; `CLAUDE.md` holds the routing rules themselves.

## When it pings you

Everything else runs in the background: routine dispatches, the
routing journal, session-start context. The system interrupts you only
for:

1. Two failed acceptances in a row at the top tier, with nowhere left
   to escalate.
2. A budget or quota breach.
3. A failed exam — at onboarding, or when swapping a model into a
   role.
4. A weekly calibration digest — one message.

## Files

- `INSTALL.md` — both installation paths.
- `delegation.config.yaml` — the one place models are bound to roles.
- `CLAUDE.md` — the routing policy itself.
- `BOOT.md` — how a session restores context.
- `docs/TWO_VOCABULARIES.md` — the function vocabulary (routing rules)
  vs. the grade vocabulary (models/accounting), and the reference
  model bindings for each channel (D-0062/D-0085).
