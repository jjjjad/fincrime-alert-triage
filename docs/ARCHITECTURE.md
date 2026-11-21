# RoboCop: architecture and team plan

*Your own personal police team of AI agents. Hacking has evolved with LLMs; it's time for
cybersecurity to evolve as well. Fight fire with fire.*

This is how the three of us build RoboCop in parallel and join it up. Read it once, then
pull the repo and start on your part. The pipeline diagram is in `docs/RoboCop_Alert_Triage.pdf`.
Where this file and the hackathon's `README.md` / `BRIEF.html` disagree, **the hackathon files win**.

## Team

| | Who | Owns | In one line |
|---|---|---|---|
| **A** | Jad | Plumbing, gate, integration, scoring | Keeps the system running end to end and measures how good it is |
| **B** | Hamid | Officer Tom (the rules engine) | Turns the four `POLICY.md` files into deterministic checks |
| **C** | Tony | The LLM officers (Oliver, Ivy, Daniel) | Makes the language-model layer accurate, cheap and repeatable |

## Rules from the brief that shape everything

- **Python standard library only.** No `pip install` packages inside `system/` (no `requests`,
  `pandas`, `openai`). HTTP calls use `urllib`.
- **The only LLM route is OpenRouter** (`https://openrouter.ai/api/v1/chat/completions`), with the key
  read from `OPENROUTER_API_KEY`. A run of 200 cases must cost **under ~$4** and take **under 30 minutes**,
  i.e. about 2p and 9 seconds per case.
- `system/run <folder> <output>` is called with full paths **from inside `system/`**, so nothing
  may depend on the working directory.
- **Nothing from the dev cases may be hard-coded** (case IDs, names, amounts, document IDs, verdicts).
- The system reads only the folder it is given and writes only its output file and files inside `system/`.
- **Every case gets exactly one line.** A missing case counts as an escalation.
- Scored 5 times on 800 unseen cases: same verdict and reason code every run (temperature 0, cache).
- Cost per case: a wrongful CLOSE costs **10**, a missed close costs **3**, an RFI on an escalation costs **3**,
  an escalation on an RFI costs **2**. Escalating everything scores 0.
- A demo (e.g. Streamlit) is fine for the pitch, but it must live **outside** `system/`.

## The pipeline

```
                  case bundle (loader.py)
                 /                        \
      Officer Tom (tom.py)          Officer Oliver (officers.py)
      rules from POLICY.md          LLM, sees Tom's findings
                 \                        /
           gate (main.py): agree AND both >= 0.9 sure?
          /                  |                        \
   yes: verdict stands   avg P(escalate) >= 0.2    otherwise
                         Inspector Ivy              Detective Daniel
                         ESCALATE vs RFI            RFI vs CLOSE
                                   \               /
             safety rule -> finish() checks -> one JSON line
```

