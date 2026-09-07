const {chromium}=require('D:/Temp/GPT_Temp/property-qa/node_modules/playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
 const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),base=process.env.PROPERTY_BASE||'http://127.0.0.1:8767',errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const statusName='年度停车'+String(Date.now()).slice(-5);
 const buildingFee='整栋测试费'+String(Date.now()).slice(-5),vehicleFee='指定车辆特别费'+String(Date.now()).slice(-5);
 await page.goto(base+'/#spaces');await page.locator('h1').waitFor();
 assert.match(await page.locator('main').innerText(),/车位状态与默认年度收费/);
 assert.equal(await page.evaluate(()=>Boolean(document.querySelector('.space-status-panel').compareDocumentPosition(document.querySelector('.status-stats'))&Node.DOCUMENT_POSITION_FOLLOWING)),true);
 assert.equal(await page.locator('main [data-act="import"],main [data-act="export"]').count(),0);
 // Add a customizable status with split annual parking charges.
 await page.locator('[data-act="edit"][data-kind="space_statuses"]').first().click();
 await page.locator('[name="name"]').fill(statusName);await page.locator('[name="management_rate"]').fill('480');await page.locator('[name="public_rate"]').fill('1320');
 await page.locator('[name="charge_management"]').check();await page.locator('[name="charge_public"]').check();await page.locator('[name="require_house"]').check();
 await page.locator('#recordForm [type="submit"]').click();await page.waitForFunction(name=>document.querySelector('main')?.innerText.includes(name),statusName);
 // Add a whole-building rule through the combined target selector.
 await page.goto(base+'/#fees');await page.locator('h1').waitFor();
 await page.locator('[data-act="edit"][data-kind="fees"]').first().click();
 await page.locator('[name="name"]').fill(buildingFee);
 let target=page.locator('[name="target_choice"] option').filter({hasText:'1栋（整栋）'}).first();await page.locator('[name="target_choice"]').selectOption(await target.getAttribute('value'));
 await page.locator('[name="rate"]').fill('12.5');await page.locator('#recordForm [type="submit"]').click();await page.waitForFunction(name=>document.querySelector('main')?.innerText.includes(name),buildingFee);
 assert.match(await page.locator('main').innerText(),/1栋 整栋/);
 // Add a flexible vehicle-specific annual rule.
 await page.locator('[data-act="edit"][data-kind="fees"]').first().click();
 await page.locator('[name="name"]').fill(vehicleFee);target=page.locator('[name="target_choice"] option').filter({hasText:'车辆：浙A00001'}).first();await page.locator('[name="target_choice"]').selectOption(await target.getAttribute('value'));
 await page.locator('[name="rate"]').fill('88');await page.locator('[name="cycle"]').selectOption('每年');await page.locator('#recordForm [type="submit"]').click();await page.waitForFunction(name=>document.querySelector('main')?.innerText.includes(name),vehicleFee);
 // Update a rented space to Sep annual term and verify split bundle.
 await page.goto(base+'/#spaces');await page.locator('h1').waitFor();await page.locator('[data-act="edit"][data-kind="spaces"][data-id]').first().click();
 await page.locator('[name="kind"]').selectOption(statusName);await page.locator('[name="start"]').fill('2026-09-01');await page.locator('#recordForm [type="submit"]').click();await page.waitForTimeout(800);if(await page.locator('#modal').evaluate(x=>x.open))throw Error('space save failed: '+JSON.stringify(await page.evaluate(()=>({error:document.querySelector('#modalError').innerText,valid:document.querySelector('#recordForm').checkValidity(),invalid:[...document.querySelectorAll('#recordForm :invalid')].map(x=>[x.name,x.value,x.validationMessage])}))));await page.waitForFunction(name=>document.querySelector('main')?.innerText.includes(name),statusName);
 await page.goto(base+'/#billing');await page.locator('h1').waitFor();
 const parkingRows=page.locator('tbody tr').filter({hasText:'A-001'});assert.ok(await parkingRows.count()>=2);
 await parkingRows.first().locator('[data-act="parking-pay"]').click();await page.locator('#payForm').waitFor();
 assert.equal(await page.locator('.pay-amount').count(),2);assert.match(await page.locator('#payForm').innerText(),/车位管理费/);assert.match(await page.locator('#payForm').innerText(),/停车公共收益/);assert.match(await page.locator('#payTotal').innerText(),/1,800.00/);
 await page.locator('#payForm [type="submit"]').click();await page.locator('.receipt').waitFor();assert.match(await page.locator('.receipt').innerText(),/车位管理费/);assert.match(await page.locator('.receipt').innerText(),/停车公共收益/);
 await page.locator('[data-act="close-modal"]').click();
 await page.locator('main [data-act="payments"]').click();assert.equal(await page.locator('#modal th').nth(1).innerText(),'房屋');assert.match(await page.locator('#modal tbody').innerText(),/1栋 · 1单元 · 101/);await page.locator('[data-act="close-modal"]').click();
 // Flexible receivables audit with presets, custom ranges and structural scope.
 await page.goto(base+'/#dashboard');await page.locator('h1').waitFor();assert.match(await page.locator('main').innerText(),/应收核算/);assert.match(await page.locator('main').innerText(),/所选范围未收账单/);assert.ok(await page.locator('#auditScope option').filter({hasText:'整栋：'}).count()>0);assert.ok(await page.locator('#auditScope option').filter({hasText:'整单元：'}).count()>0);
 await page.locator('[data-audit-preset="previous-year"]').click();assert.equal(await page.locator('#auditStart').inputValue(),'2025-01');assert.equal(await page.locator('#auditEnd').inputValue(),'2025-12');await page.locator('#auditStart').fill('2026-03');await page.locator('#auditStart').dispatchEvent('change');await page.locator('#auditEnd').fill('2026-06');await page.locator('#auditEnd').dispatchEvent('change');assert.equal(await page.locator('[data-audit-preset="custom"]').getAttribute('class'),'active');assert.match(await page.locator('.flexible-audit').innerText(),/2026-03 至 2026-06/);
 await page.screenshot({path:path.resolve(__dirname,'../qa-output/v2-dashboard.png'),fullPage:true});
 await page.goto(base+'/#archives');await page.locator('h1').waitFor();assert.equal(await page.locator('main [data-act="import"],main [data-act="export"]').count(),0);
 await page.goto(base+'/#settings');assert.match(await page.locator('main').innerText(),/住户身份选项/);assert.match(await page.locator('main').innerText(),/停车费到期提前提醒/);assert.match(await page.locator('main').innerText(),/批量导入与导出/);assert.equal(await page.locator('main [data-act="import"]').count(),4);assert.equal(await page.locator('main [data-act="export"]').count(),4);assert.equal(await page.locator('main [data-act="history-import"]').count(),1);
 await page.locator('[data-act="bill-export"]').click();assert.match(await page.locator('#modal').innerText(),/起始月份.*结束月份.*房屋范围.*费项范围.*缴费状态/s);await page.locator('[data-act="close-modal"]').first().click();const exportResponse=await page.request.get(base+'/api/bill-export?start=2026-01&end=2026-12&status=unpaid');assert.equal(exportResponse.status(),200);assert.equal((await exportResponse.body()).subarray(0,2).toString(),'PK');
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS: centralized batch operations, bill export, payment house column, flexible accounting rules and reminders.');
})().catch(e=>{console.error(e);process.exit(1)});
