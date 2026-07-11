# Pi harness for gateway workers

A recipe for running the `@earendil-works/pi-coding-agent` CLI as a
gateway worker (scout/builder/etc.) through this toolkit's proxy, plus
the gaps hit while getting it working and how each was closed. Numbers
and task IDs from the deployment that produced this recipe have been
generalized away -- what's kept is the reproducible mechanics and the
class of each gap, since a fresh deployment will hit its own instance
of most of these at different times.

## Installed

- `npm install -g @earendil-works/pi-coding-agent` (MIT license).
- A provider entry pointing at this gateway (Pi's own provider config,
  typically under a per-user `.pi/agent/models.json`): base URL
  `http://localhost:4000/v1`, API shape `openai-completions`, one
  model entry per gateway alias you want Pi to drive (e.g. `scout`,
  `builder`). The schema REQUIRES all four cost fields in each entry:
  input/output/cacheRead/cacheWrite.
- Start the proxy first, from `gateway/`, with your keys exported and
  `PYTHONUTF8=1` set (see `gateway/README.md` "Run").

## Working invocation form (verified)

    cmd /c "pi --provider <your-provider-name> --model <provider-name>/<alias> -p
      --mode json -nc -ns -ne --no-session --offline
      -t read,bash
      --append-system-prompt <role-file>
      @<questions-file> "<message>" < NUL > <output.json> 2>&1"

Critical details:

- `< NUL` -- in `-p` (print) mode Pi waits on EOF on an open stdin and
  hangs forever without it.
- `--mode json` -- the text-mode renderer only draws in a TTY; a
  redirected run comes back empty without this flag.
- `-nc` -- don't auto-load a repo `CLAUDE.md`; the role is supplied as
  its own file via `--append-system-prompt` instead.
- Parse the response from the JSON stream's `message_end` events, not
  by scraping stdout as text.

## Scout profile (hardened)

The system prompt worth appending for a scout-role run, after an early
attempt at a looser version let a small/weak model answer from
"memory" instead of from tool output:

    You are a scout, a repository reconnaissance agent. Rules:
    0. THE MAIN RULE: answer ONLY with facts from files you ACTUALLY
       read with tools (read/bash) in THIS run. Answering from memory
       or a guess is forbidden. Citing a file, line, or command in
       your answer or in your "Trail" that you did not actually
       read/run is forbidden. If you didn't find the answer, say so --
       "not found" -- with the list of searches you actually ran.
    1. Strict READ-ONLY mode: do not create, modify, or delete files;
       bash is for search and reading only (grep, ls, cat).
    2. Answer as a DIGEST: file:line for every claim.
    3. A negative claim is only valid with a trail behind it.
    4. Don't resolve a judgment call: facts only, plus "needs a
       decision from a tier above."
    5. End with a "Trail" block: searches run and files read.

## Post-run guard (mandatory before trusting an exam-grade transcript)

BEFORE a coordinator reads or scores a Pi worker's report, run the
deterministic fabrication detector (catches the specific failure mode
of a substantive-looking answer backed by zero structural tool calls):

    python tools/pi_run_guard.py --json <output.json>
    python tools/pi_run_guard.py --db gateway/requests.db --model <alias> --since <ts> --until <ts>

Exit codes: 0 PASS / 1 REJECTED (stamp the result rejected without
further scoring) / 2 INCONCLUSIVE (an ops abort, not a verdict on the
transcript). The `--db` window must start AND end on the answer being
scored (see the KNOWN GOTCHA in the guard's own docstring for why a
loose window silently pulls in unrelated requests). The guard's own
failure detector is a dedicated calibration check (see
`PROCESS/WEEKLY_CALIBRATION_PROTOCOL.md`).

## Known gaps and recipes

1. **Streaming tool-call deltas through the proxy.** Structured
   `tool_calls` in a streamed response can, for some providers,
   intermittently arrive broken or truncated by the time they reach
   the client through a proxy hop -- distinguishing a real provider-
   side regression from a one-off transient matters before you draw
   any conclusion about a model's tool-calling capability. Isolate it
   with `gateway/tools_stream_check.py` (`--stream`/`--no-stream`,
   `--model`; exit 0/1) -- run it fresh on any recurrence before
   trusting an observation about streaming behavior.

