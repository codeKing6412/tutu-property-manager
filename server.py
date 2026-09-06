"""图图物业管家 — 本地物业管理。仅依赖 Python 标准库。"""
import argparse
import base64
import csv
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import io
import json
import os
import re
from pathlib import Path
import sqlite3
import socket
import threading
import uuid
import webbrowser
import zipfile
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / 'data' / 'property.db'
LOCK = threading.RLock()
TODAY = lambda: dt.date.today()
TABLES = {
 'houses': ['community','building','unit','number','area','rate','start','contact_id','note','active'],
 'owners': ['name','phone','identity','house_ids','role','note'],
 'vehicles': ['plate','house_id','owner_id','space_id','note'],
 'spaces': ['number','kind','house_id','owner_id','management_rate','public_rate','start','note','active'],
 'space_statuses': ['name','management_rate','public_rate','charge_management','charge_public','require_house','active'],
 'fees': ['name','target_type','target_community','target_building','target_unit','target_id','basis','rate','cycle','interval_months','start','end','note','active'],
 'todos': ['title','due','done'],
}
LABELS = {
 'id':'编号','community':'小区','building':'楼栋','unit':'单元','number':'门牌号','area':'建筑面积',
 'rate':'单价','start':'起算日期','contact_id':'主联系人编号','note':'备注','active':'启用',
 'name':'姓名','phone':'手机号','identity':'证件号','house_ids':'关联房屋编号','role':'身份',
 'plate':'车牌号','house_id':'计费房屋编号','owner_id':'业主编号','space_id':'车位编号ID',
 'kind':'车位状态','management_rate':'管理费年度金额','public_rate':'公共收益年度金额',
 'charge_management':'收取管理费','charge_public':'收取公共收益','require_house':'必须关联房屋',
 'target_type':'适用范围','target_community':'目标小区','target_building':'目标楼栋','target_unit':'目标单元','target_id':'目标档案编号',
 'basis':'计费方式','cycle':'周期','interval_months':'自定义间隔月数','end':'结束日期',
 'title':'待办事项','due':'截止日期','done':'已完成'
}
DEFAULTS = {'name':'我的小区','property_rate':'2.50','start':TODAY().replace(day=1).isoformat(),
            'currency':'CNY','reminder_days':'14','roles':'业主,租户,家属','payment_methods':'现金,微信,支付宝,银行转账,其他'}
DEFAULT_STATUSES = [
 {'id':'status-sold','name':'已售','management_rate':'480','public_rate':'0','charge_management':True,'charge_public':False,'require_house':True,'active':True},
 {'id':'status-rented','name':'已租','management_rate':'480','public_rate':'1320','charge_management':True,'charge_public':True,'require_house':True,'active':True},
 {'id':'status-temporary','name':'临停','management_rate':'0','public_rate':'0','charge_management':False,'charge_public':False,'require_house':False,'active':True},
 {'id':'status-empty','name':'空闲','management_rate':'0','public_rate':'0','charge_management':False,'charge_public':False,'require_house':False,'active':True},
]

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try: return super().__exit__(*args)
        finally: self.close()

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, factory=ClosingConnection)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

