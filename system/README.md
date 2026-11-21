# RoboCop alert triage

`system/run <institution folder> <output file>` reads one institution folder and writes one JSON
line per case: a verdict (`CLOSE`, `RFI` or `ESCALATE`), a reason code, a 2–4 sentence reason and
the IDs of the evidence relied on.

## How it decides

RoboCop is a team of "officers":

1. **Officer Tom** (`tom.py`) is a deterministic rules engine written from each institution's
   `POLICY.md`. He says which verdicts are still possible: CLOSE, CLOSE or RFI, RFI, RFI or ESCALATE, or ESCALATE.
2. **Officer Oliver** (`officers.py`) is a language model that checks the same case independently,
   with Tom's findings as context.
3. **Gate** (`main.py`). If Tom is sure and Oliver agrees with near certainty, that verdict stands.
4. Otherwise a specialist chooses between the two verdicts still in play:
   **Inspector Ivy** (ESCALATE or RFI) if there is any real chance of escalation, else
   **Detective Daniel** (RFI or CLOSE).
5. **Safety rule.** A language model may make a verdict safer, never less safe: nothing is closed
   unless Tom left CLOSE open. If anything fails, the case is escalated, never dropped.

## Model, time and cost

- Model: `openai/gpt-4.1-mini` via OpenRouter, temperature 0, fixed seed, answers cached in
  `system/cache/`. One call per case for Oliver, plus one for Ivy or Daniel on cases the gate
  does not settle. Failed calls are retried twice and never cached.
- Time for 200 cases: 2 minutes 13 seconds, measured on the first 200 bank cases with an empty
  cache (cases are decided 8 at a time). A repeat run reads the cache and takes about 1 second.
- Cost for 200 cases: $1.12, measured on the same run (332 calls, about 9,400 prompt tokens each).
  `python3 system/llm.py` totals the token and cost log in `system/cache/usage.jsonl`.
- Repeatability: the repeat run gave a byte-identical output file.
- Without `OPENROUTER_API_KEY`, the system runs on Tom alone.
