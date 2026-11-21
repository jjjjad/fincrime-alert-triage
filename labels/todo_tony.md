# Cases for Tony to hand-label first

Picked from Officer Tom alone (no API key). Tom has no checks yet, so he gives every case ESCALATE with
P(RFI) 0.5 / P(ESCALATE) 0.5. Ranked by: TM22 or 3+ alerts first, then most documents and earlier reviews
on file, then case ID. At most 2 cases per alert mix per institution.

| Institution | Case | Tom's verdict | Tom's probs (C / R / E) | Why it's hard | Alerts | Rules |
|---|---|---|---|---|---|---|
| bank | BK-CS106820 | ESCALATE | 0.0 / 0.5 / 0.5 | 3 alerts; Tom unsure (top p 0.5); P(esc) 0.5 | 3 | TM01, TM02, TM04 |
| bank | BK-CS219631 | ESCALATE | 0.0 / 0.5 / 0.5 | 3 alerts; Tom unsure (top p 0.5); P(esc) 0.5 | 3 | TM01, TM02, TM04 |
| bank | BK-CS408229 | ESCALATE | 0.0 / 0.5 / 0.5 | TM22 list match; Tom unsure (top p 0.5); P(esc) 0.5 | 1 | TM22 |
| bank | BK-CS430998 | ESCALATE | 0.0 / 0.5 / 0.5 | TM22 list match; Tom unsure (top p 0.5); P(esc) 0.5 | 1 | TM22 |
| bank | BK-CS291474 | ESCALATE | 0.0 / 0.5 / 0.5 | 3 alerts; Tom unsure (top p 0.5); P(esc) 0.5 | 3 | TM01, TM02, TM05 |
| emoney | EM-CS102434 | ESCALATE | 0.0 / 0.5 / 0.5 | 3 alerts; Tom unsure (top p 0.5); P(esc) 0.5 | 3 | TM01, TM02, TM04 |
| emoney | EM-CS455664 | ESCALATE | 0.0 / 0.5 / 0.5 | 3 alerts; Tom unsure (top p 0.5); P(esc) 0.5 | 3 | TM01, TM02, TM04 |
| emoney | EM-CS153155 | ESCALATE | 0.0 / 0.5 / 0.5 | TM22 list match; Tom unsure (top p 0.5); P(esc) 0.5 | 1 | TM22 |
| emoney | EM-CS191617 | ESCALATE | 0.0 / 0.5 / 0.5 | TM22 list match; Tom unsure (top p 0.5); P(esc) 0.5 | 1 | TM22 |
| emoney | EM-CS104160 | ESCALATE | 0.0 / 0.5 / 0.5 | 3 alerts; Tom unsure (top p 0.5); P(esc) 0.5 | 3 | TM01, TM02, TM03 |
| investment | IV-CS309453 | ESCALATE | 0.0 / 0.5 / 0.5 | 3 alerts; TM22 list match; Tom unsure (top p 0.5); P(esc) 0.5 | 3 | TM01, TM08, TM22 |
| investment | IV-CS524880 | ESCALATE | 0.0 / 0.5 / 0.5 | 3 alerts; TM22 list match; Tom unsure (top p 0.5); P(esc) 0.5 | 3 | TM01, TM08, TM22 |
| investment | IV-CS390579 | ESCALATE | 0.0 / 0.5 / 0.5 | 3 alerts; Tom unsure (top p 0.5); P(esc) 0.5 | 3 | TM01, TM02, TM08 |
| investment | IV-CS465424 | ESCALATE | 0.0 / 0.5 / 0.5 | TM22 list match; Tom unsure (top p 0.5); P(esc) 0.5 | 2 | TM08, TM22 |
| investment | IV-CS660358 | ESCALATE | 0.0 / 0.5 / 0.5 | 3 alerts; Tom unsure (top p 0.5); P(esc) 0.5 | 3 | TM01, TM02, TM08 |
| payments_fx | PX-CS285847 | ESCALATE | 0.0 / 0.5 / 0.5 | TM22 list match; Tom unsure (top p 0.5); P(esc) 0.5 | 1 | TM22 |
| payments_fx | PX-CS719791 | ESCALATE | 0.0 / 0.5 / 0.5 | 3 alerts; Tom unsure (top p 0.5); P(esc) 0.5 | 3 | TM02, TM06, TM08 |
| payments_fx | PX-CS722632 | ESCALATE | 0.0 / 0.5 / 0.5 | TM22 list match; Tom unsure (top p 0.5); P(esc) 0.5 | 1 | TM22 |
| payments_fx | PX-CS357008 | ESCALATE | 0.0 / 0.5 / 0.5 | 3 alerts; Tom unsure (top p 0.5); P(esc) 0.5 | 3 | TM02, TM06, TM08 |
| payments_fx | PX-CS313282 | ESCALATE | 0.0 / 0.5 / 0.5 | 3 alerts; Tom unsure (top p 0.5); P(esc) 0.5 | 3 | TM02, TM04, TM08 |
