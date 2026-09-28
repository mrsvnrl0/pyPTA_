// Real DOM regressions. Install Playwright and Chrome (or its Chromium) to run.
const assert = require('node:assert/strict');
const {test, before, after} = require('node:test');
const fs = require('node:fs');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional browser test dependency. */ }
let browser;
before(async () => {
  if (chromium) browser = await chromium.launch(process.env.PYPTA_BROWSER_PATH
    ? {headless:true, executablePath:process.env.PYPTA_BROWSER_PATH} : {headless:true});
});
after(async () => { if (browser) await browser.close(); });
const script = name => fs.readFileSync(__dirname + '/adaptive_crypto/static/' + name + '.js', 'utf8');
const html = (body, generation = 'one') => `<!doctype html><body data-generation="${generation}" data-refresh="5"><main>${body}<p id="refresh-status"></p></main></body>`;
const form = (value = '10', action = '/save') => `<form class="position-form" action="${action}"><input name="stop_price" value="${value}"><button type="submit">Save</button><p class="form-result"></p></form>`;
async function harness(t, initial, dashboard = false) {
  const page = await browser.newPage();
  t.after(() => page.close());
  let next = initial, fail = false, hold = null, requests = 0;
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  t.after(() => assert.deepEqual(errors, []));
  await page.route('http://pypta.test/**', async route => {
    requests++;
    if (hold) await hold;
    if (fail) await route.fulfill({status:503, body:'Unavailable'});
    else await route.fulfill({contentType:'text/html', body:next});
  });
  await page.goto('http://pypta.test/positions');
  await page.addScriptTag({content:script('live_refresh')});
  if (dashboard) {
    await page.evaluate(() => { window.setTimeout = () => 0; window.setInterval = () => 0; });
    await page.addScriptTag({content:script('dashboard')});
  }
  return {page, set:body => { next = body; }, fail:value => { fail = value; },
    hold:promise => { hold = promise; }, requests:() => requests,
    refresh:() => page.evaluate(() => window.pyptaRefresh(() => !window.editing))};
}
const browserTest = (name, fn) => test(name, {skip:!chromium && 'Playwright unavailable'}, fn);

browserTest('refresh keeps charts, open disclosures and focused controls while updating readings', async t => {
  const body = value => `<article data-refresh-key="asset:BTC"><p id="reading">${value}</p><div class="mini-chart" data-asset="BTC"><canvas></canvas></div><section class="gex" data-asset="BTC"><svg></svg></section><div data-live-price>live</div><details data-panel="evidence"><summary>Evidence</summary><p>${value}</p></details>${form(value)}</article>`;
  const ui = await harness(t, html(body('10')));
  await ui.page.evaluate(() => {
    window.kept = Array.from(document.querySelectorAll('canvas, svg, details, input, [data-live-price]'));
    document.querySelector('details').open = true;
    document.querySelector('input').focus();
    document.querySelector('svg').setAttribute('data-interaction', 'kept');
  });
  ui.set(html(body('20')));
  await ui.refresh();
  assert.deepEqual(await ui.page.evaluate(() => ({
    same:window.kept.every(node => node.isConnected), open:document.querySelector('details').open,
    reading:document.getElementById('reading').textContent,
    input:document.querySelector('input').value,
    interaction:document.querySelector('svg').getAttribute('data-interaction')
  })), {same:true, open:true, reading:'20', input:'20', interaction:'kept'});
  assert.equal(await ui.page.evaluate(() => document.activeElement === document.querySelector('input')), true);
  assert.equal(ui.requests(), 2);
});

browserTest('a form edited during an in-flight refresh is retained, then clean forms resume', async t => {
  const ui = await harness(t, html('<p id="reading">old</p>'+form()));
  let release;
  ui.hold(new Promise(resolve => { release = resolve; }));
  ui.set(html('<p id="reading">new</p>'+form('20')));
  const request = ui.page.waitForRequest('http://pypta.test/positions');
  const updating = ui.refresh();
  await request;
  await ui.page.evaluate(() => { window.editing = true; document.querySelector('input').value = 'unsaved'; });
  release(); await updating;
  assert.equal(await ui.page.locator('input').inputValue(), 'unsaved');
  assert.equal(await ui.page.locator('#reading').textContent(), 'old');
  await ui.page.evaluate(() => { window.editing = false; });
  await ui.refresh();
  assert.equal(await ui.page.locator('input').inputValue(), '20');
});

browserTest('inserted positions bind forms once and keyed removal retains the surviving position', async t => {
  const card = (id, value) => `<article data-position-id="${id}"><p>${value}</p>${form(value, '/save/'+id)}</article>`;
  const ui = await harness(t, html(card('first','10')), true);
  ui.set(html(card('new','20')+card('first','30')));
  await ui.refresh();
  await ui.page.evaluate(() => { window.survivor = document.querySelector('[data-position-id="first"]'); });
  ui.set(html(card('first','40')));
  await ui.refresh();
  assert.equal(await ui.page.evaluate(() => window.survivor === document.querySelector('article')), true);
  await ui.page.locator('input').fill('77');
  assert.equal(await ui.page.locator('form').getAttribute('data-dirty'), '1');
  assert.equal(await ui.page.evaluate(() => positionFormDirty()), true);
});

browserTest('refresh failures retain readings and recover without a navigation', async t => {
  const ui = await harness(t, html('<p id="reading">old</p>'));
  ui.fail(true); await ui.refresh();
  assert.equal(await ui.page.locator('#reading').textContent(), 'old');
  assert.match(await ui.page.locator('#refresh-status').textContent(), /Retrying/);
  ui.fail(false); ui.set(html('<p id="reading">fresh</p>')); await ui.refresh();
  assert.equal(await ui.page.locator('#reading').textContent(), 'fresh');
  assert.equal(await ui.page.locator('#refresh-status').textContent(), '');
});

