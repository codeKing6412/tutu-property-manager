import base64
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server as app

class AccountingTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'tests')
        self.original=app.DB_PATH;self.clock=app.TODAY
        app.DB_PATH=Path(self.temp.name)/'test.db';app.TODAY=lambda:dt.date(2026,9,6)
        app.init_db();self.c=app.connect()
        s=dict(app.DEFAULTS,start='2026-01-01',property_rate='2.5',sold_rate='360',rented_rate='2400')
        self.c.execute('UPDATE settings SET data=?',(json.dumps(s),))
        self.h=app.save(self.c,'houses',dict(id='H001',community='测试小区',building='1栋',unit='1单元',number='101',area='100'))
    def tearDown(self):
        self.c.close();app.DB_PATH=self.original;app.TODAY=self.clock;self.temp.cleanup()
    def test_monthly_generation_and_catchup_are_idempotent(self):
        self.assertEqual(app.generate(self.c),9)
        self.assertEqual(app.generate(self.c),0)
        bs=app.bills(self.c);self.assertEqual(sum(b['amount'] for b in bs),225000)
        app.TODAY=lambda:dt.date(2026,11,2)
        self.assertEqual(app.generate(self.c),2)
    def test_override_zero_and_rounding(self):
        app.save(self.c,'houses',dict(app.get(self.c,'houses',self.h),rate='0'))
        app.generate(self.c);self.assertEqual(sum(b['amount'] for b in app.bills(self.c)),0)
        self.assertEqual(app.money('2.345'),235)
        for invalid in ['NaN','Infinity','-1']:
            with self.assertRaises(ValueError):app.money(invalid)
    def test_annual_spaces_and_additional_fees(self):
        app.save(self.c,'spaces',dict(number='A001',kind='已售',house_id=self.h,management_rate='480'))
        app.save(self.c,'spaces',dict(number='A002',kind='已租',house_id=self.h))
        app.save(self.c,'fees',dict(name='垃圾费',house_id=self.h,basis='固定金额',rate='10',cycle='每月'))
        app.save(self.c,'fees',dict(name='公共能耗',basis='按面积',rate='0.20',cycle='每月',end='2026-02-01'))
        app.generate(self.c,'2027-12-01')
        bs=app.bills(self.c)
        self.assertEqual(sum(b['amount'] for b in bs if b['item'].startswith('车位管理')),192000)
        self.assertEqual(sum(b['amount'] for b in bs if b['item'].startswith('停车公共收益')),264000)
        self.assertEqual(sum(b['amount'] for b in bs if b['item']=='垃圾费'),24000)
        self.assertEqual(sum(b['amount'] for b in bs if b['item']=='公共能耗'),4000)
    def test_partial_payment_prepay_and_overpayment(self):
        app.generate(self.c,'2027-12-01');bs=app.bills(self.c)
        key=app.pay(self.c,{'date':'2026-09-06','allocations':[{'bill_id':bs[0]['id'],'amount':'100'},{'bill_id':bs[-1]['id'],'amount':'250'}]})
        current=app.bills(self.c);self.assertEqual(current[0]['paid'],10000);self.assertEqual(current[-1]['paid'],25000)
        self.assertEqual(sum(b['amount']-b['paid'] for b in current if b['due']<=app.TODAY().isoformat()),215000)
        with self.assertRaises(ValueError):app.pay(self.c,{'allocations':[{'bill_id':bs[0]['id'],'amount':'151'}]})
        self.c.execute('UPDATE payments SET void=1 WHERE id=?',(key,));self.assertTrue(all(b['paid']==0 for b in app.bills(self.c)))
    def test_payment_validation_is_atomic(self):
        app.generate(self.c);bs=app.bills(self.c)
        with self.assertRaises(ValueError):app.pay(self.c,{'allocations':[{'bill_id':bs[0]['id'],'amount':'20'},{'bill_id':bs[1]['id'],'amount':'999'}]})
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM payments').fetchone()[0],0)
        with self.assertRaises(ValueError):app.pay(self.c,{'date':'2027-01-01','allocations':[{'bill_id':bs[0]['id'],'amount':'20'}]})
    def test_relationship_validation(self):
        o=app.save(self.c,'owners',dict(name='王女士',house_ids=[self.h],phone='0012345'))
        sp=app.save(self.c,'spaces',dict(number='P01',kind='已售',house_id=self.h,owner_id=o))
        app.save(self.c,'vehicles',dict(plate='京a12345',house_id=self.h,owner_id=o,space_id=sp))
        self.assertEqual(app.records(self.c,'vehicles')[0]['plate'],'京A12345')
        with self.assertRaises(ValueError):app.delete(self.c,'owners',o)
        with self.assertRaises(ValueError):app.save(self.c,'vehicles',dict(plate='京A12345',house_id=self.h))
        app.save(self.c,'spaces',dict(number='空001',kind='空闲'))
    def test_manual_temporary_charge_and_historical_debt(self):
        f=app.save(self.c,'fees',dict(name='临停费',basis='按次',rate='3.5',cycle='手动'))
        app.manual_bill(self.c,dict(house_id=self.h,fee_id=f,units='4',due='2026-09-06'))
        app.manual_bill(self.c,dict(house_id=self.h,item='历史物业费',amount='800.25',due='2025-12-01'))
        self.assertEqual(sum(b['amount'] for b in app.bills(self.c)),81425)
    def test_import_preview_atomic_rollback_and_xlsx_roundtrip(self):
        rows=app.export_rows(self.c,'houses')
        raw=app.xlsx(rows)
        self.assertEqual(app.read_sheet(raw,'houses.xlsx'),[[str(v) for v in r] for r in rows])
        def payload(rows,preview):return {'filename':'test.xlsx','content':base64.b64encode(app.xlsx(rows)).decode(),'preview':preview}
        new=list(rows[1]);new[0]='H002';new[4]='102'
        r=app.import_rows(self.c,'houses',payload([rows[0],new],True));self.assertFalse(r['errors']);self.assertEqual(len(app.records(self.c,'houses')),1)
        bad=list(new);bad[0]='H003';bad[4]='103';bad[5]='非法面积'
        r=app.import_rows(self.c,'houses',payload([rows[0],new,bad],False));self.assertEqual(r['errors'][0]['row'],3);self.assertEqual(len(app.records(self.c,'houses')),1)
        r=app.import_rows(self.c,'houses',payload([rows[0],new],False));self.assertTrue(r['committed']);self.assertEqual(len(app.records(self.c,'houses')),2)
    def test_generated_bill_preserves_price(self):
        app.generate(self.c)
        app.save(self.c,'houses',dict(app.get(self.c,'houses',self.h),rate='3'))
        app.generate(self.c,'2026-10-01')
        bs=app.bills(self.c);self.assertEqual(bs[0]['amount'],25000);self.assertEqual(bs[-1]['amount'],30000)
    def test_duplicate_payment_request(self):
        app.generate(self.c);b=app.bills(self.c)[0]
        d={'request_id':'unique','allocations':[{'bill_id':b['id'],'amount':'10'}]}
        app.pay(self.c,d)
        with self.assertRaises(ValueError):app.pay(self.c,d)
        self.assertEqual(app.bills(self.c)[0]['paid'],1000)
    def test_next_month_auto_bill_and_annual_parking_bundle(self):
        sp=app.save(self.c,'spaces',dict(number='年租01',kind='已租',house_id=self.h,start='2026-09-01'))
        state=app.state(self.c)
        self.assertTrue(any(b['period']=='2026-10' and b['item']=='物业费' for b in state['bills']))
        parking=[b for b in state['bills'] if b['period']=='2026-09' and b['entity_id']==sp]
        self.assertEqual({b['item'].split(' · ')[0] for b in parking},{'车位管理费','停车公共收益'})
        self.assertEqual(sum(b['amount'] for b in parking),180000)
        self.assertEqual(len({b['group_key'] for b in parking}),1)
        pay_id=app.pay(self.c,{'date':'2026-09-06','allocations':[{'bill_id':b['id'],'amount':str(b['amount']/100)} for b in parking]})
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM allocations WHERE payment_id=?',(pay_id,)).fetchone()[0],2)
        self.assertTrue(all(b['coverage_end']=='2027-08-31' for b in parking))
    def test_custom_scope_status_vehicle_rule_and_reminder(self):
        h2=app.save(self.c,'houses',dict(community='测试小区',building='1栋',unit='2单元',number='201',area='80'))
        h3=app.save(self.c,'houses',dict(community='测试小区',building='2栋',unit='1单元',number='101',area='90'))
        v=app.save(self.c,'vehicles',dict(plate='测A10001',house_id=h2))
        app.save(self.c,'fees',dict(name='整栋附加',target_type='building',target_community='测试小区',target_building='1栋',basis='固定金额',rate='5',cycle='每月'))
        app.save(self.c,'fees',dict(name='车辆专用',target_type='vehicle',target_id=v,basis='固定金额',rate='88',cycle='每年',start='2026-09-01'))
        app.generate(self.c,'2026-09-01')
        building=[b for b in app.bills(self.c) if b['item']=='整栋附加']
        self.assertEqual({b['house_id'] for b in building},{self.h,h2})
        vehicle=[b for b in app.bills(self.c) if b['item'].startswith('车辆专用')]
        self.assertEqual(len(vehicle),1);self.assertEqual(vehicle[0]['entity_type'],'vehicle');self.assertEqual(vehicle[0]['entity_id'],v)
        custom=app.save(self.c,'space_statuses',dict(name='包年使用',management_rate='480',public_rate='1320',charge_management=True,charge_public=True,require_house=True,active=True))
        sp=app.save(self.c,'spaces',dict(number='自定义01',kind='包年使用',house_id=h3,start='2026-09-01'))
        app.save(self.c,'space_statuses',dict(app.get(self.c,'space_statuses',custom),name='年度使用'))
        self.assertEqual(app.get(self.c,'spaces',sp)['kind'],'年度使用')
        app.TODAY=lambda:dt.date(2027,8,20)
        state=app.state(self.c)
        reminder=next(r for r in state['reminders'] if r['space_id']==sp)
        self.assertEqual(reminder['due'],'2027-09-01');self.assertEqual(reminder['amount'],180000)
    def test_custom_month_interval(self):
        app.save(self.c,'fees',dict(name='双月车辆服务',target_type='all_houses',basis='固定金额',rate='6',cycle='自定义周期',interval_months='2',start='2026-01-01'))
        app.generate(self.c,'2026-07-01')
        rows=[b for b in app.bills(self.c) if b['item']=='双月车辆服务']
        self.assertEqual([b['period'] for b in rows],['2026-01','2026-03','2026-05','2026-07'])
        with self.assertRaises(ValueError):app.save(self.c,'fees',dict(name='错误周期',target_type='all_houses',basis='固定金额',rate='1',cycle='自定义周期',interval_months='0'))
    def test_legacy_parking_migration_preserves_total_and_payment(self):
        old={'number':'旧临停01','kind':'临停','house_id':self.h,'owner_id':'','annual_rate':'1800','start':'2026-09-01','note':'','active':True}
        self.c.execute('INSERT INTO records VALUES(?,?,?)',('spaces','LEGACY',json.dumps(old,ensure_ascii=False)))
        self.c.execute('''INSERT INTO bills(id,house_id,source,period,item,amount,due,created) VALUES(?,?,?,?,?,?,?,?)''',('OLDBILL',self.h,'spaces:LEGACY','2026-09','车位租赁费 · 旧临停01',180000,'2026-09-01','2026-09-01'))
        self.c.execute("INSERT INTO payments(id,date,amount,method,note,void,created) VALUES('OLDPAY','2026-09-06',180000,'现金','',0,'2026-09-06')")
        self.c.execute("INSERT INTO allocations VALUES('OLDPAY','OLDBILL',180000)");self.c.commit();self.c.close()
        app.init_db();self.c=app.connect()
        sp=app.get(self.c,'spaces','LEGACY');self.assertEqual(sp['management_rate'],'480');self.assertEqual(sp['public_rate'],'1320')
        rows=[b for b in app.bills(self.c) if b['entity_id']=='LEGACY'];self.assertEqual(len(rows),2);self.assertEqual(sum(b['amount'] for b in rows),180000);self.assertEqual(sum(b['paid'] for b in rows),180000)
        self.assertEqual({b['item'].split(' · ')[0] for b in rows},{'车位管理费','停车公共收益'})
        temp=next(x for x in app.records(self.c,'space_statuses') if x['name']=='临停');self.assertTrue(temp['charge_management']);self.assertTrue(temp['charge_public'])
    def test_excel_native_date_and_invalid_start(self):
        import io,zipfile,xml.etree.ElementTree as ET
        raw=app.xlsx([['起算日期'],['2026-09-01']])
        with zipfile.ZipFile(io.BytesIO(raw)) as z: files={name:z.read(name) for name in z.namelist()}
        xml=ET.fromstring(files['xl/worksheets/sheet1.xml'])
        cell=list(xml.iter('{'+app.NS+'}c'))[1]
        cell.clear();cell.attrib.update(r='A2',s='0',t='n')
        ET.SubElement(cell,'{'+app.NS+'}v').text=str((dt.date(2026,9,1)-dt.date(1899,12,30)).days)
        files['xl/worksheets/sheet1.xml']=ET.tostring(xml)
        files['xl/styles.xml']=('<styleSheet xmlns="'+app.NS+'"><cellXfs><xf numFmtId="14"/></cellXfs></styleSheet>').encode()
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as z:
            for name,value in files.items():z.writestr(name,value)
        self.assertEqual(app.read_sheet(output.getvalue(),'native.xlsx')[1],['2026-09-01'])
        with self.assertRaises(ValueError):app.save(self.c,'houses',dict(app.get(self.c,'houses',self.h),start='1900-01-01'))

    def test_history_payment_import_is_atomic_grouped_and_duplicate_safe(self):
        app.generate(self.c,'2026-01-01')
        headers=[label for _,label in app.HISTORY_PAYMENT_COLUMNS]
        def row(ref,receipt,item,period,amount,paid,house='H001'):
            values={
                'import_ref':ref,'receipt_ref':receipt,'house_id':house,'item':item,'period':period,
                'amount':amount,'paid':paid,'payment_date':'2026-02-10','method':'银行转账','note':'历史迁移'
            }
            return [values.get(key,'') for key,_ in app.HISTORY_PAYMENT_COLUMNS]
        def payload(rows,preview):
            return {'filename':'history.xlsx','content':base64.b64encode(app.xlsx([headers]+rows)).decode(),'preview':preview}
        rows=[row('HIST-001','RCPT-001','物业费','2026-01','250','250'),row('HIST-002','RCPT-001','历史清洁费','2027-09','80','50')]
        preview=app.import_history_payments(self.c,payload(rows,True))
        self.assertEqual((preview['count'],preview['matched_count'],preview['bill_count'],preview['payment_count']),(2,1,1,1))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM payments').fetchone()[0],0)
        result=app.import_history_payments(self.c,payload(rows,False))
        self.assertTrue(result['committed']);self.assertEqual(self.c.execute('SELECT COUNT(*) FROM history_imports').fetchone()[0],2)
        payment=self.c.execute('SELECT * FROM payments').fetchone();self.assertEqual(payment['amount'],30000)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM allocations WHERE payment_id=?',(payment['id'],)).fetchone()[0],2)
        history=next(b for b in app.bills(self.c) if b['item']=='历史清洁费');self.assertEqual((history['amount'],history['paid']),(8000,5000))
        duplicate=app.import_history_payments(self.c,payload(rows,False));self.assertEqual(len(duplicate['errors']),2)
        counts=tuple(self.c.execute('SELECT (SELECT COUNT(*) FROM payments),(SELECT COUNT(*) FROM bills),(SELECT COUNT(*) FROM history_imports)').fetchone())
        invalid=[row('HIST-003','RCPT-002','历史维修费','2025-11','30','30'),row('HIST-004','RCPT-002','历史维修费','2025-11','30','30','不存在')]
        failed=app.import_history_payments(self.c,payload(invalid,False));self.assertTrue(failed['errors'])
        self.assertEqual(tuple(self.c.execute('SELECT (SELECT COUNT(*) FROM payments),(SELECT COUNT(*) FROM bills),(SELECT COUNT(*) FROM history_imports)').fetchone()),counts)
        too_far=app.import_history_payments(self.c,payload([row('HIST-005','RCPT-003','远期费用','2031-10','10','10')],False));self.assertTrue(too_far['errors'])

    def test_bill_export_filters_and_money_columns(self):
        owner=app.save(self.c,'owners',dict(name='张女士',house_ids=[self.h]))
        app.save(self.c,'houses',dict(app.get(self.c,'houses',self.h),contact_id=owner))
        app.generate(self.c,'2026-03-01');bill=app.bills(self.c)[0]
        app.pay(self.c,{'date':'2026-02-10','allocations':[{'bill_id':bill['id'],'amount':'100'}]})
        rows=app.read_sheet(app.bill_export(self.c,{'start':'2026-01','end':'2026-01','house_id':self.h,'item':'物业费','status':'unpaid'}),'bills.xlsx')
        self.assertEqual(len(rows),2);self.assertEqual(rows[1][1],'H001');self.assertEqual(rows[1][6],'张女士')
        self.assertEqual(rows[1][11:14],['250.00','100.00','150.00'])
        paid=app.read_sheet(app.bill_export(self.c,{'start':'2026-01','end':'2026-03','status':'paid'}),'bills.xlsx')
        self.assertEqual(len(paid),1)
        with self.assertRaises(ValueError):app.bill_export(self.c,{'start':'2026-12','end':'2026-01'})

if __name__=='__main__':unittest.main(verbosity=2)
