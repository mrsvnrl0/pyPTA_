// Run with node --test test_gex_ui.js. No external packages or network access.
const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/adaptive_crypto/static/gex.js', 'utf8');
const NOW = 1788793200000;

class Element {
  constructor() { this.children = []; this.attrs = {}; this.dataset = {}; this.text = ''; }
  setAttribute(key, value) { this.attrs[key] = value; }
  append(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return [this.text, ...this.children.map(child => child.textContent)].join(' '); }
}

function profile() {
  return {status:'ready', stale:false, asof_ms:NOW, options_valid_until_ms:NOW+300000,
    price_valid_until_ms:NOW+45000, smc_valid_until_ms:NOW+45000, smc_fresh:true,
    market:'Synthetic options', option_count:2, quote_currency:'USD', units:'USD delta / 1% move',
    spot:100, price:100, call_wall:{strike:110}, put_wall:{strike:90}, gamma_flip:98,
    net_gex:10, regime:'positive', basis_percent:0, flip_candidates:[98],
    levels:[{key:'call',price:110,distance_percent:10,relation:'above'},
      {key:'flip',price:98,distance_percent:-2,relation:'below'},
      {key:'put',price:90,distance_percent:-10,relation:'below'}],
    strikes:[{strike:90,net_gex:-20},{strike:110,net_gex:30}],
    curve:[{price:70,gex:-20},{price:98,gex:0},{price:130,gex:40}],
    zones:[{label:'Bearish HTF order block',low:109,high:111,side:'short',confluence:true,
      relation:'above',distance_percent:9,reference_only:false}]};
}

async function harness(initial = profile(), context = 'smc') {
  const elements = new Map(), timeouts = [], intervals = [];
  const panel = {dataset:{digits:'2',asset:'BTC',context}, classList:{toggle() {}},
    querySelector(selector) {
      if (context === 'nn' && ['.gex-poi-rows','.gex-target-policy'].includes(selector)) throw new Error('NN has no SMC table');
      if (!elements.has(selector)) elements.set(selector, new Element());
      return elements.get(selector);
    }};
  let now = NOW, provider = async () => ({json:async () => structuredClone(initial)});
  class Clock extends Date { static now() { return now; } }
  vm.runInNewContext(source, {Date:Clock, Intl, AbortSignal, fetch:(...args) => provider(...args),
    window:{setTimeout:fn => timeouts.push(fn), setInterval:fn => intervals.push(fn)},
    document:{querySelectorAll:() => [panel], createElement:() => new Element(),
      createElementNS:() => new Element(), addEventListener() {}}});
  await new Promise(resolve => setImmediate(resolve));
  return {element:selector => elements.get(selector),
    tick(stamp) { now = stamp; intervals.forEach(fn => fn()); },
    refresh:() => timeouts.shift()(),
    setProvider(value) { provider = value; }};
}

test('wall distances and confluence expire while a refresh is pending', async () => {
  const ui = await harness();
  assert.match(ui.element('.gex-confluence').textContent, /wall confluence/);
  assert.match(ui.element('[data-distance="call"]').textContent, /10% above/);
  ui.setProvider(() => new Promise(() => {}));
  ui.refresh();
  ui.tick(NOW+45001);
  assert.doesNotMatch(ui.element('.gex-confluence').textContent, /wall confluence/);
  assert.match(ui.element('[data-distance="call"]').textContent, /Waiting for Kraken/);
  assert.equal(ui.element('.gex-status').textContent.trim(), 'Options current');
  ui.tick(NOW+300001);
  assert.equal(ui.element('.gex-status').textContent.trim(), 'STALE');
  assert.match(ui.element('[data-distance="call"]').textContent, /Historical/);
});

test('NN renders both gamma graphs without SMC panels and still expires stale data', async () => {
  const ui = await harness(profile(),'nn');
  assert.equal(ui.element('.gex-status').textContent.trim(),'Options current');
  assert.match(ui.element('.gex-map').textContent,/Call wall/);
  assert.ok(ui.element('.gex-map').children.some(e => e.attrs.height === 4));
  assert.ok(ui.element('.gex-curve').children.some(e => e.attrs.points));
  assert.doesNotMatch(ui.element('.gex-confluence').textContent,/SMC|order.block|confluence/);
  ui.tick(NOW+300001);
  assert.equal(ui.element('.gex-status').textContent.trim(),'STALE');
  assert.match(ui.element('[data-distance="call"]').textContent,/Historical/);
});

test('outages retain historical prices and a fresh response restores the map', async () => {
  const ui = await harness();
  ui.setProvider(async () => { throw new Error('offline'); });
  await ui.refresh();
  assert.equal(ui.element('.gex-status').textContent.trim(), 'STALE');
  assert.match(ui.element('[data-level="call"]').textContent, /110/);
  assert.doesNotMatch(ui.element('.gex-confluence').textContent, /wall confluence/);
  ui.setProvider(async () => ({json:async () => profile()}));
  await ui.refresh();
  assert.equal(ui.element('.gex-status').textContent.trim(), 'Options current');
  assert.match(ui.element('.gex-confluence').textContent, /wall confluence/);
});

test('coincident levels keep labels separated and within the chart', async () => {
  const data = profile();
  data.call_wall.strike = data.put_wall.strike = data.gamma_flip = data.price = 100;
  data.zones = [];
  const ui = await harness(data);
  const labels = ui.element('.gex-map').children.filter(el => el.attrs.x === 442 && el.attrs['font-size'] === 20);
  assert.equal(labels.length, 4);
  for (let i = 0; i < labels.length; i++) {
    assert.ok(labels[i].attrs.y >= 0 && labels[i].attrs.y+25 < 344);
    if (i) assert.ok(labels[i].attrs.y-labels[i-1].attrs.y >= 44);
  }
  assert.equal(ui.element('.gex-curve').children.filter(el => el.attrs.r === 5).length, 1);
});

