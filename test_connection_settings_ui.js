const assert = require('node:assert/strict');
const {test, before, after} = require('node:test');
const fs = require('node:fs');
let chromium, browser;
try { ({chromium} = require('playwright')); } catch (_) {}
before(async () => { if (chromium) browser = await chromium.launch({headless:true, ...(process.env.PYPTA_BROWSER_PATH ? {executablePath:process.env.PYPTA_BROWSER_PATH} : {})}); });
after(async () => { if (browser) await browser.close(); });
const browserTest = (name, fn) => test(name, {skip:!chromium && 'Playwright unavailable'}, fn);
const file = name => fs.readFileSync(__dirname+'/adaptive_crypto/'+name,'utf8');
const initial = () => ({editable:true,
  ai:{provider:'openai', enabled:true, configured:true, state:'configured', profiles:{openai:{has_key:true,model:'model-a'},gemini:{has_key:false,model:''}}},
  telegram:{enabled:true,configured:true,state:'configured',has_token:true,has_chat:true}});
async function harness(t, editable = true) {
  const page = await browser.newPage(); t.after(() => page.close());
  const errors=[], posts=[]; page.on('pageerror', e => errors.push(e.message)); t.after(() => assert.deepEqual(errors,[]));
  let state={...initial(),editable}, hold=null, failPost=false;
  await page.route('http://pypta.test/**', async route => {
    const request=route.request(), url=request.url();
    if (request.method()==='POST') {
      const body=request.postDataJSON(); posts.push({url,body});
      if(failPost) return route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'Connection rejected.'})});
      const kind=url.includes('/ai')?'ai':'telegram';
      if (url.endsWith('/check')) state[kind]={...state[kind],state:'verified',message:'Credentials verified.'};
      else if(url.endsWith('/disable')) state[kind]={...state[kind],state:'off',configured:false,enabled:false,profiles:{},message:'Connection removed and disabled.'};
      else if(kind==='ai') state.ai={...state.ai,provider:body.provider,state:'configured',profiles:{...state.ai.profiles,[body.provider]:{has_key:true,model:body.model}},message:'Connection saved.'};
      else state.telegram={...state.telegram,state:'configured',message:'Connection saved.'};
      return route.fulfill({contentType:'application/json',body:JSON.stringify(state[kind])});
    }
    if(url.endsWith('/api/settings/connections')) {
      const snapshot=JSON.stringify(state); if(hold) await hold;
      return route.fulfill({contentType:'application/json',body:snapshot});
    }
    if(url.endsWith('/api/settings/deribit')) return route.fulfill({contentType:'application/json',body:JSON.stringify({editable,configured:false,state:'public'})});
    return route.fulfill({contentType:'text/html',body:`<!doctype html><body>
      ${file('templates/connection_settings.html')}${file('templates/deribit_settings.html')}
      <script id="saved-strategy" type="application/json">{}</script>
      <form id="settings-form" action="/api/settings/save"><input name="csrf_token" value="test-csrf" type="hidden">
      <div id="asset-rows"><div><input data-asset-field="name" value="BTC"><button type="button">Remove</button></div></div>
      <button id="add-asset" type="button">Add</button><input id="refresh-seconds" value="15"><button id="save-settings">Save</button></form>
      <p id="settings-result"></p><p id="unsaved-status"></p><form id="restart-form"></form><p id="restart-result"></p></body>`});
  });
  await page.goto('http://pypta.test/settings');
  await page.evaluate(() => { window.polls=[]; window.setInterval=fn=>window.polls.push(fn); });
  for(const script of ['settings','deribit_settings','connection_settings']) await page.addScriptTag({content:file('static/'+script+'.js')});
  await page.waitForFunction(() => !document.querySelector('[data-connection-status=ai]').textContent.startsWith('Loading'));
  return {page,posts,ai:page.locator('[data-connection=ai]'),telegram:page.locator('[data-connection=telegram]'),
    hold:value=>{hold=value;},state:value=>{state=value;},fail:value=>{failPost=value;},poll:()=>page.evaluate(()=>window.polls[1]())};
}
browserTest('provider switching clears typed keys and preserves independent saved model selections',async t=>{
  const ui=await harness(t);
  assert.equal(await ui.ai.locator('[name=model]').inputValue(),'model-a');
  await ui.ai.locator('[name=api_key]').fill('typed-openai-key');
  await ui.ai.locator('[name=provider]').selectOption('gemini');
  assert.equal(await ui.ai.locator('[name=api_key]').inputValue(),'');
  assert.equal(await ui.ai.locator('[name=model]').inputValue(),'');
  await ui.ai.locator('[name=model]').fill('model-g');
  await ui.ai.locator('[name=api_key]').fill('synthetic-gemini-key');
  await ui.ai.locator('[type=submit]').click();
  await ui.ai.locator('[data-connection-result]').filter({hasText:'Connection saved.'}).waitFor();
  assert.deepEqual(ui.posts[0].body,{csrf_token:'test-csrf',provider:'gemini',model:'model-g',api_key:'synthetic-gemini-key'});
  assert.equal(await ui.ai.locator('[name=api_key]').inputValue(),'');
  await ui.ai.locator('[name=provider]').selectOption('openai');
  assert.equal(await ui.ai.locator('[name=model]').inputValue(),'model-a');
  await ui.ai.locator('[type=submit]').click();
  await ui.page.waitForFunction(()=>document.querySelector('[data-connection=ai] [data-connection-result]').textContent==='Connection saved.');
  assert.equal(ui.posts[1].body.api_key,'');
});
browserTest('connection checks send only CSRF and retain honest verification wording',async t=>{
  const ui=await harness(t);
  await ui.telegram.locator('[data-connection-action=check]').click();
  await ui.telegram.locator('[data-connection-result]').filter({hasText:'Credentials verified.'}).waitFor();
  assert.deepEqual(ui.posts,[{url:'http://pypta.test/api/settings/connections/telegram/check',body:{csrf_token:'test-csrf'}}]);
  assert.match(await ui.telegram.textContent(),/without sending a message/);
  assert.equal(await ui.telegram.locator('[name=bot_token]').getAttribute('type'),'password');
  assert.equal(await ui.telegram.locator('[name=chat_id]').getAttribute('type'),'password');
});
browserTest('polls retain edits and failed saves never replace them or reveal saved keys',async t=>{
  const ui=await harness(t); await ui.ai.locator('[name=model]').fill('unsaved-model');
  await ui.ai.locator('[name=api_key]').fill('synthetic-secret'); await ui.poll();
  assert.equal(await ui.ai.locator('[name=model]').inputValue(),'unsaved-model');
  ui.fail(true); await ui.ai.locator('[type=submit]').click();
  await ui.ai.locator('[data-connection-result]').filter({hasText:'Connection rejected.'}).waitFor();
  assert.equal(await ui.ai.locator('[name=api_key]').inputValue(),'synthetic-secret');
  assert.equal(await ui.ai.locator('[name=api_key]').evaluate(input=>input.defaultValue),'');
});
browserTest('late polling cannot overwrite a saved provider selection',async t=>{
  const ui=await harness(t); let release; ui.hold(new Promise(resolve=>{release=resolve;}));
  const requested=ui.page.waitForRequest('http://pypta.test/api/settings/connections'); const pending=ui.poll(); await requested;
  await ui.ai.locator('[name=provider]').selectOption('gemini'); await ui.ai.locator('[name=model]').fill('model-g');
  await ui.ai.locator('[name=api_key]').fill('synthetic-gemini'); await ui.ai.locator('[type=submit]').click();
  await ui.ai.locator('[data-connection-result]').filter({hasText:'Connection saved.'}).waitFor();
  release(); await pending;
  assert.equal(await ui.ai.locator('[name=provider]').inputValue(),'gemini');
  assert.equal(await ui.ai.locator('[name=model]').inputValue(),'model-g');
});
browserTest('remote users can read status but cannot change connections',async t=>{
  const ui=await harness(t,false);
  assert.equal(await ui.page.locator('[data-connection-local]').isVisible(),true);
  assert.equal(await ui.page.locator('[data-connection] input, [data-connection] select, [data-connection] button').evaluateAll(nodes=>nodes.every(node=>node.disabled)),true);
  await ui.ai.evaluate(form=>form.dispatchEvent(new Event('submit',{cancelable:true})));
  assert.equal(ui.posts.length,0);
});
browserTest('AI, Telegram, Deribit and strategy forms protect each other\'s edits',async t=>{
  const ui=await harness(t);
  await ui.ai.locator('[name=model]').fill('unsaved-model');
  await ui.telegram.locator('[data-connection-action=check]').click();
  await ui.page.locator('#deribit-public').click(); await ui.page.locator('#save-settings').click();
  assert.equal(ui.posts.length,0);
  await ui.ai.locator('[data-connection-action=discard]').click();
  await ui.page.locator('#deribit-form [name=client_id]').fill('unsaved-deribit');
  await ui.telegram.locator('[data-connection-action=check]').click();
  assert.equal(ui.posts.length,0);
  assert.match(await ui.page.locator('#deribit-result').textContent(),/Deribit connection edits first/);
});
