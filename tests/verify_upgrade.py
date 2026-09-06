"""Verify that the live v1-to-v2 migration preserved imported records and money."""
import json
from decimal import Decimal
from pathlib import Path
import urllib.request

root=Path(__file__).resolve().parents[1]
backups=sorted((root/'data'/'backups').glob('before-v2-upgrade-*.json'))
assert backups, 'missing pre-upgrade JSON backup'
before=json.loads(backups[-1].read_text(encoding='utf-8'))
with urllib.request.urlopen('http://127.0.0.1:8877/api/state',timeout=30) as response:
    after=json.load(response)

for kind in ['houses','owners','vehicles','spaces','fees','todos']:
    assert len(before[kind])==len(after[kind]),(kind,len(before[kind]),len(after[kind]))

before_spaces={x['id']:x for x in before['spaces']}
after_spaces={x['id']:x for x in after['spaces']}
for key,old in before_spaces.items():
    if old.get('annual_rate') not in ['',None]:
        new=after_spaces[key]
        assert Decimal(str(old['annual_rate']))==Decimal(str(new['management_rate'] or 0))+Decimal(str(new['public_rate'] or 0)),key

after_bills=after['bills']
legacy=[]
for old in before['bills']:
    parts=old['source'].split(':')
    if len(parts)==2 and parts[0]=='spaces':
        matches=[x for x in after_bills if x['entity_id']==parts[1] and x['period']==old['period'] and x['source'].startswith('spaces:'+parts[1]+':')]
        assert len(matches)==2,(old['id'],len(matches))
        assert sum(x['amount'] for x in matches)==old['amount'],old['id']
        legacy.append(old)

before_pay={x['id']:x for x in before['payments']}
after_pay={x['id']:x for x in after['payments']}
assert before_pay.keys()<=after_pay.keys()
for key,payment in before_pay.items():
    assert payment['amount']==after_pay[key]['amount']
    old_alloc=sum(x['amount'] for x in before['allocations'] if x['payment_id']==key)
    new_alloc=sum(x['amount'] for x in after['allocations'] if x['payment_id']==key)
    assert old_alloc==new_alloc==(payment['amount'])

print(json.dumps({
    'records_preserved':{k:len(after[k]) for k in ['houses','owners','vehicles','spaces','fees','todos']},
    'payments_preserved':len(before_pay),
    'legacy_parking_bills_split':len(legacy),
    'current_bills':len(after_bills),
    'space_statuses':[x['name'] for x in after['space_statuses']],
},ensure_ascii=False,indent=2))