browserTest('dashboard polling calls reconciliation and pauses during form editing', async t => {
  const ui = await harness(t, html('<p id="reading">old</p>'+form()), true);
  ui.set(html('<p id="reading">fresh</p>'+form('20')));
  await ui.page.evaluate(() => refreshDashboard());
  assert.equal(await ui.page.locator('#reading').textContent(), 'fresh');
  await ui.page.locator('input').fill('99');
  ui.set(html('<p id="reading">blocked</p>'+form('30')));
  await ui.page.evaluate(() => refreshDashboard());
  assert.equal(await ui.page.locator('#reading').textContent(), 'fresh');
  assert.equal(await ui.page.locator('input').inputValue(), '99');
  assert.equal(ui.requests(), 2);
});

browserTest('dismissed warnings stay dismissed when the next snapshot contains them', async t => {
  const warning = '<div data-warning-id="warning:1"><span>Notice</span><button class="notice-dismiss" hidden>Dismiss</button></div>';
  const ui = await harness(t, html(warning), true);
  await ui.page.locator('button').click();
  await ui.refresh();
  assert.equal(await ui.page.locator('[data-warning-id]').count(), 0);
});

browserTest('a changed runtime generation reloads configuration only after editing ends', async t => {
  const ui = await harness(t, html(form()));
  ui.set(html(form('20'), 'two'));
  await ui.page.evaluate(() => { window.editing = true; });
  await ui.refresh();
  assert.equal(ui.requests(), 1);
  await ui.page.evaluate(() => { window.editing = false; });
  await Promise.all([ui.page.waitForNavigation(), ui.refresh().catch(error => {
    if (!/Execution context was destroyed/.test(error.message)) throw error;
  })]);
  assert.equal(await ui.page.locator('body').getAttribute('data-generation'), 'two');
  assert.equal(ui.requests(), 3);
});

browserTest('newly inserted forms submit once after repeated refreshes', async t => {
  const ui = await harness(t, html('<p>No positions</p>'), true);
  ui.set(html(form('20')));
  await ui.refresh(); await ui.refresh();
  let posts = 0;
  await ui.page.route('http://pypta.test/save', async route => {
    posts++;
    await route.fulfill({status:400, contentType:'application/json', body:JSON.stringify({error:'Synthetic failure'})});
  });
  await ui.page.locator('button').click();
  await ui.page.locator('.form-result').filter({hasText:'Synthetic failure'}).waitFor();
  assert.equal(posts, 1);
});

browserTest('fresh server quotes recover a failed chart feed without overwriting newer chart prices', async t => {
  const now = Date.now();
  const quote = (stamp, value) => `<div data-live-price data-asof="${stamp}"><b data-price-value>${value}</b><small data-price-status>Live price</small></div>`;
  const ui = await harness(t, html(quote(now-10000, '$10')));
  await ui.page.evaluate(() => { window.priceNode = document.querySelector('[data-live-price]'); });
  ui.set(html(quote(now, '$20')));
  await ui.refresh();
  assert.equal(await ui.page.locator('[data-price-value]').textContent(), '$20');
  assert.equal(await ui.page.evaluate(() => window.priceNode === document.querySelector('[data-live-price]')), true);
  ui.set(html(quote(now-5000, '$15')));
  await ui.refresh();
  assert.equal(await ui.page.locator('[data-price-value]').textContent(), '$20');
});

browserTest('a generation change during editing retains values but prevents submitting an expired form', async t => {
  const ui = await harness(t, html(form()), true);
  let release;
  ui.hold(new Promise(resolve => { release = resolve; }));
  ui.set(html(form('20'), 'two'));
  const request = ui.page.waitForRequest('http://pypta.test/positions');
  const updating = ui.refresh();
  await request;
  await ui.page.evaluate(() => { window.editing = true; document.querySelector('input').value = 'unsaved'; });
  release(); await updating;
  assert.equal(await ui.page.locator('input').inputValue(), 'unsaved');
  assert.equal(await ui.page.locator('button').isDisabled(), true);
  assert.match(await ui.page.locator('#refresh-status').textContent(), /Copy your unsaved edits/);
  const requests = ui.requests();
  await ui.page.evaluate(() => document.querySelector('form').dispatchEvent(new Event('submit', {cancelable:true})));
  assert.match(await ui.page.locator('.form-result').textContent(), /reload before saving/);
  assert.equal(ui.requests(), requests);
});

browserTest('position dialog and newly received suggested stops work after refresh', async t => {
  const controls = '<button id="add-position">Add position</button><dialog id="add-position-dialog"><button id="cancel-add-position">Close</button>'+form()+'</dialog>';
  const ui = await harness(t, html(controls), true);
  await ui.page.addScriptTag({content:script('positions')});
  await ui.page.locator('#add-position').click();
  assert.equal(await ui.page.locator('dialog').evaluate(node=>node.open), true);
  await ui.page.locator('#cancel-add-position').click();
  const stopForm = '<form class="position-form"><input name="stop_price"><button type="button" data-suggested-stop="123.45">Use suggested stop</button><p class="form-result"></p></form>';
  ui.set(html(controls+stopForm));
  await ui.refresh();
  await ui.page.getByRole('button',{name:'Use suggested stop'}).click();
  assert.equal(await ui.page.locator('main > form input').inputValue(), '123.45');
  assert.equal(await ui.page.locator('main > form').getAttribute('data-dirty'), '1');
});