test('target confluence table and POI markers expire together', async () => {
  const data = profile();
  const poi = {kind:'bsl',label:'Buy-side liquidity',price:110,pivot_ms:1,side:'long'};
  data.pois = [poi];
  data.target_confluence = {enabled:true,ready:true,alignment_bps:10,valid_until_ms:NOW+30000,
    matches:[{key:'call',gex_price:110,poi,eligible:true,reason:'Positive-gamma wall aligns with SMC POI'}]};
  const ui = await harness(data);
  assert.match(ui.element('.gex-poi-rows').textContent, /High confluence/);
  assert.match(ui.element('.gex-poi-rows').textContent, /Buy-side liquidity/);
  assert.match(ui.element('.gex-map').textContent, /BSL/);
  ui.tick(NOW+30001);
  assert.doesNotMatch(ui.element('.gex-poi-rows').textContent, /High confluence/);
  assert.ok(ui.element('.gex-map').children.filter(e => e.attrs.r === 5).length === 0);
});

test('the table shows the newest match by time while the map retains all POIs', async () => {
  const data = profile();
  const reason = 'Positive-gamma wall aligns with SMC POI';
  data.pois = [115,110,114,111,113,112].map((price, i) => ({kind:'bsl',label:'Buy-side liquidity',
    price,pivot_ms:i+1,side:'long'}));
  data.target_confluence = {enabled:true,ready:true,alignment_bps:500,valid_until_ms:NOW+30000,
    matches:data.pois.map(poi => ({key:'call',gex_price:110,poi,eligible:true,reason})).reverse()};
  const ui = await harness(data);
  for (let refresh = 0; refresh < 3; refresh++) {
    const rows = ui.element('.gex-poi-rows').children;
    assert.equal(rows.length, 3);
    assert.equal(rows[0].children[2].textContent, 'High confluence · '+reason);
    assert.equal(rows[0].children[1].textContent,
      'Buy-side liquidity: $112.00');
    assert.equal(ui.element('.gex-map').children.filter(e => e.attrs.r === 5).length, 6);
    await ui.refresh();
  }
  data.target_confluence.matches = [];
  await ui.refresh();
  assert.equal(ui.element('.gex-poi-rows').children[0].children[2].textContent,
    'No confirmed alignment for target selection.');
});

test('the displayed explanation belongs to the newest match, including context-only matches', async () => {
  const data = profile();
  const poi = {kind:'bsl',label:'Buy-side liquidity',price:110,pivot_ms:1,side:'long'};
  const match = {key:'call',gex_price:110,poi,eligible:true,reason:'Shared reason'};
  data.pois = [poi];
  data.target_confluence = {enabled:true,ready:true,alignment_bps:500,valid_until_ms:NOW+30000,
    matches:[match, {...match}, {...match,poi:{...poi,price:111,pivot_ms:9},eligible:false,reason:'Latest context'},
      {...match,poi:{...poi,price:115,pivot_ms:3},reason:'Older eligible reason'}]};
  const ui = await harness(data);
  assert.equal(ui.element('.gex-poi-rows').children[0].children[1].textContent,
    'Buy-side liquidity: $111.00');
  assert.equal(ui.element('.gex-poi-rows').children[0].children[2].textContent,
    'Context · Latest context');
});

test('refreshes replace the latest counterpart without retaining older values or types', async () => {
  const data = profile();
  const poi = {kind:'bsl',label:'Buy-side liquidity',price:110,pivot_ms:1,side:'long'};
  data.pois = [poi, {...poi,pivot_ms:2}, {...poi,price:111,pivot_ms:3},
    {...poi,kind:'premium_ob',label:'Premium order block',pivot_ms:4}];
  data.target_confluence = {enabled:true,ready:true,alignment_bps:500,valid_until_ms:NOW+30000,
    matches:data.pois.map(poi => ({key:'call',gex_price:110,poi,eligible:true,reason:'Aligned'}))};
  const ui = await harness(data);
  for (let refresh = 0; refresh < 2; refresh++) {
    assert.equal(ui.element('.gex-poi-rows').children[0].children[1].textContent,
      'Premium order block: $110.00');
    await ui.refresh();
  }
  data.target_confluence.matches = [data.target_confluence.matches[2]];
  await ui.refresh();
  assert.equal(ui.element('.gex-poi-rows').children[0].children[1].textContent,
    'Buy-side liquidity: $111.00');
});

test('position alert labels expire even when editing pauses page reloads', async () => {
  const label = new Element();
  label.dataset = {alertCurrent:'1',alertExpires:NOW+30000};
  label.textContent = 'Latest signal';
  let now = NOW;
  const intervals = [];
  class Clock extends Date { static now() { return now; } }
  vm.runInNewContext(fs.readFileSync(__dirname + '/adaptive_crypto/static/dashboard.js', 'utf8'), {
    Date:Clock,
    ResizeObserver:class { observe() {} },
    sessionStorage:{getItem:() => null}, localStorage:{getItem:() => null},
    window:{setInterval:fn => intervals.push(fn), setTimeout() {}},
    document:{body:{dataset:{refresh:'15'}}, getElementById:() => null,
      querySelector:() => new Element(),
      querySelectorAll:selector => selector === '[data-alert-current="1"]' && label.dataset.alertCurrent === '1' ? [label] : [],
      addEventListener() {}}
  });
  now += 30000;
  intervals.forEach(fn => fn());
  assert.match(label.textContent, /Past alert/);
  assert.equal(label.dataset.alertCurrent, '0');
});
