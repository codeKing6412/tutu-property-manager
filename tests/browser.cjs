const {chromium}=require(process.env.PROPERTY_PLAYWRIGHT||'D:/Temp/GPT_Temp/property-qa/node_modules/playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1100}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const base='http://127.0.0.1:8766';
 const out=path.resolve(__dirname,'../qa-output');fs.mkdirSync(out,{recursive:true});
 await page.goto(base);await page.locator('h1').waitFor();
 assert.match(await page.locator('h1').innerText(),/每一处家园/);
 await page.screenshot({path:path.join(out,'dashboard.png'),fullPage:true});
 await page.locator('#globalSearch').fill('浙A00001');await page.locator('.search-hit').first().click();
 await page.locator('#drawer').waitFor({state:'visible'});
 assert.match(await page.locator('#drawer').innerText(),/陈先生/);
 await page.locator('[data-act="detail-tab"][data-id="owners"]').click();
 assert.match(await page.locator('#drawer').innerText(),/13800000000/);
 await page.locator('[data-act="detail-tab"][data-id="vehicles"]').click();
 assert.match(await page.locator('#drawer').innerText(),/A-001/);
 await page.screenshot({path:path.join(out,'house-detail.png'),fullPage:true});
 await page.locator('[data-act="close-drawer"]').click();
 for(const route of ['archives','spaces','fees','settings','history','billing']){
   await page.goto(base+'/#'+route);await page.locator('h1').waitFor();
   assert.equal(await page.locator('#pageLabel').innerText(),({archives:'综合档案',spaces:'车位管理',fees:'费项配置',settings:'系统设置',history:'操作记录',billing:'收费中心'})[route]);
 }
 // Actual UI partial payment, printable receipt, and reversal.
 await page.selectOption('#billHouse','H001');
 await page.locator('[data-bill]:not(:disabled)').first().check();
 await page.locator('[data-act="pay-selected"]').click();
 await page.locator('.pay-amount').first().fill('1.25');
 await page.locator('#payForm button[type="submit"]').click();
 await page.locator('.receipt').waitFor();assert.match(await page.locator('.receipt').innerText(),/1.25/);
 await page.locator('#modal [data-act="payments"]').click();
 await page.locator('[data-act="void"]').first().click();
 await page.locator('[name="reason"]').fill('浏览器验收：撤销测试');
 await page.locator('#voidForm [type="submit"]').click();await page.waitForFunction(()=>!document.querySelector('#modal').open);
 // Generate two years ahead, then verify repeat cannot duplicate.
 await page.locator('[data-act="generate"]').click();
 await page.locator('[data-act="prepay-months"][data-n="24"]').click();
 const target=await page.locator('[name="until"]').inputValue();
 await page.locator('#generateForm [type="submit"]').click();await page.waitForFunction(()=>!document.querySelector('#modal').open);
 const generated=await (await page.request.post(base+'/api/generate',{data:{house_id:'H001',until:target+'-01'}})).json();assert.equal(generated.count,0);
 await page.screenshot({path:path.join(out,'billing.png'),fullPage:true});
 // New house through the real form; validates tree reconstruction.
 await page.goto(base+'/#archives');await page.locator('[data-act="edit"][data-kind="houses"]').first().click();
 const suffix=String(Date.now()).slice(-6);
 await page.locator('[name="building"]').fill('验收楼栋'+suffix);await page.locator('[name="unit"]').fill('1单元');await page.locator('[name="number"]').fill('901');await page.locator('[name="area"]').fill('88.88');await page.locator('[name="rate"]').fill('0');
 await page.locator('#recordForm [type="submit"]').click();await page.waitForFunction(()=>!document.querySelector('#modal').open);
 await page.waitForFunction(s=>document.querySelector('.tree')?.innerText.includes('验收楼栋'+s),suffix);
 // Export a real Excel file and validate workbook signature.
 const response=await page.request.get(base+'/api/export?kind=houses');assert.equal(response.status(),200);assert.equal((await response.body()).subarray(0,2).toString(),'PK');
 // Backup, alter data, restore, and confirm original state returns atomically.
 const backup=await (await page.request.get(base+'/api/backup')).body();
 const before=await (await page.request.get(base+'/api/state')).json();
 const marker='backup-check-'+Date.now();
 await page.request.post(base+'/api/save',{data:{kind:'todos',record:{title:marker}}});
 const restored=await page.request.post(base+'/api/restore',{data:{content:backup.toString('base64')}});
 assert.equal(restored.status(),200,await restored.text());
 const after=await (await page.request.get(base+'/api/state')).json();
 assert.equal(after.todos.length,before.todos.length);assert.ok(!after.todos.some(t=>t.title===marker));
 assert.equal(after.bills.length,before.bills.length);assert.equal(after.payments.length,before.payments.length);
 assert.ok(after.houses.find(h=>h.building==='验收楼栋'+suffix).monthly_amount===0);
 // Verify no horizontal overflow at desktop and compact width.
 for(const width of [1440,900,760]){await page.setViewportSize({width,height:1000});await page.goto(base+'/#dashboard');await page.locator('h1').waitFor();assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'horizontal overflow '+width)}
 assert.deepEqual(errors,[]);
 await browser.close();console.log('PASS: dashboard, all 7 routes, reverse plate lookup, 3 detail tabs, partial payment, receipt, reversal, 24-month prepayment, duplicate prevention, house creation, navigation tree, XLSX export, backup/restore, zero-rate display, responsive layout; no browser errors.');
})().catch(e=>{console.error(e);process.exit(1)});