def init_db():
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL,id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS bills (id TEXT PRIMARY KEY,house_id TEXT NOT NULL,source TEXT NOT NULL,
          period TEXT NOT NULL,item TEXT NOT NULL,amount INTEGER NOT NULL CHECK(amount>=0),due TEXT NOT NULL,
          created TEXT NOT NULL,coverage_end TEXT NOT NULL DEFAULT '',group_key TEXT NOT NULL DEFAULT '',
          entity_type TEXT NOT NULL DEFAULT 'house',entity_id TEXT NOT NULL DEFAULT '',UNIQUE(house_id,source,period));
        CREATE TABLE IF NOT EXISTS payments (id TEXT PRIMARY KEY,date TEXT NOT NULL,amount INTEGER NOT NULL,
          method TEXT NOT NULL,note TEXT NOT NULL,void INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS allocations (payment_id TEXT NOT NULL REFERENCES payments(id),
          bill_id TEXT NOT NULL REFERENCES bills(id),amount INTEGER NOT NULL CHECK(amount>0),PRIMARY KEY(payment_id,bill_id));
        CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT,time TEXT NOT NULL,action TEXT NOT NULL,data TEXT NOT NULL);
        ''')
        columns={r[1] for r in c.execute('PRAGMA table_info(bills)')}
        for name,definition in [('coverage_end',"TEXT NOT NULL DEFAULT ''"),('group_key',"TEXT NOT NULL DEFAULT ''"),('entity_type',"TEXT NOT NULL DEFAULT 'house'"),('entity_id',"TEXT NOT NULL DEFAULT ''")]:
            if name not in columns: c.execute(f'ALTER TABLE bills ADD COLUMN {name} {definition}')
        c.execute('INSERT OR IGNORE INTO settings VALUES(1,?)',(json.dumps(DEFAULTS),))
        current=json.loads(c.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])
        merged={**DEFAULTS,**current}
        c.execute('UPDATE settings SET data=? WHERE id=1',(json.dumps(merged,ensure_ascii=False),))
        if not c.execute("SELECT 1 FROM records WHERE kind='space_statuses'").fetchone():
            for status in DEFAULT_STATUSES:
                item=dict(status); key=item.pop('id')
                c.execute('INSERT INTO records VALUES(?,?,?)',('space_statuses',key,json.dumps(item,ensure_ascii=False)))
        # Upgrade records created by the first local version without rewriting bills.
        for row in list(c.execute("SELECT id,data FROM records WHERE kind='spaces'")):
            item=json.loads(row['data'])
            if 'management_rate' not in item:
                old=item.pop('annual_rate','')
                if old not in ['',None]:
                    total=number(old); management=min(total,Decimal('480'))
                    item['management_rate']=str(management); item['public_rate']=str(total-management)
                else:
                    item['management_rate']=''; item['public_rate']=''
                c.execute("UPDATE records SET data=? WHERE kind='spaces' AND id=?",(json.dumps(item,ensure_ascii=False),row['id']))
        # A previously custom-priced status must keep producing annual bills after migration.
        for status_row in list(c.execute("SELECT id,data FROM records WHERE kind='space_statuses'")):
            status=json.loads(status_row['data']); related=[json.loads(x['data']) for x in c.execute("SELECT data FROM records WHERE kind='spaces'") if json.loads(x['data']).get('kind')==status['name']]
            if any(number(x.get('management_rate') or 0)>0 or number(x.get('public_rate') or 0)>0 for x in related):
                status['charge_management']=any(number(x.get('management_rate') or 0)>0 for x in related)
                status['charge_public']=any(number(x.get('public_rate') or 0)>0 for x in related)
                status['require_house']=True
                c.execute("UPDATE records SET data=? WHERE kind='space_statuses' AND id=?",(json.dumps(status,ensure_ascii=False),status_row['id']))
        # Split legacy aggregate parking bills in place. Payment totals and allocations are preserved.
        spaces_by_id={x['id']:x for x in records(c,'spaces')}
        for bill in list(c.execute("SELECT * FROM bills WHERE source LIKE 'spaces:%'")):
            parts=bill['source'].split(':')
            if len(parts)!=2: continue
            space_id=parts[1]; sp=spaces_by_id.get(space_id)
            if not sp: continue
            management=min(money(sp.get('management_rate') or 0),bill['amount']); public=bill['amount']-management
            group=f"parking:{space_id}:{bill['period']}"; coverage=(add_month(month(bill['due']),12)-dt.timedelta(days=1)).isoformat()
            c.execute("""UPDATE bills SET source=?,item=?,amount=?,coverage_end=?,group_key=?,entity_type='space',entity_id=? WHERE id=?""",
              (f'spaces:{space_id}:management','车位管理费 · '+sp['number'],management,coverage,group,space_id,bill['id']))
            public_id=uid()
            c.execute('''INSERT INTO bills(id,house_id,source,period,item,amount,due,created,coverage_end,group_key,entity_type,entity_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(public_id,bill['house_id'],f'spaces:{space_id}:public',bill['period'],'停车公共收益 · '+sp['number'],public,bill['due'],bill['created'],coverage,group,'space',space_id))
            remaining_management=management
            for allocation in list(c.execute('SELECT * FROM allocations WHERE bill_id=? ORDER BY rowid',(bill['id'],))):
                to_management=min(allocation['amount'],remaining_management); to_public=allocation['amount']-to_management
                if to_management: c.execute('UPDATE allocations SET amount=? WHERE payment_id=? AND bill_id=?',(to_management,allocation['payment_id'],bill['id']))
                else: c.execute('DELETE FROM allocations WHERE payment_id=? AND bill_id=?',(allocation['payment_id'],bill['id']))
                if to_public: c.execute('INSERT INTO allocations VALUES(?,?,?)',(allocation['payment_id'],public_id,to_public))
                remaining_management-=to_management
        for row in list(c.execute("SELECT id,data FROM records WHERE kind='fees'")):
            item=json.loads(row['data'])
            changed=False
            if 'target_type' not in item:
                old=item.pop('house_id','')
                item.update(target_type='house' if old else 'all_houses',target_community='',target_building='',target_unit='',target_id=old)
                changed=True
            if 'interval_months' not in item:
                item['interval_months']=''; changed=True
            if changed:
                c.execute("UPDATE records SET data=? WHERE kind='fees' AND id=?",(json.dumps(item,ensure_ascii=False),row['id']))

