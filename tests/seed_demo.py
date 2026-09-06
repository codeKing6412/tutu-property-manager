"""Create isolated, clearly fictional data for browser acceptance testing."""
from pathlib import Path
import sys
import json
import argparse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server as a

def seed(db_name='demo.db'):
    a.DB_PATH=Path(__file__).resolve().parents[1]/'qa-output'/db_name
    a.init_db()
    with a.connect() as c:
        if a.records(c,'houses'):return
        start=a.TODAY().replace(month=1,day=1).isoformat()
        c.execute('UPDATE settings SET data=?',(json.dumps(dict(a.DEFAULTS,name='云栖花园 · 演示',start=start)),))
        people=['陈先生','林女士','周先生','许女士','王先生','刘女士','赵先生','张女士','李先生','吴女士','徐先生','孙女士']
        for i,name in enumerate(people):
            h=a.save(c,'houses',dict(id=f'H{i+1:03}',community='云栖花园',building=f'{i//4+1}栋',unit=f'{i%2+1}单元',number=str((i//2+1)*100+1),area=str(86+i*4),rate='2.5'))
            o=a.save(c,'owners',dict(id=f'O{i+1:03}',name=name,phone=f'1380000{i:04}',identity='演示证件（非真实）',house_ids=[h],role='业主'))
            a.save(c,'houses',dict(a.get(c,'houses',h),contact_id=o))
            sp=a.save(c,'spaces',dict(id=f'S{i+1:03}',number=f'A-{i+1:03}',kind='已售' if i%2 else '已租',house_id=h,owner_id=o))
            a.save(c,'vehicles',dict(id=f'V{i+1:03}',plate=f'浙A{i+1:05}',house_id=h,owner_id=o,space_id=sp))
        a.save(c,'fees',dict(name='公共能耗费',basis='按面积',rate='0.15',cycle='每月'))
        a.save(c,'fees',dict(name='临停费',basis='按次',rate='3',cycle='手动'))
        for title in ['跟进3栋住户本月物业费','核对本周车位租赁到期名单','安排公共区域照明巡检']:
            a.save(c,'todos',dict(title=title,due=a.TODAY().isoformat()))
        a.generate(c)
        for i,h in enumerate(a.records(c,'houses')):
            for b in a.bills(c):
                if b['house_id']!=h['id'] or i%3==0 and b['period']==a.TODAY().strftime('%Y-%m'):continue
                if i%4==0 and b['period'].endswith('01'):continue
                a.pay(c,dict(date=b['due'],method=['微信','支付宝','银行转账'][i%3],allocations=[dict(bill_id=b['id'],amount=str(b['amount']/100))]))
    print(a.DB_PATH)
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--db',default='demo.db');args=parser.parse_args();seed(args.db)
