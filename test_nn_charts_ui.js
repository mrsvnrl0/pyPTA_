// Offline tests for live NN prices, independent of page and NN signal refresh.
const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname+'/adaptive_crypto/static/dashboard.js','utf8');
const NOW = 1788793200000;
function classes() {
  const values = new Set();
  return {add:v=>values.add(v),remove:v=>values.delete(v),contains:v=>values.has(v),
    toggle(v,on) { if(on) values.add(v); else values.delete(v); }};
}
function candle(price,stamp=NOW,current=true) {
  return {stale:false,asof_ms:stamp,interval_ms:14400000,
    candles:[{t:Math.floor(stamp/14400000)*14400000,o:price,h:price,l:price,c:price,current}]};
}
async function harness(responses,initialStamp='',positionSignals=[],structurePanels=[]) {
  let now = NOW;
  const intervals = [], requests = [], cards = {};
  const chartElements = Object.keys(responses).map(asset => {
    const value = {textContent:'$90.00'}, label = {}, state = {};
    const display = {dataset:{asof:initialStamp},classList:classes(),
      querySelector:s=>s === '[data-price-value]' ? value : label};
    const canvas = {getBoundingClientRect:()=>({width:120}),getContext:()=>null};
    const element = {dataset:{asset,digits:'2'},classList:classes(),
      closest:()=>({querySelector:()=>display}),querySelector:s=>s === 'canvas' ? canvas : state};
    cards[asset] = {value,label,state,display,element};
    return element;
  });
  class Clock extends Date { static now() { return now; } }
  const document = {body:{dataset:{refresh:'3600'}},hidden:false,addEventListener(){},
    querySelector:()=>null,getElementById:()=>null,
    querySelectorAll:s=>s === '.mini-chart' ? chartElements : s === '[data-position-signal]' ? positionSignals : s === '[data-structure-expires]' ? structurePanels : []};
  const storage = {getItem:()=>null,setItem(){}};
  const context = {Date:Clock,document,localStorage:storage,sessionStorage:storage,AbortSignal,
    window:{devicePixelRatio:1,setTimeout(){},setInterval:fn=>intervals.push(fn)},
    fetch:async url=>{
      const asset = url.split('/').pop(); requests.push(asset);
      const data = responses[asset];
      if (data instanceof Error) throw data;
      return {ok:!data.stale,json:async()=>structuredClone(data)};
    }};
  vm.createContext(context); vm.runInContext(source,context);
  await new Promise(resolve=>setImmediate(resolve));
  return {cards,requests,
    tick(stamp) { now=stamp; intervals.forEach(fn=>fn()); },
    refresh:()=>context.refreshCharts()};
}

test('each NN card updates from its own current candle without a page reload',async()=>{
  const responses = {BTC:candle(65000.12),ETH:candle(2500.5),SOL:candle(140.25)};
  const ui = await harness(responses);
  assert.equal(ui.cards.BTC.value.textContent,'$65,000.12');
  assert.equal(ui.cards.ETH.value.textContent,'$2,500.50');
  assert.equal(ui.cards.SOL.value.textContent,'$140.25');
  assert.equal(ui.cards.BTC.label.textContent,'Live price · USD');
  responses.BTC = candle(65123.45,NOW+15000);
  ui.tick(NOW+15000); await ui.refresh();
  assert.equal(ui.cards.BTC.value.textContent,'$65,123.45');
});

test('expired prices are retained and clearly labelled during an outage',async()=>{
  const responses = {BTC:candle(65000.12)};
  const ui = await harness(responses);
  responses.BTC = new Error('offline');
  await ui.refresh(); ui.tick(NOW+45001);
  assert.equal(ui.cards.BTC.value.textContent,'$65,000.12');
  assert.equal(ui.cards.BTC.label.textContent,'Last price · USD');
  assert.ok(ui.cards.BTC.display.classList.contains('stale'));
});

test('a stale or completed-only candle cannot be presented as a live price',async()=>{
  const ui = await harness({BTC:{...candle(65000),stale:true},ETH:candle(2500,NOW,false)});
  for (const asset of ['BTC','ETH']) {
    assert.equal(ui.cards[asset].label.textContent,'Waiting for price');
    assert.equal(ui.cards[asset].value.textContent,'$90.00');
  }
});

test('an older cached candle cannot overwrite a newer server-rendered quote',async()=>{
  const ui = await harness({BTC:candle(50,NOW-15000)},String(NOW));
  assert.equal(ui.cards.BTC.value.textContent,'$90.00');
  assert.equal(ui.cards.BTC.label.textContent,'Live price · USD');
});

test('position action lights expire while editing without waiting for a page reload',async()=>{
  function position(expires,label) {
    const selection = {textContent:'Selected'}, action = {textContent:label};
    const light = {classList:classes(),attributes:{'aria-pressed':'true'},
      setAttribute(name,value) { this.attributes[name]=value; },querySelector:()=>selection};
    light.classList.add('is-selected');
    const panel = {dataset:{signalExpires:String(expires)},querySelector:s=>
      s === '.signal-light.is-selected' ? light.classList.contains('is-selected') ? light : null : action};
    return {panel,light,selection,action};
  }
  const expired = position(NOW-1,'TAKE PROFIT'), current = position(NOW+30000,'STOP LOSS');
  const ui = await harness({},'', [expired.panel,current.panel]);
  assert.equal(expired.action.textContent,'WAITING FOR FRESH READING');
  assert.equal(expired.light.attributes['aria-pressed'],'false');
  assert.equal(current.action.textContent,'STOP LOSS');
  assert.ok(current.light.classList.contains('is-selected'));
  ui.tick(NOW+30000);
  assert.equal(current.action.textContent,'WAITING FOR FRESH READING');
  assert.equal(current.selection.textContent,'Inactive');
  assert.equal(current.light.attributes['aria-pressed'],'false');
  assert.ok(!current.light.classList.contains('is-selected'));
  ui.tick(NOW+60000);
});

test('4H structure is labelled last known when its next close is missing',async()=>{
  const status = {textContent:'CURRENT'};
  const panel = {dataset:{structureExpires:NOW+1000},querySelector:()=>status};
  const ui = await harness({},'',[],[panel]);
  assert.equal(status.textContent,'CURRENT');
  ui.tick(NOW+1000);
  assert.equal(status.textContent,'LAST KNOWN · WAITING FOR CURRENT CANDLES');
});
