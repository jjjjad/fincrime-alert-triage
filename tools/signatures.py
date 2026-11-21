"""Group cases by (rules fired, non-routine document types). usage: python3 tools/signatures.py <folder>"""
import json,sys,collections,os
f=sys.argv[1]
J=lambda n:[json.loads(l) for l in open(os.path.join(f,n))]
al={a['alert_id']:a for a in J('alerts.jsonl')}
docs=collections.defaultdict(list)
for e in J('evidence.jsonl'): docs[e['customer_id']].append(e['type'])
rev=collections.Counter(r['customer_id'] for r in J('previous_reviews.jsonl'))
ROUT={'due_diligence_summary','proof_of_address','contact_note','company_register_extract'}
sig=collections.Counter()
for c in J('cases.jsonl'):
  rules='+'.join(sorted(al[a]['rule_id'] for a in c['alert_ids']))
  d='+'.join(sorted(set(docs[c['customer_id']])-ROUT))
  sig[(rules,d)]+=1
for (r,d),n in sorted(sig.items(),key=lambda x:-x[1]): print(f"{n:4} {r:<22} {d}")
