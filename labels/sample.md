# Hand-label sample

Built by label-drafter with `random.Random(42)`: each institution's cases (minus any already in labels/*.jsonl) are shuffled once, then picked greedily, hard cases first then singles. Score: new rule_ids covered, then an unmet requirement (previous reviews; held payment at payments_fx), then an unused alert mix; ties go to the earlier case in seeded order. At most 2 cases per identical alert mix.

Difficulty: *single* = one alert (not TM22); *hard* = 3+ alerts or a TM22 list match.

## Counts

| Institution | Cases | Single | Hard | With previous reviews | With held payment | Rules covered |
|---|---|---|---|---|---|---|
| bank | 8 | 4 | 4 | 3 | 0 | TM01, TM02, TM03, TM04, TM05, TM06, TM07, TM22 |
| emoney | 8 | 4 | 4 | 3 | 0 | TM01, TM02, TM03, TM04, TM05, TM06, TM07, TM22 |
| investment | 7 | 3 | 4 | 3 | 0 | TM01, TM02, TM06, TM07, TM08, TM10, TM22 |
| payments_fx | 7 | 3 | 4 | 4 | 4 | TM01, TM02, TM03, TM04, TM06, TM07, TM08, TM22 |

## Cases

| Institution | case_id | Alert rule_ids | Why picked |
|---|---|---|---|
| bank | BK-CS486474 | TM01, TM02, TM05 | hard: adds rule coverage TM01,TM02,TM05; previous reviews on file (has previous reviews) |
| bank | BK-CS778624 | TM01, TM02, TM04 | hard: adds rule coverage TM04 (has previous reviews) |
| bank | BK-CS806039 | TM22 | hard: adds rule coverage TM22 |
| bank | BK-CS124109 | TM01, TM02, TM05 | hard: next in seeded order |
| bank | BK-CS911291 | TM06 | single: adds rule coverage TM06 |
| bank | BK-CS409082 | TM07 | single: adds rule coverage TM07 |
| bank | BK-CS421586 | TM03 | single: adds rule coverage TM03 (has previous reviews) |
| bank | BK-CS541107 | TM01 | single: new alert mix |
| emoney | EM-CS889012 | TM01, TM02, TM04 | hard: adds rule coverage TM01,TM02,TM04; previous reviews on file (has previous reviews) |
| emoney | EM-CS718647 | TM22 | hard: adds rule coverage TM22 |
| emoney | EM-CS530258 | TM01, TM02, TM03 | hard: adds rule coverage TM03 |
| emoney | EM-CS202096 | TM01, TM04, TM06 | hard: adds rule coverage TM06 |
| emoney | EM-CS876385 | TM05 | single: adds rule coverage TM05 |
| emoney | EM-CS784884 | TM07 | single: adds rule coverage TM07 |
| emoney | EM-CS997860 | TM02 | single: new alert mix (has previous reviews) |
| emoney | EM-CS518224 | TM03 | single: new alert mix (has previous reviews) |
| investment | IV-CS830175 | TM01, TM08, TM22 | hard: adds rule coverage TM01,TM08,TM22; previous reviews on file (has previous reviews) |
| investment | IV-CS381328 | TM01, TM06, TM08 | hard: adds rule coverage TM06 |
| investment | IV-CS852803 | TM01, TM02, TM08 | hard: adds rule coverage TM02 (has previous reviews) |
| investment | IV-CS265456 | TM01, TM08, TM10 | hard: adds rule coverage TM10 |
| investment | IV-CS243737 | TM07 | single: adds rule coverage TM07 |
| investment | IV-CS878862 | TM06 | single: new alert mix (has previous reviews) |
| investment | IV-CS715650 | TM08 | single: new alert mix |
| payments_fx | PX-CS313282 | TM02, TM04, TM08 | hard: adds rule coverage TM02,TM04,TM08; previous reviews on file; held payment (has previous reviews, held payment PX-T754929855) |
| payments_fx | PX-CS582076 | TM22 | hard: adds rule coverage TM22 (held payment PX-T626939492) |
| payments_fx | PX-CS357008 | TM02, TM06, TM08 | hard: adds rule coverage TM06 (has previous reviews) |
| payments_fx | PX-CS722632 | TM22 | hard: next in seeded order (has previous reviews, held payment PX-T880658730) |
| payments_fx | PX-CS664550 | TM01 | single: adds rule coverage TM01 |
| payments_fx | PX-CS327236 | TM03 | single: adds rule coverage TM03 (has previous reviews, held payment PX-T406272424) |
| payments_fx | PX-CS322412 | TM07 | single: adds rule coverage TM07 |
