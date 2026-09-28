const assert = require('node:assert/strict');
const {test, before, after} = require('node:test');
const fs = require('node:fs');

let chromium, browser;
try { ({chromium} = require('playwright')); } catch (_) {}
before(async () => {
  if (chromium) browser = await chromium.launch({headless:true, ...(process.env.PYPTA_BROWSER_PATH ? {executablePath:process.env.PYPTA_BROWSER_PATH} : {})});
});
after(async () => { if (browser) await browser.close(); });
const browserTest = (name, fn) => test(name, {skip:!chromium && 'Playwright unavailable'}, fn);

const catalog = [
  {id:'parente_mlp_v1',label:'Parente 5/2',available:true,coverage:['BTC','ETH','SOL'],trained_through_ms:1670169599999,artifact_id:'parente-hash'},
  {id:'lstm_classifier_v1@abc123',label:'LSTM · abc123',available:true,status:'Trained and validated',reason:'Trained and validated',coverage:['BTC','ETH'],trained_through_ms:1750000000000,live_eligibility_after_ms:1780000000000,target_horizon:2,artifact_id:'abc123',report_url:'/reports/lstm'},
  {id:'gru_classifier_v1',label:'GRU',available:false,reason:'No trained artifact',coverage:[],report_url:'javascript:alert(1)'},
];
async function harness(t) {
  const page = await browser.newPage(); t.after(() => page.close());
  const errors = [], posts = []; let fail = false;
  page.on('pageerror', error => errors.push(error.message));
  t.after(() => assert.deepEqual(errors, []));
  await page.route('http://pypta.test/**', async route => {
    if (route.request().method() === 'POST') {
      posts.push(route.request().postDataJSON());
      return route.fulfill({status:fail?400:200,contentType:'application/json',body:JSON.stringify(fail?{error:'Invalid candidate selection.'}:{revision:'saved-revision',message:'Settings saved. Apply to activate.'})});
    }
    return route.fulfill({contentType:'text/html',body:`<!doctype html><html><body>
      <script id="saved-strategy" type="application/json">{"strategy_model":"neural_network","nn_model_id":"parente_mlp_v1","nn_limitations":false,"fee_rate":0.004}</script>
      <form id="settings-form" action="/api/settings/save" data-revision="original">
        <input type="hidden" name="csrf_token" value="synthetic-csrf">
        <div id="asset-rows"><div><input data-asset-field="name" value="BTC"><input data-asset-field="symbol" value="BTC/USD"><select data-asset-field="enabled"><option value="true" selected>On</option></select><input data-asset-field="price_decimals" value="2"><button type="button" class="remove-asset">Remove</button></div></div>
        <button type="button" id="add-asset">Add pair</button><input id="refresh-seconds" value="15">
        <label for="nn-model-id">NN model</label>
        <select id="nn-model-id" data-rule="nn_model_id" data-kind="text" data-saved-model-id="parente_mlp_v1" data-applied-model-id="parente_mlp_v1">
          <option value="parente_mlp_v1" selected>Parente 5/2</option>
          <option value="lstm_classifier_v1@abc123">LSTM · abc123</option>
          <option value="gru_classifier_v1" disabled>GRU · unavailable</option>
        </select>
        <p id="nn-model-state"></p><div id="nn-model-detail"><p id="nn-model-availability"></p><p id="nn-model-coverage"></p><p id="nn-model-provenance"></p><a id="nn-model-report" hidden>Validation report</a></div>
        <script id="nn-model-catalog" type="application/json">${JSON.stringify(catalog)}</script>
        <label>NN limitations<input type="checkbox" data-rule="nn_limitations" data-kind="bool"></label>
        <button type="submit">Save</button><button type="reset">Discard</button>
        <p id="settings-result" role="status"></p><p id="unsaved-status">No unsaved edits</p>
      </form>
      <form id="restart-form"></form><p id="restart-result"></p>
      </body></html>`});
  });
  await page.goto('http://pypta.test/settings');
  await page.addScriptTag({content:fs.readFileSync(__dirname+'/adaptive_crypto/static/settings.js','utf8')});
  return {page, posts, selector:page.locator('#nn-model-id'), box:page.locator('[data-rule=nn_limitations]'), fail:value=>{fail=value;}};
}