2. **A `provider/` litellm prefix that doesn't forward tools.** Some
   litellm provider prefixes only forward a subset of the request body
   -- if a local model bound through litellm never seems to receive
   `tools` at all (it writes tool calls as plain text inside a
   reasoning/thinking field and the harness sees an empty structured
   answer), check whether a "native chat API" prefix variant exists
   for that provider (e.g. Ollama's `ollama_chat/` vs. the legacy
   `ollama/`) and switch to it.

3. **A trimmed toolset plus an explicit token cap makes a tight
   free-tier worker viable.** A reduced tool set (e.g.
   `-t read,bash,write,edit -nc`) is measurably lighter in prompt
   tokens than Pi's default toolset -- worth measuring per role rather
   than assuming. Pair it with an explicit, conservative `maxTokens`
   cap in the provider's model entry: a provider's rate-limit
   "admission" arithmetic can count prompt tokens plus a fixed
   completion-token allowance, not the completion tokens you actually
   use, so a generous default allowance can blow a tight TPM wall on
   its own even when the real token usage is well inside it.

   *Historical TPM-ceiling diagnosis, as a worked example of the
   class:* a reasoning-capable model on a free tier can have a TPM
   budget so tight that the harness's default system prompt plus a
   single tool call already exceeds it before generation even starts
   -- a 429 "request too large" with zero tokens generated. The class:
   *the default harness system prompt's weight doesn't fit a
   free-tier model's TPM budget.* Candidates: trim the harness's
   system prompt, move to a paid tier, or bind the role to a
   different underlying model.

   *TPD addendum.* A provider's daily quota window can be a genuinely
   rolling 24h window rather than a reset at a fixed hour -- a
   used/limit report an hour after an "expected" reset can still show
   most of the previous day's usage still counted. Consequence: a
   quota-sensitive run started right after other traffic that same
   day can die against spend that a naive "it resets at midnight"
   mental model would have already forgotten.

   *Planning addendum.* Each burned chunk of quota falls out of the
   rolling window exactly 24h after the call that burned it -- the
   "try again in X" a 429 reports frees room for roughly one more
   call, not the whole budget; a multi-turn exam needs headroom sized
   to the WHOLE conversation (history grows every turn), not the next
   single call. Compute "safe to start" from the SUM across every
   local DB that could be logging traffic against the same provider
   quota (a wall that only reads one DB path is blind to traffic
   logged through a different `GATEWAY_DB_PATH`, and the provider
   itself counts all of it).

   *Launch rule* (supersedes doing any of the above arithmetic by
   hand): a quota-sensitive run starts ONLY after
   `python tools/preflight_quota.py --alias <alias> --need <estimate>`
   returns GO. A NO-GO is not argued with or retried immediately --
   either wait for the time the script names, or change the plan.
   `--probe` gets ground truth directly from the provider via a
   deliberate 429 when you have quota to spare for the probe itself.

4. **Reasoning-echo loop between a harness and a reasoning-capable
   model, through a proxy.** Some reasoning models return their
   reasoning content inside the response; some harnesses store that as
   a "thinking" block and echo it back verbatim on the next turn (as
   context); some providers REJECT that field on input -- the result
   is a 400 on every multi-turn call, even though the first turn
   worked fine. A harness-side "don't request reasoning" setting does
   not fix this on its own if the underlying model returns reasoning
   unprompted regardless of what was requested. Fix: strip/hide the
   reasoning field at the gateway layer for that alias (litellm's
   `reasoning_format: hidden` param, passed straight through to a
   Groq-style backend, is one concrete instance) so the harness never
   sees it to echo back. A single-turn role (e.g. a judge) doesn't
   need this fix -- there is no second turn to echo into, and the
   reasoning content is harmless, occasionally even useful, there.

5. **Entrance-exam fabrication under multi-question load.** A
   local or free-tier model, when pushed past its actual capability on
   a multi-question exam, can fall back to writing tool-call syntax as
   plain text (e.g. `<function/bash ...>` inline in its answer) instead
   of emitting structured tool calls, and can go on to fabricate a
   plausible-looking "Trail" section describing searches it never
   actually ran. The post-run guard above is what catches this
   deterministically -- it does not matter how plausible the
   fabricated trail reads, zero structural tool calls behind a
   substantive answer is rejected outright. Bistability (real tool
   calls on some attempts, fabricated ones on others, across repeated
   attempts at the same exam) does not rescue a passing verdict: a
   worker that silently fabricates its evidence some fraction of the
   time is incompatible with a recon/scout function regardless of how
   often it happens to behave.