1. **Officer Tom** gives a three-way verdict with a confidence for each verdict.
2. **Officer Oliver** decides the same case independently with an LLM.
3. **Gate.** If they agree and both are at least 0.9 sure, Tom's decision stands (his reason has exact facts).
4. Otherwise, if the average P(escalate) is 0.2 or more, **Inspector Ivy** chooses ESCALATE or RFI
   ("does anything look criminal?"). If not, **Detective Daniel** chooses RFI or CLOSE ("does the
   evidence explain every alert?").
5. **Safety rule: the LLM may make a verdict safer, never less safe.** If the final verdict is CLOSE
   but Tom didn't close, Tom's decision is used. A model mistake can therefore never cause a 10-point wrongful close.
6. `finish()` fixes the reason code if it doesn't fit the verdict and drops any evidence ID that
   doesn't exist. Any crash on a case writes a safe ESCALATE line.
7. **No API key, or the LLM call fails?** Tom decides alone. The system always runs.

The thresholds (0.9 and 0.2) are starting values. Jad tunes them on the hand labels.
When choosing between verdicts from probabilities, use `common.cheapest_verdict` (lowest
expected cost), not the most likely verdict. For example, CLOSE only beats RFI when P(close) > ~0.77.

## The contract: two data formats

Everyone codes against these. Change them only after telling the team.

**1. Case bundle.** `loader.load_folder(folder)` returns one dict per case:

```python
bundle = {
    "institution": "bank",           # folder name: bank, emoney, investment or payments_fx
    "policy": "...",                 # POLICY.md text
    "case": {...},                   # the line from cases.jsonl (case_id, customer_id, review_window,
                                     #   alert_ids, held_payment_id)
    "alerts": [...],                 # this case's lines from alerts.jsonl (rule_id, message,
                                     #   measured, transaction_ids)
    "customer": {...},               # the line from customers.jsonl (incl. "declared" profile)
    "transactions": [...],           # ALL this customer's transactions
    "evidence": [{"doc_id": "INV-7291", "type": "...", "title": "...", "dated": "...",
                  "text": "<the Markdown document>"}, ...],
    "previous_reviews": [...],       # this customer's earlier reviews
    "list_entries": [...],           # internal-list entries named by the case's TM22 alerts
    "valid_ids": set(...),           # every ID this case may cite as evidence
}
```

**2. Decision.** Every officer returns `common.make_decision(...)`:

```python
decision = {
    "verdict": "CLOSE",                          # CLOSE | RFI | ESCALATE
    "reason_code": "INVOICE_MATCHES",            # must be in common.REASON_CODES[verdict]
    "reason": "2-4 sentences naming this case's own documents, people and amounts.",
    "evidence": ["INV-7291", "BK-T380846439"],   # IDs exactly as in the files
    "probs": {"CLOSE": 0.95, "RFI": 0.05, "ESCALATE": 0.0},
    "officer": "tom",                            # tom | oliver | ivy | daniel
}
```

The reason is 40% of each case's score, so every decision needs real, specific wording and the
deciding document IDs, not generic text.

## Files and owners

Each person mostly edits only their own files, so merges rarely clash.

| File | Owner | What it does |
|---|---|---|
| `system/run` | A | 4-line shell script that runs `main.py` from any directory |
| `system/main.py` | A | Runs the officers, the gate, the safety rule, `finish()`, the fallback, writes the output |
| `system/loader.py` | A | Reads the folder and builds the bundles |
| `system/common.py` | A (shared) | Verdicts, reason codes, cost table, `make_decision`, `cheapest_verdict` |
| `system/tom.py` | B | Officer Tom: a list of policy checks |
| `system/llm.py` | C | OpenRouter client: `urllib`, temperature 0, seed, cache in `system/cache/` |
| `system/officers.py` | C | Prompts and parsing for Oliver, Ivy and Daniel |
| `system/README.md` | C | Required deliverable: how it decides, model, time and cost per 200 cases |
| `tools/validate.py` | A | Checks an output file: every case, fields, codes fit verdicts, IDs exist, 2–4 sentences |
| `tools/score.py` | A | Scores output against hand labels with the cost table |
| `tools/run_all.sh` | A | Runs and validates all four institutions |
| `labels/<institution>.jsonl` | Everyone | Hand labels (see below) |
| `output/<institution>.jsonl` | A | Generated by the system at the end, never hand-written |

## What each person does

### A: Jad (plumbing, gate, integration)

The skeleton already runs end to end (every case escalated, 0 validator errors). From here:

1. Keep `main` always runnable. Merge Tom and the officers as they land and re-run `tools/run_all.sh`.
2. Check the loader against all four institutions (e.g. `held_payment_id` at payments_fx, any fields
   the officers need that the bundle lacks).
3. Tune `NEAR_CERTAIN` and `ESCALATE_ROUTE` on the hand labels with `tools/score.py`.
4. Before the freeze: run 5× and confirm identical output, time a 200-case run, generate `output/*.jsonl`.

### B: Hamid (Officer Tom)

1. Read all four `POLICY.md` files. Use `tools/score.py` and the labels to see what you're missing.
2. Write one function per check in `tom.py` and add it to `CHECKS`, crime signs first:
   - invoice / contract / payment file matches the alerted payment on amount, payee and date
   - own-account transfers proven by a document, plus the **5× declared income/volume → RFI** rule
   - internal-list match: compare date of birth, address and identifiers with the registers on file
     (different party → CLOSE, listed party or controller → ESCALATE, can't tell → RFI)
   - payments_fx: **£10,000 or more** to anyone but the client needs an invoice or contract on file
   - earlier reviews cover only what they describe, never new or larger activity
   - dormant accounts, documented events, business records, refunds, group companies
3. Put a high probability (≥ 0.9) only on verdicts a rule clearly settles. Uncertain → `unsure()`.
4. Write specific reasons: name the document, the counterparty and the amount.
5. Context only, **never a trigger on its own**: round amounts, a shared device/IP/contact,
   a recipient added the same day, crypto or gambling counterparties.

### C: Tony (LLM officers)

1. Get an OpenRouter key working. Pick the model: cheap enough for 4 calls on hard cases within ~$4
   per 200 cases, and good at following a policy. Log tokens and cost per run.
2. Improve the prompts in `officers.py`. Oliver must decide independently; Ivy and Daniel each choose
   between only two verdicts. All must return valid JSON with specific reasons and real evidence IDs.
3. Keep runs repeatable: temperature 0, the cache in `system/cache/`, and no randomness.
4. Check the prompt size: `case_text` trims transactions to the review window. Trim more if cost demands.
5. Write `system/README.md` (model, time and cost for 200 cases). Own the pitch and demo if time allows.

### Everyone, first 30 minutes: hand labels

We have no answers, so we label our own. Each person labels about **20 cases per institution**
(Jad: bank + emoney, Hamid: investment + payments_fx, Tony: a mix of all four, aiming at hard ones).
This is also the fastest way to learn the policies. Format, one line per case in `labels/<institution>.jsonl`:

```json
{"case_id": "BK-CS100112", "verdict": "RFI", "reason_code": "RFI_SOURCE_OF_FUNDS", "note": "why, in a few words"}
```

Then `python3 tools/score.py labels/bank.jsonl output/bank.jsonl` gives wrongful closes, cost per case
and the verdict score.

## Working together in git

- Copy the hackathon's `institutions/` folder into the repo root. It's gitignored (not ours to commit).
- Work on `main`, commit small and often, `git pull --rebase` before every push.
- Stay inside your own files. To change `common.py` or the bundle format, tell the team first.
- Before pushing, run `tools/run_all.sh` and check it gives 0 validator problems.
- Never commit `system/cache/` (gitignored) or hand-written output.

## Timeline (code freeze 17:00)

| By | Milestone |
|---|---|
| 13:00 | Everyone has pulled the skeleton and is labelling |
| 13:30 | Labels pushed. B and C working in their own files |
| 14:30 | First real merge: Tom + Oliver + gate on all dev cases, scored against the labels |
| 15:30 | Ivy and Daniel in. Thresholds tuned on the cost score |
| 16:15 | **Feature freeze.** 5× reproducibility run, timing and cost check |
| 16:45 | `output/*.jsonl` generated and pushed, `system/README.md` complete |

**Fallback if we run short:** Ivy and Daniel become one LLM call that's told which two verdicts it
may choose between. Same story for the pitch, half the work and cost.

## Decisions still open

- Which OpenRouter model (Tony).
- Whether to commit `system/cache/` for the dev cases, so the final dev re-run is identical even if
  the model's output drifts.