browserTest('trained candidate is selectable without changing limitations; save does not apply it', async t => {
  const ui = await harness(t);
  assert.equal(await ui.selector.inputValue(), 'parente_mlp_v1');
  assert.equal(await ui.selector.locator('option[value=gru_classifier_v1]').isDisabled(), true);
  assert.equal(await ui.box.isChecked(), false);
  await ui.selector.selectOption('lstm_classifier_v1@abc123');
  assert.match(await ui.page.locator('#nn-model-state').textContent(), /Selected: LSTM.*Saved: Parente.*Applied: Parente/);
  assert.match(await ui.page.locator('#nn-model-coverage').textContent(), /BTC, ETH/);
  assert.equal(await ui.page.locator('#nn-model-availability').textContent(), 'Trained and validated');
  assert.match(await ui.page.locator('#nn-model-provenance').textContent(), /abc123/);
  assert.match(await ui.page.locator('#nn-model-provenance').textContent(), /Training\/calibration through: 2025-06.*Live eligible after: 2026-05/);
  assert.equal(await ui.page.locator('#nn-model-report').getAttribute('href'), '/reports/lstm');
  await ui.page.locator('#settings-form [type=submit]').click();
  await ui.page.waitForFunction(() => document.getElementById('settings-result').textContent.includes('saved.'));
  assert.equal(ui.posts[0].settings.strategy.nn_model_id, 'lstm_classifier_v1@abc123');
  assert.equal(ui.posts[0].settings.strategy.nn_limitations, false);
  assert.equal(ui.posts[0].settings.strategy.fee_rate, .004);
  assert.equal(ui.posts[0].csrf_token, 'synthetic-csrf');
  assert.match(await ui.page.locator('#nn-model-state').textContent(), /Saved: LSTM.*Applied: Parente.*Apply saved settings/);
});

browserTest('discard and failed save keep the last saved selection and applied model separate', async t => {
  const ui = await harness(t);
  await ui.selector.selectOption('lstm_classifier_v1@abc123');
  await ui.page.locator('#settings-form [type=reset]').click();
  assert.equal(await ui.selector.inputValue(), 'parente_mlp_v1');
  await ui.selector.selectOption('lstm_classifier_v1@abc123');
  await ui.page.locator('#settings-form [type=submit]').click();
  await ui.page.waitForFunction(() => document.getElementById('settings-result').textContent.includes('saved.'));
  await ui.selector.selectOption('parente_mlp_v1');
  await ui.page.locator('#settings-form [type=reset]').click();
  assert.equal(await ui.selector.inputValue(), 'lstm_classifier_v1@abc123');
  ui.fail(true);
  await ui.selector.selectOption('parente_mlp_v1');
  await ui.page.locator('#settings-form [type=submit]').click();
  await ui.page.waitForFunction(() => document.getElementById('settings-result').classList.contains('danger'));
  assert.equal(await ui.selector.inputValue(), 'parente_mlp_v1');
  assert.match(await ui.page.locator('#unsaved-status').textContent(), /Unsaved edits/);
  await ui.page.locator('#settings-form [type=reset]').click();
  assert.equal(await ui.selector.inputValue(), 'lstm_classifier_v1@abc123');
  assert.match(await ui.page.locator('#nn-model-state').textContent(), /Saved: LSTM.*Applied: Parente/);
});

browserTest('untrained candidates cannot be selected and unsafe report URLs are not linked', async t => {
  const ui = await harness(t);
  assert.equal(await ui.selector.locator('option[value=gru_classifier_v1]').isDisabled(), true);
  await ui.page.evaluate(() => {
    const selector = document.getElementById('nn-model-id');
    selector.querySelector('option[value=gru_classifier_v1]').disabled = false;
    selector.value = 'gru_classifier_v1';
    selector.dispatchEvent(new Event('change', {bubbles:true}));
  });
  assert.match(await ui.page.locator('#nn-model-availability').textContent(), /Unavailable · No trained artifact/);
  assert.equal(await ui.page.locator('#nn-model-report').isVisible(), false);
  assert.equal(await ui.page.locator('#nn-model-report').getAttribute('href'), null);
});
