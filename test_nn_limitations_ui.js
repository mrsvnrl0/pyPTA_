const assert=require('node:assert/strict');
const {test,before,after}=require('node:test');
const fs=require('node:fs');
let chromium,browser;
try{({chromium}=require('playwright'));}catch(_){}
before(async()=>{if(chromium)browser=await chromium.launch({headless:true,...(process.env.PYPTA_BROWSER_PATH?{executablePath:process.env.PYPTA_BROWSER_PATH}:{})});});
after(async()=>{if(browser)await browser.close();});
const browserTest=(name,fn)=>test(name,{skip:!chromium&&'Playwright unavailable'},fn);
async function harness(t){
  const page=await browser.newPage();t.after(()=>page.close());
  const posts=[],errors=[];let fail=false;
  page.on('pageerror',error=>errors.push(error.message));t.after(()=>assert.deepEqual(errors,[]));
  await page.route('http://pypta.test/**',async route=>{
    if(route.request().method()==='POST'){
      posts.push(route.request().postDataJSON());
      return route.fulfill({status:fail?400:200,contentType:'application/json',body:JSON.stringify(fail?{error:'Settings changed in another tab.'}:{revision:'saved-revision',message:'Settings saved. Apply to activate.'})});
    }
    return route.fulfill({contentType:'text/html',body:`<html><body>
      <script id="saved-strategy" type="application/json">{"strategy_model":"neural_network","nn_limitations":true,"nn_stop_loss":0.1,"fee_rate":0.004}</script>
      <form id="settings-form" action="/api/settings/save" data-revision="original">
      <input type="hidden" name="csrf_token" value="token">
      <div id="asset-rows"><div><input data-asset-field="name" value="BTC"><input data-asset-field="symbol" value="BTC/USD"><select data-asset-field="enabled"><option value="true">On</option></select><input data-asset-field="price_decimals" value="2"><button type="button" class="remove-asset">Remove</button></div></div>
      <button type="button" id="add-asset">Add</button><input id="refresh-seconds" value="15">
      <label>NN limitations<input type="checkbox" data-rule="nn_limitations" data-kind="bool" checked></label>
      <button type="submit">Save settings</button><button type="reset">Discard</button>
      <p id="settings-result" role="status"></p><p id="unsaved-status">No unsaved edits</p></form>
      <form id="restart-form"></form><p id="restart-result"></p>
      </body></html>`});
  });
  await page.goto('http://pypta.test/settings');
  await page.addScriptTag({content:fs.readFileSync(__dirname+'/adaptive_crypto/static/settings.js','utf8')});
  return {page,posts,box:page.locator('[data-rule=nn_limitations]'),fail:value=>{fail=value;}};
}
browserTest('NN checkbox saves false and true as booleans and preserves other strategy settings',async t=>{
  const ui=await harness(t);
  assert.equal(await ui.box.isChecked(),true);
  await ui.box.focus();await ui.page.keyboard.press('Space');
  assert.match(await ui.page.locator('#unsaved-status').textContent(),/Unsaved edits/);
  await ui.page.locator('[type=submit]').click();
  await ui.page.waitForFunction(()=>document.getElementById('settings-result').textContent.includes('saved.'));
  assert.equal(ui.posts[0].settings.strategy.nn_limitations,false);
  assert.equal(ui.posts[0].settings.strategy.fee_rate,.004);
  assert.equal(ui.posts[0].csrf_token,'token');
  await ui.box.check();await ui.page.locator('[type=submit]').click();
  await ui.page.waitForFunction(()=>document.getElementById('unsaved-status').textContent==='No unsaved edits');
  assert.equal(ui.posts[1].settings.strategy.nn_limitations,true);
  assert.equal(ui.posts[1].revision,'saved-revision');
});
browserTest('discard restores the last successfully saved checkbox value',async t=>{
  const ui=await harness(t);
  await ui.box.uncheck();await ui.page.locator('[type=reset]').click();
  assert.equal(await ui.box.isChecked(),true);
  await ui.box.uncheck();await ui.page.locator('[type=submit]').click();
  await ui.page.waitForFunction(()=>document.getElementById('settings-result').textContent.includes('saved.'));
  await ui.box.check();await ui.page.locator('[type=reset]').click();
  assert.equal(await ui.box.isChecked(),false);
});
browserTest('failed save preserves an unsaved mode and does not change the reset baseline',async t=>{
  const ui=await harness(t);ui.fail(true);await ui.box.uncheck();
  await ui.page.locator('[type=submit]').click();
  await ui.page.waitForFunction(()=>document.getElementById('settings-result').classList.contains('danger'));
  assert.equal(await ui.box.isChecked(),false);
  assert.match(await ui.page.locator('#unsaved-status').textContent(),/Unsaved edits/);
  await ui.page.locator('[type=reset]').click();assert.equal(await ui.box.isChecked(),true);
});
