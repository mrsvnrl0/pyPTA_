// Credential form regressions against the actual scripts in a real browser.
const assert = require('node:assert/strict');
const {test, before, after} = require('node:test');
const fs = require('node:fs');
let chromium, browser;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional browser dependency. */ }
before(async () => {
  if (chromium) browser = await chromium.launch({headless:true, ...(process.env.PYPTA_BROWSER_PATH ? {executablePath:process.env.PYPTA_BROWSER_PATH} : {})});
});
after(async () => { if (browser) await browser.close(); });
const browserTest = (name, fn) => test(name, {skip:!chromium && 'Playwright unavailable'}, fn);
const source = name => fs.readFileSync(__dirname + '/adaptive_crypto/' + name, 'utf8');
const publicState = {editable:true, configured:false, state:'public', authenticated:false, error:null};
const savedState = {editable:true, configured:true, source:'saved', state:'not_connected', authenticated:false, error:null, message:'Connection saved.'};

async function harness(t, initial = publicState) {
  const page = await browser.newPage();
  t.after(() => page.close());
  const errors = [], posts = [];
  page.on('pageerror', error => errors.push(error.message));
  t.after(() => assert.deepEqual(errors, []));
  let state = initial, hold = null;
  await page.route('http://pypta.test/**', async route => {
    if (route.request().method() === 'POST') {
      posts.push({url:route.request().url(), body:route.request().postDataJSON()});
      state = route.request().url().endsWith('/public') ? {...publicState, message:'Public requests enabled.'} : savedState;
      return route.fulfill({contentType:'application/json', body:JSON.stringify(state)});
    }
    if (route.request().url().includes('/api/settings/deribit')) {
      const snapshot = state;
      if (hold) await hold;
      return route.fulfill({contentType:'application/json', body:JSON.stringify(snapshot)});
    }
    return route.fulfill({contentType:'text/html', body:`<!doctype html><body>
      ${source('templates/deribit_settings.html')}
      <script id="saved-strategy" type="application/json">{}</script>
      <form id="settings-form" action="/api/settings/save" data-revision="one">
        <input name="csrf_token" value="synthetic-csrf" type="hidden">
        <div id="asset-rows"><div><input data-asset-field="name" value="BTC"><button type="button">Remove</button></div></div>
        <button type="button" id="add-asset">Add</button><input id="refresh-seconds" value="15">
        <button id="save-settings" type="submit">Save settings</button>
      </form><p id="settings-result"></p><span id="unsaved-status"></span>
      <form id="restart-form"></form><p id="restart-result"></p></body>`});
  });
  await page.goto('http://pypta.test/settings');
  await page.evaluate(() => { window.polls = []; window.setInterval = callback => { window.polls.push(callback); return window.polls.length; }; });
  await page.addScriptTag({content:source('static/settings.js')});
  await page.addScriptTag({content:source('static/deribit_settings.js')});
  await page.waitForFunction(() => document.getElementById('deribit-status').textContent !== 'Checking connection status…');
  return {page, posts, state:value => { state = value; }, hold:value => { hold = value; }, poll:() => page.evaluate(() => window.polls[0]())};
}
async function enter(page) {
  await page.locator('[name=client_id]').fill('synthetic-client');
  await page.locator('[name=client_secret]').fill('synthetic-secret');
}

browserTest('saving masked credentials clears their values and sends only the connection form', async t => {
  const ui = await harness(t);
  assert.equal(await ui.page.locator('[name=client_id]').getAttribute('type'), 'password');
  assert.equal(await ui.page.locator('[name=client_secret]').getAttribute('type'), 'password');
  await enter(ui.page);
  await ui.page.locator('#deribit-form button[type=submit]').click();
  await ui.page.locator('#deribit-result').filter({hasText:'Connection saved.'}).waitFor();
  assert.deepEqual(ui.posts, [{url:'http://pypta.test/api/settings/deribit', body:{csrf_token:'synthetic-csrf', client_id:'synthetic-client', client_secret:'synthetic-secret'}}]);
  assert.deepEqual(await ui.page.locator('#deribit-form input').evaluateAll(inputs => inputs.map(i => [i.value, i.defaultValue])), [['',''], ['','']]);
  assert.match(await ui.page.locator('#deribit-status').textContent(), /Waiting for the next options request/);
  assert.equal(await ui.page.evaluate(() => localStorage.length), 0);
});

browserTest('status polling preserves unsaved credentials and displays authenticated success or failure', async t => {
  const ui = await harness(t);
  await enter(ui.page);
  ui.state({...savedState, state:'connected', authenticated:true});
  await ui.poll();
  assert.match(await ui.page.locator('#deribit-status').textContent(), /^Authenticated/);
  assert.equal(await ui.page.locator('[name=client_secret]').inputValue(), 'synthetic-secret');
  ui.state({...savedState, state:'error', error:'Authentication failed.'});
  await ui.poll();
  assert.equal(await ui.page.locator('#deribit-status').textContent(), 'Authentication failed.');
  assert.equal(await ui.page.locator('[name=client_secret]').inputValue(), 'synthetic-secret');
});

browserTest('public mode works with empty fields and sends no credentials', async t => {
  const ui = await harness(t, savedState);
  await ui.page.locator('#deribit-public').click();
  await ui.page.locator('#deribit-result').filter({hasText:'Public requests enabled.'}).waitFor();
  assert.deepEqual(ui.posts, [{url:'http://pypta.test/api/settings/deribit/public', body:{csrf_token:'synthetic-csrf'}}]);
  assert.match(await ui.page.locator('#deribit-status').textContent(), /^Public requests/);
});

browserTest('remote status keeps all credential controls disabled', async t => {
  const ui = await harness(t, {...savedState, editable:false});
  assert.equal(await ui.page.locator('#deribit-local-only').isVisible(), true);
  assert.equal(await ui.page.locator('#deribit-form input, #deribit-form button').evaluateAll(items => items.every(item => item.disabled)), true);
  await ui.page.locator('#deribit-form').evaluate(form => form.dispatchEvent(new Event('submit', {cancelable:true})));
  assert.equal(ui.posts.length, 0);
});

browserTest('the two settings forms preserve each other\'s unsaved edits', async t => {
  const ui = await harness(t);
  await enter(ui.page);
  await ui.page.locator('#save-settings').click();
  assert.equal(ui.posts.length, 0);
  assert.match(await ui.page.locator('#deribit-result').textContent(), /connection edits before changing other settings/);
  await ui.page.locator('#deribit-discard').click();
  await ui.page.locator('#refresh-seconds').fill('20');
  await enter(ui.page);
  await ui.page.locator('#deribit-form button[type=submit]').click();
  assert.equal(ui.posts.length, 0);
  assert.match(await ui.page.locator('#deribit-result').textContent(), /settings edits before changing this connection/);
});

browserTest('a poll started before saving cannot overwrite the new connection status', async t => {
  const ui = await harness(t);
  let release;
  ui.hold(new Promise(resolve => { release = resolve; }));
  const requested = ui.page.waitForRequest('http://pypta.test/api/settings/deribit');
  const pending = ui.poll();
  await requested;
  await enter(ui.page);
  await ui.page.locator('#deribit-form button[type=submit]').click();
  await ui.page.locator('#deribit-result').filter({hasText:'Connection saved.'}).waitFor();
  release(); await pending;
  assert.match(await ui.page.locator('#deribit-status').textContent(), /Waiting for the next options request/);
});