def uid(): return uuid.uuid4().hex[:12]
def now(): return dt.datetime.now().isoformat(timespec='seconds')
def money(value):
    try:
        v = Decimal(str(value))
        if not v.is_finite() or v < 0 or v > Decimal('1000000000'): raise ValueError()
        return int((v * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except Exception: raise ValueError('金额必须是非负有效数字，且不超过十亿元')
def number(value):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n < 0 or n > 1000000000: raise ValueError()
        return n
    except Exception: raise ValueError('面积或单价必须是非负有效数字')
def date(value):
    try: return dt.date.fromisoformat(str(value))
    except Exception: raise ValueError('日期格式应为 YYYY-MM-DD')
def month(value): return date(value).replace(day=1)
def add_month(value, n=1):
    i = value.year*12+value.month-1+n
    return dt.date(i//12, i%12+1, 1)
def audit(c, action, data):
    c.execute('INSERT INTO audit(time,action,data) VALUES(?,?,?)',(now(),action,json.dumps(data,ensure_ascii=False)))
def settings(c): return {**DEFAULTS,**json.loads(c.execute('SELECT data FROM settings WHERE id=1').fetchone()[0])}
def records(c, kind):
    return [dict(json.loads(r['data']),id=r['id']) for r in c.execute('SELECT * FROM records WHERE kind=? ORDER BY rowid',(kind,))]
def get(c, kind, key):
    r=c.execute('SELECT data FROM records WHERE kind=? AND id=?',(kind,key)).fetchone()
    if not r: raise ValueError('关联记录不存在：'+str(key))
    return dict(json.loads(r[0]),id=key)
def house_label(h): return f"{h['community']} / {h['building']} / {h['unit']} / {h['number']}"
def validate(c, kind, row):
    if kind not in TABLES: raise ValueError('未知档案类型')
    d={k:row.get(k,'') for k in TABLES[kind]}
    if kind=='spaces' and row.get('annual_rate') not in [None,'']:
        if row.get('kind')=='已售' and d['management_rate']=='': d['management_rate']=row['annual_rate']
        if row.get('kind')=='已租' and d['public_rate']=='': d['public_rate']=row['annual_rate']
    if kind=='fees' and not d['target_type']:
        d['target_type']='house' if row.get('house_id') else 'all_houses'
        d['target_id']=row.get('house_id','')
    for k in d:
        if isinstance(d[k], str): d[k]=d[k].strip()
    for key in ['active','done','charge_management','charge_public','require_house']:
        if key in d: d[key]=str(d[key]).lower() not in ['false','0','否'] if d[key]!='' else key=='active'
    for key in ['start','end','due']:
        if d.get(key): date(d[key])
    if d.get('start') and date(d['start']).year<2000: raise ValueError('起算年份不能早于2000年')
    if d.get('end') and d.get('start') and d['end'] < d['start']: raise ValueError('结束日期不能早于起算日期')
    for key in ['rate','management_rate','public_rate','area']:
        if d.get(key)!='' and key in d: number(d[key])
    def required(*fields):
        for key in fields:
            if not d.get(key): raise ValueError(LABELS.get(key,key)+'不能为空')
    if kind=='houses':
        required('community','building','unit','number','area')
        if number(d['area'])<=0: raise ValueError('建筑面积必须大于0')
        if d['contact_id']:
            owner=get(c,'owners',d['contact_id'])
            if row.get('id') not in owner['house_ids']: raise ValueError('主联系人必须关联本房屋')
    if kind=='owners':
        required('name','house_ids')
        if isinstance(d['house_ids'],str): d['house_ids']=[x.strip() for x in d['house_ids'].replace('，',',').split(',') if x.strip()]
        d['house_ids']=list(dict.fromkeys(d['house_ids']))
        for h in d['house_ids']: get(c,'houses',h)
        if row.get('id'):
            for v in records(c,'vehicles')+records(c,'spaces'):
                if v.get('owner_id')==row['id'] and v['house_id'] not in d['house_ids']: raise ValueError('该业主仍有关联车辆或车位，请先修改关联')
            for h in records(c,'houses'):
                if h.get('contact_id')==row['id'] and h['id'] not in d['house_ids']: raise ValueError('请先变更房屋主联系人')
        d['role']=d['role'] or '业主'
    if kind in ['vehicles','spaces']:
        status=next((x for x in records(c,'space_statuses') if x['name']==d.get('kind') and x['active']),None) if kind=='spaces' else None
        if kind=='vehicles' or (status and status['require_house']): required('house_id')
        if d['house_id']: get(c,'houses',d['house_id'])
        if d['owner_id']:
            owner=get(c,'owners',d['owner_id'])
            if d['house_id'] not in owner['house_ids']: raise ValueError('所选业主不属于该房屋')
    if kind=='vehicles':
        required('plate')
        d['plate']=d['plate'].upper().replace(' ','')
        if d['space_id']:
            space=get(c,'spaces',d['space_id'])
            if space['house_id']!=d['house_id']: raise ValueError('车位与车辆必须归属同一房屋')
    if kind=='spaces':
        required('number','kind')
        if not status: raise ValueError('车位状态不存在或已停用，请先在费项配置中维护状态')
        for v in records(c,'vehicles'):
            if v.get('space_id')==row.get('id') and v['house_id']!=d['house_id']: raise ValueError('请先变更该车位关联车辆的房屋')
    if kind=='space_statuses':
        required('name')
        if not (d['charge_management'] or d['charge_public']):
            d['management_rate']=d['management_rate'] or '0'; d['public_rate']=d['public_rate'] or '0'
        if d['charge_management']: required('management_rate')
        if d['charge_public']: required('public_rate')
    if kind=='fees':
        required('name','basis','cycle','rate')
        if d['basis'] not in ['固定金额','按面积','按次']: raise ValueError('计费方式无效')
        if d['cycle'] not in ['每月','每季度','每半年','每年','自定义周期','一次性','手动']: raise ValueError('周期无效')
        if d['basis']=='按次' and d['cycle']!='手动': raise ValueError('按次收费请使用手动周期')
        if d['cycle']=='自定义周期':
            try: interval=int(d['interval_months'])
            except Exception: raise ValueError('自定义周期必须填写整数月数')
            if interval<1 or interval>120: raise ValueError('自定义周期应为1至120个月')
        allowed=['all_houses','building','unit','house','all_vehicles','vehicle','space_status','space']
        if d['target_type'] not in allowed: raise ValueError('适用范围无效')
        if d['target_type']=='building': required('target_community','target_building')
        if d['target_type']=='unit': required('target_community','target_building','target_unit')
        if d['target_type']=='house': required('target_id'); get(c,'houses',d['target_id'])
        if d['target_type']=='vehicle': required('target_id'); get(c,'vehicles',d['target_id'])
        if d['target_type']=='space': required('target_id'); get(c,'spaces',d['target_id'])
        if d['target_type']=='space_status':
            required('target_id')
            if not any(x['name']==d['target_id'] for x in records(c,'space_statuses')): raise ValueError('目标车位状态不存在')
        if d['basis']=='按面积' and d['target_type'] not in ['all_houses','building','unit','house']:
            raise ValueError('车辆和车位费项不能按房屋面积计费')
    if kind=='todos': required('title')
    for other in records(c,kind):
        if other['id']==row.get('id'): continue
        if kind=='houses' and all(other[k]==d[k] for k in ['community','building','unit','number']): raise ValueError('房屋门牌重复')
        if kind=='vehicles' and other['plate']==d['plate']: raise ValueError('车牌重复')
        if kind=='spaces' and other['number']==d['number']: raise ValueError('车位编号重复')
        if kind=='space_statuses' and other['name']==d['name']: raise ValueError('车位状态名称重复')
    return d

def save(c, kind, row):
    key=row.get('id') or uid()
    d=validate(c,kind,row)
    old=c.execute('SELECT data FROM records WHERE kind=? AND id=?',(kind,key)).fetchone()
    c.execute('INSERT INTO records VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data',(kind,key,json.dumps(d,ensure_ascii=False)))
    if kind=='space_statuses' and old:
        before=json.loads(old[0])
        if before.get('name')!=d['name']:
            for sp in records(c,'spaces'):
                if sp.get('kind')==before.get('name'):
                    sp['kind']=d['name']; c.execute("UPDATE records SET data=? WHERE kind='spaces' AND id=?",(json.dumps({k:v for k,v in sp.items() if k!='id'},ensure_ascii=False),sp['id']))
            for fee in records(c,'fees'):
                if fee.get('target_type')=='space_status' and fee.get('target_id')==before.get('name'):
                    fee['target_id']=d['name']; c.execute("UPDATE records SET data=? WHERE kind='fees' AND id=?",(json.dumps({k:v for k,v in fee.items() if k!='id'},ensure_ascii=False),fee['id']))
    audit(c,'修改档案' if old else '新增档案',{'kind':kind,'id':key,'before':json.loads(old[0]) if old else None,'after':d})
    return key

def delete(c, kind, key):
    get(c,kind,key)
    refs=[]
    if kind=='houses':
        refs=[o for o in records(c,'owners') if key in o['house_ids']]
        refs += [x for k in ['vehicles','spaces'] for x in records(c,k) if x.get('house_id')==key]
        refs += [x for x in records(c,'fees') if x.get('target_type')=='house' and x.get('target_id')==key]
        refs += c.execute('SELECT id FROM bills WHERE house_id=?',(key,)).fetchall()
    if kind=='owners':
        refs=[x for k in ['vehicles','spaces'] for x in records(c,k) if x.get('owner_id')==key]
        refs += [h for h in records(c,'houses') if h.get('contact_id')==key]
    if kind=='spaces': refs=[v for v in records(c,'vehicles') if v.get('space_id')==key]
    if kind=='vehicles': refs=[f for f in records(c,'fees') if f.get('target_type')=='vehicle' and f.get('target_id')==key]
    if kind=='spaces': refs += [f for f in records(c,'fees') if f.get('target_type')=='space' and f.get('target_id')==key]
    if kind=='space_statuses':
        status_name=get(c,kind,key)['name']
        refs=[s for s in records(c,'spaces') if s.get('kind')==status_name]
        refs += [f for f in records(c,'fees') if f.get('target_type')=='space_status' and f.get('target_id')==status_name]
    if kind=='spaces': refs += c.execute("SELECT id FROM bills WHERE entity_type='space' AND entity_id=?",(key,)).fetchall()
    if kind=='fees': refs += c.execute("SELECT id FROM bills WHERE source LIKE ?",('fees:'+key+':%',)).fetchall()
    if refs: raise ValueError('已有档案或账单关联，不能删除。请先解除关联，或停用以保留历史账目。')
    audit(c,'删除档案',{'kind':kind,'record':get(c,kind,key)})
    c.execute('DELETE FROM records WHERE kind=? AND id=?',(kind,key))

def fee_entities(c, fee, houses=None, vehicles=None, spaces=None):
    houses=houses if houses is not None else records(c,'houses')
    vehicles=vehicles if vehicles is not None else records(c,'vehicles')
    spaces=spaces if spaces is not None else records(c,'spaces')
    target=fee['target_type']; result=[]
    if target in ['all_houses','building','unit','house']:
        for h in houses:
            if target=='building' and not (h['community']==fee['target_community'] and h['building']==fee['target_building']): continue
            if target=='unit' and not (h['community']==fee['target_community'] and h['building']==fee['target_building'] and h['unit']==fee['target_unit']): continue
            if target=='house' and h['id']!=fee['target_id']: continue
            result.append(('house',h,h))
    elif target in ['all_vehicles','vehicle']:
        for v in vehicles:
            if target=='vehicle' and v['id']!=fee['target_id']: continue
            h=next((x for x in houses if x['id']==v['house_id']),None)
            if h: result.append(('vehicle',v,h))
    elif target in ['space_status','space']:
        for sp in spaces:
            if target=='space_status' and sp['kind']!=fee['target_id']: continue
            if target=='space' and sp['id']!=fee['target_id']: continue
            h=next((x for x in houses if x['id']==sp.get('house_id')),None)
            if h: result.append(('space',sp,h))
    return result

def generate(c, until=None, only_house=None):
    s=settings(c)
    end=month(until or TODAY().isoformat())
    if end>add_month(TODAY().replace(day=1),60): raise ValueError('最多可生成未来5年的账单')
    if end.year<2000: raise ValueError('起算年份不能早于2000年')
    count=0
    known={(r['house_id'],r['source'],r['period']) for r in c.execute('SELECT house_id,source,period FROM bills')}
    all_houses=records(c,'houses'); all_spaces=records(c,'spaces'); all_vehicles=records(c,'vehicles')
    all_fees=records(c,'fees'); statuses={x['name']:x for x in records(c,'space_statuses') if x['active']}
    def issue(h, source, item, rate, start, cycle, finish='',entity_type='house',entity_id='',group_key='',interval_months=''):
        nonlocal count
        begin=month(start or s['start'])
        if begin.year<2000: raise ValueError('起算年份不能早于2000年')
        stop=min(end,month(finish)) if finish else end
        p=begin
        step={'每月':1,'每季度':3,'每半年':6,'每年':12,'一次性':1}.get(cycle,int(interval_months or 1))
        while p<=stop:
            if cycle=='一次性' and p!=begin: break
            identity=(h['id'],source,p.strftime('%Y-%m'))
            if identity in known:
                p=add_month(p,step)
                continue
            amount=money(rate)
            coverage_end=(add_month(p,step)-dt.timedelta(days=1)).isoformat()
            group=(group_key+':'+p.strftime('%Y-%m')) if group_key else source+':'+p.strftime('%Y-%m')
            cur=c.execute('''INSERT OR IGNORE INTO bills(id,house_id,source,period,item,amount,due,created,coverage_end,group_key,entity_type,entity_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(uid(),h['id'],source,p.strftime('%Y-%m'),item,amount,p.isoformat(),now(),coverage_end,group,entity_type,entity_id or h['id']))
            count+=cur.rowcount
            known.add(identity)
            p=add_month(p,step)
    for h in all_houses:
        if not h['active'] or (only_house and h['id']!=only_house): continue
        rate=h['rate'] if h['rate']!='' else s['property_rate']
        issue(h,'property','物业费',number(h['area'])*number(rate),h['start'],'每月')
        for sp in all_spaces:
            if sp.get('house_id')!=h['id'] or not sp['active'] or sp['kind'] not in statuses: continue
            status=statuses[sp['kind']]
            start=max(month(sp['start'] or s['start']),month(h['start'] or s['start'])).isoformat()
            group='parking:'+sp['id']
            if status['charge_management']:
                rate=sp['management_rate'] if sp['management_rate']!='' else status['management_rate']
                issue(h,'spaces:'+sp['id']+':management','车位管理费 · '+sp['number'],rate,start,'每年',entity_type='space',entity_id=sp['id'],group_key=group)
            if status['charge_public']:
                rate=sp['public_rate'] if sp['public_rate']!='' else status['public_rate']
                issue(h,'spaces:'+sp['id']+':public','停车公共收益 · '+sp['number'],rate,start,'每年',entity_type='space',entity_id=sp['id'],group_key=group)
    for f in all_fees:
        if not f['active'] or f['cycle']=='手动': continue
        for entity_type,entity,h in fee_entities(c,f,all_houses,all_vehicles,all_spaces):
            if not h['active'] or (only_house and h['id']!=only_house): continue
            rate=number(f['rate'])*(number(h['area']) if f['basis']=='按面积' else 1)
            start=max(month(f['start'] or s['start']),month(h['start'] or s['start'])).isoformat()
            entity_name=h['number'] if entity_type=='house' else entity['plate'] if entity_type=='vehicle' else entity['number']
            item=f['name']+((' · '+entity_name) if entity_type!='house' else '')
            source=f"fees:{f['id']}:{entity_type}:{entity['id']}"
            issue(h,source,item,rate,start,f['cycle'],f['end'],entity_type,entity['id'],f"fee:{f['id']}:{entity_type}:{entity['id']}",f.get('interval_months',''))
    return count

def bills(c):
    return [dict(r) for r in c.execute('''SELECT b.*,COALESCE((SELECT SUM(a.amount) FROM allocations a JOIN payments p ON p.id=a.payment_id
        WHERE a.bill_id=b.id AND p.void=0),0) paid FROM bills b ORDER BY period,created''')]

def pay(c, data):
    entries=data.get('allocations',[])
    if not entries: raise ValueError('请选择账单并填写本次缴费金额')
    paid_date=date(data.get('date',TODAY().isoformat()))
    if paid_date>TODAY(): raise ValueError('收款日期不能在未来，预缴请选择未来账单')
    available={b['id']:b for b in bills(c)}
    valid=[]
    seen=set()
    for entry in entries:
        key=entry['bill_id']
        if key in seen: raise ValueError('账单重复')
        seen.add(key)
        b=available.get(key)
        if not b: raise ValueError('账单不存在')
        value=money(entry['amount'])
        if value<=0 or value>b['amount']-b['paid']: raise ValueError('缴费金额必须大于0且不超过该账单剩余应缴金额')
        valid.append((key,value))
    key=data.get('request_id') or uid()
    if c.execute('SELECT id FROM payments WHERE id=?',(key,)).fetchone(): raise ValueError('该付款已提交，请勿重复操作')
    c.execute('INSERT INTO payments(id,date,amount,method,note,void,created) VALUES(?,?,?,?,?,0,?)',
        (key,paid_date.isoformat(),sum(v for _,v in valid),data.get('method','现金'),data.get('note',''),now()))
    for bill_id,value in valid: c.execute('INSERT INTO allocations VALUES(?,?,?)',(key,bill_id,value))
    audit(c,'收款',{'id':key,'allocations':valid})
    return key

def manual_bill(c, data):
    when=date(data['due'])
    if data.get('fee_id'):
        f=get(c,'fees',data['fee_id'])
        if not f['active'] or f['cycle']!='手动': raise ValueError('该费项不是启用中的手动费项')
        candidates=fee_entities(c,f)
        entity_type=data.get('entity_type','house'); entity_id=data.get('entity_id') or data.get('house_id')
        match=next((x for x in candidates if x[0]==entity_type and x[1]['id']==entity_id),None)
        if not match: raise ValueError('该手动费项不适用于所选档案')
        entity_type,entity,h=match
        units=number(data.get('units','1'))
        if units<=0: raise ValueError('数量必须大于0')
        amount=money(number(f['rate'])*units*(number(h['area']) if f['basis']=='按面积' else 1))
        title=f['name']+' × '+str(units)
        source='manual:'+f['id']+':'+entity_type+':'+entity['id']+':'+uid()
    else:
        h=get(c,'houses',data['house_id'])
        entity_type='house'; entity=h
        amount=money(data['amount']); title=str(data.get('item','')).strip()
        source='manual:'+uid()
    if not title or amount<=0: raise ValueError('请填写费项名称及大于0的金额')
    key=uid()
    c.execute('''INSERT INTO bills(id,house_id,source,period,item,amount,due,created,coverage_end,group_key,entity_type,entity_id)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(key,h['id'],source,when.strftime('%Y-%m'),title,amount,when.isoformat(),now(),when.isoformat(),source,entity_type,entity['id']))
    audit(c,'补录账单',dict(data,id=key))
    return key

def state(c):
    generate(c,add_month(TODAY().replace(day=1),1).isoformat())
    data={k:records(c,k) for k in TABLES}
    config=settings(c)
    for h in data['houses']:
        h['monthly_amount']=money(number(h['area'])*number(h['rate'] if h['rate']!='' else config['property_rate']))
    current_bills=bills(c)
    reminder_end=TODAY()+dt.timedelta(days=int(config['reminder_days']))
    reminders=[]
    grouped={}
    for b in current_bills:
        if not b['source'].startswith('spaces:') or b['amount']<=b['paid']: continue
        due=date(b['due'])
        if TODAY()<=due<=reminder_end:
            item=grouped.setdefault(b['group_key'],{'house_id':b['house_id'],'space_id':b['entity_id'],'due':b['due'],'amount':0,'items':[]})
            item['amount']+=b['amount']-b['paid']; item['items'].append(b['item'])
    reminders=list(grouped.values())
    data.update(settings=config,bills=current_bills,payments=[dict(r) for r in c.execute('SELECT * FROM payments ORDER BY created DESC, rowid DESC')],
      allocations=[dict(r) for r in c.execute('SELECT * FROM allocations')],
      reminders=reminders,audit=[dict(r) for r in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 300')],today=TODAY().isoformat(),time=now())
    return data

# Minimal, dependency-free XLSX reader/writer; all cells are strings (preserves phones and IDs).
NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
def xlsx(rows):
    root=ET.Element('worksheet',xmlns=NS); sheet=ET.SubElement(root,'sheetData')
    for i,row in enumerate(rows,1):
        r=ET.SubElement(sheet,'row',r=str(i))
        for j,value in enumerate(row):
            n=j+1; col=''
            while n: n,a=divmod(n-1,26); col=chr(65+a)+col
            cell=ET.SubElement(r,'c',r=col+str(i),t='inlineStr')
            ET.SubElement(ET.SubElement(cell,'is'),'t').text=str(value if value is not None else '')
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<workbook xmlns="'+NS+'" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="档案" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml',ET.tostring(root,encoding='utf-8',xml_declaration=True))
    return output.getvalue()

def read_sheet(raw, filename):
    if filename.lower().endswith('.csv'):
        try: text=raw.decode('utf-8-sig')
        except UnicodeDecodeError: text=raw.decode('gb18030')
        return list(csv.reader(io.StringIO(text)))
    if not filename.lower().endswith('.xlsx'): raise ValueError('请使用 .xlsx 或 .csv 文件')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if sum(i.file_size for i in z.infolist())>30_000_000: raise ValueError('表格解压后过大，请分批导入')
        strings=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            strings=[''.join(si.itertext()) for si in ET.fromstring(z.read('xl/sharedStrings.xml'))]
        workbook=ET.fromstring(z.read('xl/workbook.xml'))
        date_styles=set()
        if 'xl/styles.xml' in z.namelist():
            styles=ET.fromstring(z.read('xl/styles.xml'))
            custom={int(x.attrib['numFmtId']):x.attrib.get('formatCode','') for x in styles.iter('{'+NS+'}numFmt')}
            xfs=styles.find('{'+NS+'}cellXfs')
            for index,xf in enumerate(xfs if xfs is not None else []):
                fmt=int(xf.attrib.get('numFmtId','0'))
                code=re.sub(r'"[^"]*"|\\.', '', custom.get(fmt,'')).lower()
                if fmt in set(range(14,23))|{45,46,47} or ('y' in code and ('m' in code or 'd' in code)):
                    date_styles.add(index)
        props=workbook.find('{'+NS+'}workbookPr')
        epoch=dt.datetime(1904,1,1) if props is not None and props.attrib.get('date1904') in ['1','true'] else dt.datetime(1899,12,30)
        first=workbook.find('{'+NS+'}sheets')[0]
        relid=first.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
        rels=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        target=next(r.attrib['Target'] for r in rels if r.attrib['Id']==relid)
        target=target.lstrip('/') if target.startswith('/') else 'xl/'+target
        rows=[]
        for row in ET.fromstring(z.read(target)).iter('{'+NS+'}row'):
            result=[]
            for cell in row:
                ref=cell.attrib.get('r','A1'); idx=0
                for char in ref:
                    if char.isalpha(): idx=idx*26+ord(char.upper())-64
                while len(result)<idx: result.append('')
                v=cell.find('{'+NS+'}v'); t=cell.attrib.get('t')
                value=''.join(cell.find('{'+NS+'}is').itertext()) if t=='inlineStr' else (v.text if v is not None else '')
                if t=='s': value=strings[int(value)]
                if value and t in [None,'n'] and int(cell.attrib.get('s','-1')) in date_styles:
                    value=(epoch+dt.timedelta(days=float(value))).date().isoformat()
                result[idx-1]=value
            rows.append(result)
        return rows

def export_rows(c, kind, template=False):
    keys=['id']+TABLES[kind]
    headers=[LABELS.get(k,k) if not (kind=='spaces' and k=='number') and not (kind=='fees' and k=='name') else ('车位编号' if k=='number' else '费项名称') for k in keys]
    rows=[headers]
    if not template:
        for r in records(c,kind):
            rows.append([','.join(r[k]) if isinstance(r.get(k),list) else ('1' if r[k] else '0') if isinstance(r.get(k),bool) else r.get(k,'') for k in keys])
    return rows

def import_rows(c, kind, data):
    rows=read_sheet(base64.b64decode(data['content']),data['filename'])
    if not rows: raise ValueError('表格为空')
    expected=export_rows(c,kind,True)[0]; keys=['id']+TABLES[kind]
    mapping={label:key for label,key in zip(expected,keys)}
    cols=[mapping.get(x.strip(),x.strip() if x.strip() in keys else None) for x in rows[0]]
    if not any(cols): raise ValueError('未识别表头，请下载模板')
    c.execute('SAVEPOINT import_batch')
    errors=[]; count=0
    for line,values in enumerate(rows[1:],2):
        if not any(str(x).strip() for x in values): continue
        row={k:values[i] for i,k in enumerate(cols) if k and i<len(values)}
        try:
            if row.get('id'):
                existing=c.execute('SELECT data FROM records WHERE kind=? AND id=?',(kind,row['id'])).fetchone()
                if existing: row=dict(json.loads(existing[0]),**row)
            save(c,kind,row); count+=1
        except (ValueError,KeyError) as e: errors.append({'row':line,'message':str(e)})
    if errors or data.get('preview',True): c.execute('ROLLBACK TO import_batch')
    c.execute('RELEASE import_batch')
    return {'count':count,'errors':errors,'committed':not errors and not data.get('preview',True)}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def reply(self,data,status=200,ctype='application/json; charset=utf-8',filename=None):
        raw=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type',ctype)
        self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-store')
        if filename: self.send_header('Content-Disposition','attachment; filename="'+filename+'"')
        self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        try:
            u=urlparse(self.path); q=parse_qs(u.query)
            if u.path=='/api/state':
                with LOCK,connect() as c: result=state(c)
                return self.reply(result)
            if u.path=='/api/export':
                kind=q.get('kind',['houses'])[0]
                with connect() as c: result=xlsx(export_rows(c,kind,q.get('template',['0'])[0]=='1'))
                return self.reply(result,ctype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',filename=kind+'.xlsx')
            if u.path=='/api/backup':
                with LOCK,connect() as c:
                    result={k:records(c,k) for k in TABLES}
                    result.update(format=1,settings=settings(c),bills=[dict(r) for r in c.execute('SELECT * FROM bills')],payments=[dict(r) for r in c.execute('SELECT * FROM payments')],allocations=[dict(r) for r in c.execute('SELECT * FROM allocations')],audit=[dict(r) for r in c.execute('SELECT * FROM audit')])
                return self.reply(json.dumps(result,ensure_ascii=False,indent=2).encode(),ctype='application/json',filename='tutu-property-backup-'+TODAY().isoformat()+'.json')
            path={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}.get(u.path)
            if not path: return self.reply({'error':'未找到'},404)
            self.reply((ROOT/'static'/path).read_bytes(),ctype={'html':'text/html','js':'text/javascript','css':'text/css'}[path.split('.')[-1]]+'; charset=utf-8')
        except Exception as e: self.reply({'error':str(e)},400)
    def do_POST(self):
        try:
            size=int(self.headers.get('Content-Length',0))
            if size>12_000_000: raise ValueError('文件太大，请分批导入（最大约8MB）')
            data=json.loads(self.rfile.read(size))
            with LOCK,connect() as c:
                path=urlparse(self.path).path
                if path=='/api/save': result={'id':save(c,data['kind'],data['record'])}
                elif path=='/api/delete': delete(c,data['kind'],data['id']); result={'ok':True}
                elif path=='/api/settings':
                    s=settings(c)
                    for key in DEFAULTS:
                        if key in data: s[key]=str(data[key]).strip()
                    number(s['property_rate'])
                    try: reminder_days=int(s['reminder_days'])
                    except Exception: raise ValueError('提醒天数必须是整数')
                    if reminder_days<1 or reminder_days>31: raise ValueError('提醒天数应在1至31天之间')
                    s['reminder_days']=str(reminder_days)
                    for key,label in [('roles','住户身份'),('payment_methods','收款方式')]:
                        values=[x.strip() for x in s[key].replace('，',',').split(',') if x.strip()]
                        if not values: raise ValueError(label+'至少保留一项')
                        s[key]=','.join(dict.fromkeys(values))
                    if date(s['start']).year<2000: raise ValueError('起算年份不能早于2000年')
                    if not s['name']: raise ValueError('小区名称不能为空')
                    audit(c,'修改设置',{'before':settings(c),'after':s})
                    c.execute('UPDATE settings SET data=? WHERE id=1',(json.dumps(s,ensure_ascii=False),)); result={'ok':True}
                elif path=='/api/generate': result={'count':generate(c,data['until'],data.get('house_id'))}
                elif path=='/api/pay': result={'id':pay(c,data)}
                elif path=='/api/manual': result={'id':manual_bill(c,data)}
                elif path=='/api/bill-adjust':
                    if not str(data.get('reason','')).strip(): raise ValueError('请填写账单调整原因')
                    current=next((x for x in bills(c) if x['id']==data['id']),None)
                    if not current: raise ValueError('账单不存在')
                    adjusted=money(data['amount'])
                    if adjusted<current['paid']: raise ValueError('调整后应缴不能低于已经收取的金额；请先撤销相关收款')
                    c.execute('UPDATE bills SET amount=? WHERE id=?',(adjusted,current['id']))
                    audit(c,'调整账单金额',{'id':current['id'],'item':current['item'],'period':current['period'],'before':current['amount'],'after':adjusted,'reason':str(data['reason']).strip()})
                    result={'ok':True}
                elif path=='/api/void':
                    if not str(data.get('reason','')).strip(): raise ValueError('请填写撤销或退款原因')
                    p=c.execute('SELECT * FROM payments WHERE id=?',(data['id'],)).fetchone()
                    if not p or p['void']: raise ValueError('收款不存在或已撤销')
                    c.execute('UPDATE payments SET void=1 WHERE id=?',(data['id'],)); audit(c,'整笔撤销/退款',data); result={'ok':True}
                elif path=='/api/import': result=import_rows(c,data['kind'],data)
                elif path=='/api/restore':
                    backup=json.loads(base64.b64decode(data['content']).decode('utf-8-sig'))
                    if backup.get('format')!=1: raise ValueError('不是本系统的备份文件')
                    # Preserve the current database before replacing any business records.
                    dest=DB_PATH.parent/'backups'; dest.mkdir(parents=True,exist_ok=True)
                    dest_file=dest/('before-restore-'+dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.db')
                    with sqlite3.connect(dest_file, factory=ClosingConnection) as target: c.backup(target)
                    for table in ['allocations','payments','bills','records','audit']: c.execute('DELETE FROM '+table)
                    c.execute('UPDATE settings SET data=? WHERE id=1',(json.dumps({**DEFAULTS,**backup['settings']}),))
                    for kind in TABLES:
                        for r in backup.get(kind,[]):
                            r=dict(r); key=r.pop('id')
                            if kind=='spaces' and 'management_rate' not in r:
                                old=r.pop('annual_rate','');r['management_rate']=old if r.get('kind')=='已售' else '';r['public_rate']=old if r.get('kind')=='已租' else ''
                            if kind=='fees' and 'target_type' not in r:
                                old=r.pop('house_id','');r.update(target_type='house' if old else 'all_houses',target_community='',target_building='',target_unit='',target_id=old,interval_months='')
                            c.execute('INSERT INTO records VALUES(?,?,?)',(kind,key,json.dumps(r)))
                    for table in ['bills','payments','allocations','audit']:
                        columns=[r[1] for r in c.execute('PRAGMA table_info('+table+')')]
                        defaults={'coverage_end':'','group_key':'','entity_type':'house','entity_id':''}
                        for r in backup.get(table,[]): c.execute('INSERT INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[r.get(k,defaults.get(k)) for k in columns])
                    if not c.execute("SELECT 1 FROM records WHERE kind='space_statuses'").fetchone():
                        for status in DEFAULT_STATUSES:
                            item=dict(status); key=item.pop('id'); c.execute('INSERT INTO records VALUES(?,?,?)',('space_statuses',key,json.dumps(item,ensure_ascii=False)))
                    audit(c,'恢复备份',{'snapshot':str(dest_file)}); result={'ok':True}
                else: raise ValueError('未知操作')
            self.reply(result)
        except Exception as e: self.reply({'error':str(e)},400)

class LocalServer(ThreadingHTTPServer):
    allow_reuse_address=False
    def server_bind(self):
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()

def main():
    global DB_PATH
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8877); parser.add_argument('--no-browser',action='store_true'); parser.add_argument('--db',type=Path); args=parser.parse_args()
    if args.db: DB_PATH=args.db.resolve()
    init_db()
    server=LocalServer(('127.0.0.1',args.port),Handler)
    print(f'TuTu Property Manager: http://127.0.0.1:{args.port}  Database: {DB_PATH}',flush=True)
    if not args.no_browser: webbrowser.open(f'http://127.0.0.1:{args.port}')
    server.serve_forever()

if __name__=='__main__': main()
