# Alert triage: closing transaction-monitoring alerts safely

A system for the Fin Crime Hackathon. It reads one institution folder of alerts with their
evidence and writes one verdict per case: `CLOSE`, `RFI` or `ESCALATE`, with a reason code,
a 2–4 sentence reason that names the case's own facts, and the IDs of the evidence it relied on.

```bash
system/run institutions/bank output/bank.jsonl
```

It is plain Python 3 (standard library only), makes no network calls, and gives the same
output byte for byte on every run. It works from any working directory and reads only the
folder it is given.

## Layout

```
system/
  run                 entry point required by the brief (bash -> main.py)
  main.py             CLI: folder in, JSONL out
  triage/
    data.py           loads and normalises the folder (all field-name aliases live here)
    evidence.py       classifies documents and reads amounts, names, dates, DOBs, postcodes
    policy.py         per-institution limits from the SOPs, reason-code catalogue
    engine.py         per-rule handlers, case-level checks, verdict + reason writing
tools/
  explore.py          prints the real schema of a folder (run this first on new data)
  validate.py         checks an output file against the brief's rules
  score.py            scores output against hand labels using the brief's cost matrix
  run_all.sh          runs every institution, validates, checks reproducibility
tests/
  make_fixtures.py    builds small synthetic bank and payments/FX folders
  test_system.py      end-to-end tests
institutions/         put the hackathon data here (git-ignored)
output/               system output (git-ignored)
```

## How it decides

**One case, one verdict.** Every alert in the case goes to the handler for its rule (TM01–TM08,
TM10, TM22). Each handler returns a finding. The case takes the most severe finding, so one
alert that isn't explained is enough to stop a `CLOSE`.

**Costs.** A wrongful close costs 10. Any other mistake costs 2 or 3. So:

- `CLOSE` needs positive evidence for **every** alert: an invoice that matches on amount, payee and
  date, a documented event, a proof of own account, a takings report that reconciles, a verified
  group company, a documented relationship, a refund of an earlier payment, a declared country,
  a refreshed KYC for a dormant account, or an earlier review that covers this activity at this
  size or larger.
- `ESCALATE` whenever there is a sign of crime: scam or recall reports, money arriving from
  many senders and leaving again within 48 hours, amounts split just under a threshold with no
  business records, an undeclared higher-risk country with nothing explaining it, an invoice
  that names the payee but doesn't match, a list match confirmed by date of birth, identifier or
  address, or several unrelated third parties. Once there is suspicion the system never sends an
  RFI, because asking the customer could tip them off.
- `RFI` when nothing is suspicious yet and one specific document or answer would settle the case.
  The reason always ends with exactly what to ask the customer for.

**Policy settings applied** (from the brief and each `POLICY.md`):

| Setting | Implementation |
|---|---|
| Own accounts | Only count when a document shows the customer holds the other account. At 5× declared income or volume or more, send `RFI_UPDATED_PROFILE`. Below that, `CLOSE OWN_ACCOUNT`. |
| Internal list | A name match alone gives an RFI. A different DOB, ID or postcode gives a close. A matching DOB or ID, or two matching details, gives an escalation. |
| Earlier reviews | They cover only the counterparty they name, at or below the amount reviewed. |
| Payments and FX invoices | A held payment of £10,000 or more to anyone but the client needs a matching invoice or contract on file. |
| Context, not triggers | Round amounts, shared devices, same-day payees, and crypto or gambling are never grounds on their own. |

Thresholds per institution are in `system/triage/policy.py`. Higher-risk countries are read
from `POLICY.md` when it lists them. Otherwise a default list is used.

**Robustness.** If an alert can't be read, or a handler raises an error, the case escalates.
A case is never dropped from the output, because a missing case counts as an escalation anyway.

## Workflow once the data arrives

The real file schema hasn't been seen yet. The loader reads every field through aliases, so
check them first:

```bash
python3 tools/explore.py institutions/bank          # field names, counts, example values
```

Add any field names it doesn't know to the `pick(...)` calls in `system/triage/data.py`.
Then run and check:

```bash
tools/run_all.sh                                     # all 4 institutions + validate + reproducibility
python3 tools/validate.py institutions/bank output/bank.jsonl
```

The development cases have no answers. To measure progress, hand-label a stratified sample
(about 50 per institution, `{"case_id", "verdict", "reason_code"}` per line) and score it:

```bash
python3 tools/score.py labels/bank.jsonl output/bank.jsonl
```

Nothing from the development cases (IDs, names, amounts) may be hard-coded into `system/`.
Tune rules and keyword lists, never specific cases.

## Tests

```bash
python3 -m unittest discover tests -v
```

## Next steps

1. Map the real schema (`explore.py`), then fix the aliases and the document-type detection.
2. Read all four `POLICY.md` files and encode any limits or exceptions that aren't in `policy.py` yet.
3. Hand-label a sample, then tune the handlers that produce wrongful closes first.
4. Optionally, add an LLM pass for reading documents, run at temperature 0 with results cached
   by case hash so the five scored runs still match. The rules engine stays the final decision-maker.
