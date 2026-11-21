# RoboCop alert triage

> Draft. Owner: Tony (C). Fill in the model, time and cost before the 17:00 freeze.

`system/run <institution folder> <output file>` reads one institution folder and writes one JSON
line per case: a verdict (`CLOSE`, `RFI` or `ESCALATE`), a reason code, a 2–4 sentence reason and
the IDs of the evidence relied on.

## How it decides

RoboCop is a team of "officers":

1. **Officer Tom** (`tom.py`) is a deterministic rules engine written from each institution's
   `POLICY.md`. He gives a first three-way verdict and how confident he is.
2. **Officer Oliver** (`officers.py`) is a language model that checks the same case independently,
   with Tom's findings as context.
3. **Gate** (`main.py`). If Tom and Oliver agree and both are near certain, that verdict stands.
4. Otherwise a specialist chooses between the two verdicts still in play:
   **Inspector Ivy** (ESCALATE or RFI) if there is any real chance of escalation, else
   **Detective Daniel** (RFI or CLOSE).
5. **Safety rule.** A language model may make a verdict safer, never less safe: nothing is closed
   unless Tom closed it. If anything fails, the case is escalated, never dropped.

## Model, time and cost

- Model: TODO (via OpenRouter, temperature 0, answers cached in `system/cache/`).
- Time for 200 cases: TODO.
- Cost for 200 cases: TODO.
- Without `OPENROUTER_API_KEY`, the system runs on Tom alone.
