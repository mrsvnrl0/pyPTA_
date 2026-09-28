// Remember dismissed warnings and bind newly received notices only once.
const boundNotices = new WeakSet();
function bindWarnings() {
  const storageKey = 'adaptive-crypto-dismissed-warnings';
  let dismissed = new Set();
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || '[]');
    if (Array.isArray(saved)) dismissed = new Set(saved.filter(id => typeof id === 'string'));
  } catch (_) { /* Dismissal still works when browser storage is unavailable. */ }
  const notices = Array.from(document.querySelectorAll('[data-warning-id]'));
  const currentIds = new Set(notices.map(notice => notice.dataset.warningId));
  dismissed = new Set([...dismissed].filter(id => currentIds.has(id)));
  try { localStorage.setItem(storageKey, JSON.stringify([...dismissed])); }
  catch (_) { /* Removed warnings need no persistent dismissal after a database purge. */ }
  notices.forEach(notice => {
    const id = notice.dataset.warningId;
    if (dismissed.has(id)) { notice.remove(); return; }
    const button = notice.querySelector('.notice-dismiss');
    button.hidden = false;
    if (boundNotices.has(button)) return;
    boundNotices.add(button);
    button.addEventListener('click', () => {
      try {
        const saved = JSON.parse(localStorage.getItem(storageKey) || '[]');
        if (Array.isArray(saved)) dismissed = new Set(saved);
      } catch (_) { /* Retain this page's dismissal set if storage is unavailable. */ }
      dismissed.add(id);
      notice.remove();
      try { localStorage.setItem(storageKey, JSON.stringify([...dismissed])); }
      catch (_) { /* Keep the warning dismissed for this page even if saving fails. */ }
    });
  });
}

bindWarnings();
document.addEventListener('pypta:refreshed', bindWarnings);

// Preserve disclosure state across navigation and newly received panels.
const boundPanels = new WeakSet();
function bindPanels() {
try {
  const storageKey = 'adaptive-crypto-panels';
  const saved = JSON.parse(sessionStorage.getItem(storageKey) || '{}');
  document.querySelectorAll('details[data-panel]').forEach(panel => {
    if (boundPanels.has(panel)) return;
    boundPanels.add(panel);
    panel.open = saved[panel.dataset.panel] === true;
    panel.addEventListener('toggle', () => {
      try {
        const states = Object.fromEntries(Array.from(document.querySelectorAll('details[data-panel]'), p => [p.dataset.panel, p.open]));
        sessionStorage.setItem(storageKey, JSON.stringify(states));
      } catch (_) { /* Storage can be disabled; native disclosure still works. */ }
    });
  });
} catch (_) { /* Ignore unavailable or invalid stored preferences. */ }
}
bindPanels();
document.addEventListener('pypta:refreshed', bindPanels);
const boundPositionForms = new WeakSet();
const modelForms = Array.from(document.querySelectorAll('.model-selection-form'));
modelForms.forEach(form => form.addEventListener('change', () => { form.dataset.dirty = '1'; }));
let maintenanceBusy = false;
const refreshHint = document.getElementById('position-refresh-hint');
const positionFormDirty = () => Array.from(document.querySelectorAll('.position-form, .model-selection-form')).some(form => form.dataset.dirty === '1' || form.dataset.saving === '1');
function bindPositionForms() {
 document.querySelectorAll('.position-form').forEach(form => {
  if (boundPositionForms.has(form)) return;
  boundPositionForms.add(form);
  form.addEventListener('input', () => { form.dataset.dirty = '1'; if (refreshHint) refreshHint.hidden = false; });
  form.addEventListener('change', () => { form.dataset.dirty = '1'; if (refreshHint) refreshHint.hidden = false; });
  form.addEventListener('reset', () => {
    form.dataset.dirty = '0'; form.querySelector('.form-result').textContent = '';
    if (refreshHint) refreshHint.hidden = !positionFormDirty();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (maintenanceBusy) return;
    if (form.dataset.saving === '1') return;
    const result = form.querySelector('.form-result');
    if (document.body.dataset.refreshInvalidated === '1') {
      result.textContent = 'Settings or trading data changed. Copy your edits and reload before saving.';
      return;
    }
    const button = form.querySelector('button[type="submit"]');
    form.dataset.saving = '1'; button.disabled = true; result.textContent = '';
    try {
      const response = await fetch(form.action, {method:'POST', body:new FormData(form),
        headers:{Accept:'application/json'}, signal:AbortSignal.timeout(15000)});
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || 'Unable to save this position.');
      form.dataset.dirty = '0'; window.location.href = form.classList.contains('paper-alert-preferences') ? '/paper-trading' : body.position?.status === 'closed' ? '/trade-records#manual-trades' : '/positions';
    } catch (error) {
      result.textContent = error.name === 'TimeoutError' || error.name === 'TypeError'
        ? 'Save status is unknown. Retry this same form safely; duplicate submissions will not create another position.' : error.message;
    } finally { form.dataset.saving = '0'; button.disabled = false; }
  });
});
}
bindPositionForms();
document.addEventListener('pypta:refreshed', bindPositionForms);
const purgeDialog = document.getElementById('purge-dialog');
document.getElementById('open-purge')?.addEventListener('click', () => purgeDialog.showModal());
document.getElementById('cancel-purge')?.addEventListener('click', () => purgeDialog.close());
purgeDialog?.addEventListener('cancel', event => { if (maintenanceBusy) event.preventDefault(); });
document.querySelectorAll('.maintenance-form').forEach(form => {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (maintenanceBusy) return;
    const result = (form.closest('dialog') || form.closest('.strategy-controls')).querySelector('.maintenance-result');
    result.dataset.error = '0';
    if (positionFormDirty()) {
      result.dataset.error = '1';
      result.textContent = 'Save or clear the position form you are editing before changing settings or clearing the database.';
      return;
    }
    const body = new FormData(form);
    const buttons = Array.from(document.querySelectorAll('.strategy-controls button, .position-form button')).filter(button => !button.disabled);
    maintenanceBusy = true;
    buttons.forEach(button => { button.disabled = true; });
    result.textContent = 'Waiting for any current scan or alert delivery to finish, then applying your request…';
    try {
      const response = await fetch(form.action, {method:'POST', body, headers:{Accept:'application/json'}});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Unable to complete this request.');
      try {
        const states = JSON.parse(sessionStorage.getItem('adaptive-crypto-panels') || '{}');
        states['smc:rules'] = true;
        sessionStorage.setItem('adaptive-crypto-panels', JSON.stringify(states));
      } catch (_) { /* Settings were saved even if panel preferences are unavailable. */ }
      window.location.hash = 'strategy-info';
      window.location.reload();
    } catch (error) {
      result.dataset.error = '1';
      result.textContent = error.name === 'TypeError'
        ? 'The connection was interrupted. Reload the dashboard to check the result before retrying.' : error.message;
    } finally {
      maintenanceBusy = false;
      buttons.forEach(button => { button.disabled = false; });
    }
  });
});
const alertButton = document.getElementById('desktop-alerts');
const alertStatus = document.getElementById('desktop-alert-status');
const seenAlertKey = 'adaptive-crypto-holding-alerts-seen';
const desktopEnabledKey = 'adaptive-crypto-holding-alerts-enabled';
function refreshAlertLabels() {
  document.querySelectorAll('[data-structure-expires]').forEach(panel => {
    if (Number(panel.dataset.structureExpires) <= Date.now()) {
      const status = panel.querySelector('[data-structure-status]');
      if (status) status.textContent = 'LAST KNOWN · WAITING FOR CURRENT CANDLES';
    }
  });
  document.querySelectorAll('[data-market-expires]').forEach(panel => {
    if (Number(panel.dataset.marketExpires) <= Date.now()) {
      panel.querySelectorAll('[data-market-level]').forEach(level => { level.textContent = 'Pending fresh scan'; });
    }
  });
  document.querySelectorAll('[data-position-signal]').forEach(panel => {
    if (Number(panel.dataset.signalExpires) > Date.now()) return;
    const selected = panel.querySelector('.signal-light.is-selected');
    if (!selected) return;
    selected.classList.remove('is-selected');
    selected.setAttribute('aria-pressed', 'false');
    selected.querySelector('.signal-selection').textContent = 'Inactive';
    panel.querySelector('[data-position-action]').textContent = 'WAITING FOR FRESH READING';
  });
  document.querySelectorAll('[data-alert-current="1"]').forEach(label => {
    if (Number(label.dataset.alertExpires) <= Date.now()) {
      label.textContent = 'Past alert · not a current quote';
      label.dataset.alertCurrent = '0';
    }
  });
}
window.setInterval(refreshAlertLabels, 1000);
document.addEventListener('visibilitychange', refreshAlertLabels);
refreshAlertLabels();
function desktopAlertIds() {
  try { return new Set(JSON.parse(localStorage.getItem(seenAlertKey) || '[]')); } catch (_) { return new Set(); }
}
alertButton?.addEventListener('click', async () => {
  if (!('Notification' in window)) { alertStatus.textContent = 'Desktop alerts are unavailable in this browser. Dashboard and Telegram alerts remain available.'; return; }
  try {
    const permission = await Notification.requestPermission();
    if (permission === 'granted') {
      const response = await fetch('/api/state', {cache:'no-store'});
      const state = await response.json();
      localStorage.setItem(seenAlertKey, JSON.stringify((state.holdings?.outbox || []).map(event => event.id).slice(-500)));
      localStorage.setItem(desktopEnabledKey, '1');
      alertStatus.textContent = 'Desktop alerts enabled for future position alerts while this page is open. Telegram delivery continues while the dashboard server runs.';
    } else { alertStatus.textContent = 'Desktop permission was not granted. Alerts remain in the dashboard and configured Telegram chat.'; }
  } catch (_) { alertStatus.textContent = 'Could not enable desktop alerts. Alerts remain available in the dashboard.'; }
});
async function notifyHoldingAlerts() {
  try {
    if (!('Notification' in window) || Notification.permission !== 'granted' || localStorage.getItem(desktopEnabledKey) !== '1') return;
    const response = await fetch('/api/state', {cache:'no-store',signal:AbortSignal.timeout(10000)});
    if (!response.ok) return;
    const state = await response.json();
    const openIds = new Set((state.holdings?.positions || []).filter(p => p.status === 'open').map(p => p.id));
    const watchIds = new Set((state.holdings?.buy_watches || []).filter(w => ['watching', 'reached'].includes(w.status)).map(w => w.id));
    const seen = desktopAlertIds();
    for (const event of (state.holdings?.outbox || []).slice(-100)) {
      if (seen.has(event.id)) continue;
      if (!event.is_current || event.expires_ms <= Date.now() ||
          !(event.position_ids.some(id => openIds.has(id)) || (event.buy_watch_ids || []).some(id => watchIds.has(id)))) continue;
      seen.add(event.id);
      localStorage.setItem(seenAlertKey, JSON.stringify([...seen].slice(-500)));
      const label = event.alert_type === 'near_take_profit' ? 'take-profit approaching' : `${event.direction} momentum`;
      new Notification(event.title || `${event.asset}: ${label}`, {body:event.summary || event.text, tag:event.id});
    }
  } catch (_) { /* Persistent dashboard alerts remain the source of record. */ }
}
async function refreshDashboard() {
  await notifyHoldingAlerts();
  const canUpdate = () => !maintenanceBusy && !purgeDialog?.open && !document.getElementById('add-position-dialog')?.open && !positionFormDirty();
  if (window.pyptaRefresh) await window.pyptaRefresh(canUpdate);
  window.setTimeout(refreshDashboard, Math.max(5000, Number(document.body.dataset.refresh) * 1000));
}
notifyHoldingAlerts();
window.setTimeout(refreshDashboard, Math.max(5000, Number(document.body.dataset.refresh) * 1000));
const chartElements = Array.from(document.querySelectorAll('.mini-chart'));
const chartData = new Map();
function updateLivePrice(element, data) {
  const display = element.closest('.asset-head')?.querySelector('[data-live-price]');
  if (!display) return;
  const bar = data?.candles?.[data.candles.length - 1];
  const stamp = Number(data?.asof_ms);
  if (data && !data.stale && bar?.current && Number.isFinite(bar.c) && bar.c > 0 &&
      Number.isFinite(stamp) && stamp >= Number(display.dataset.asof || 0) &&
      Date.now() - stamp >= -2000 && Date.now() - stamp <= 45000) {
    display.querySelector('[data-price-value]').textContent = '$' + bar.c.toLocaleString('en-US', {
      minimumFractionDigits:Number(element.dataset.digits), maximumFractionDigits:Number(element.dataset.digits)});
    display.dataset.asof = String(stamp);
  }
  const asof = Number(display.dataset.asof);
  const known = Number.isFinite(asof) && asof > 0;
  const fresh = known && Date.now() - asof >= -2000 && Date.now() - asof <= 45000;
  display.classList.toggle('stale', known && !fresh);
  const label = display.querySelector('[data-price-status]');
  label.textContent = fresh ? 'Live price · USD' : known ? 'Last price · USD' : 'Waiting for price';
  label.title = known ? 'Observed ' + new Date(asof).toISOString() : '';
}
chartElements.forEach(element => updateLivePrice(element));
function drawCandles(element, data) {
  const canvas = element.querySelector('canvas');
  const bounds = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(bounds.width * ratio));
  canvas.height = Math.round(62 * ratio);
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.scale(ratio, ratio);
  const bars = data.candles || [];
  if (!bars.length) return;
  const low = Math.min(...bars.map(b => b.l));
  const high = Math.max(...bars.map(b => b.h));
  const spread = Math.max(high - low, Math.abs(high) * 0.00001, 0.00000001);
  const y = price => 6 + (high - price) / spread * 48;
  const step = bounds.width / bars.length;
  ctx.strokeStyle = '#303947';
  ctx.beginPath(); ctx.moveTo(0, 55); ctx.lineTo(bounds.width, 55); ctx.stroke();
  bars.forEach((bar, index) => {
    const x = (index + .5) * step;
    const width = Math.max(2, step * .56);
    const color = bar.c >= bar.o ? '#80d8b2' : '#ef9999';
    ctx.strokeStyle = color; ctx.fillStyle = color;
    ctx.lineWidth = bar.current ? 1.8 : 1;
    ctx.beginPath(); ctx.moveTo(x, y(bar.h)); ctx.lineTo(x, y(bar.l)); ctx.stroke();
    const top = y(Math.max(bar.o, bar.c));
    const height = Math.max(1.5, Math.abs(y(bar.o) - y(bar.c)));
    if (bar.current) ctx.strokeRect(x - width / 2, top, width, height);
    else ctx.fillRect(x - width / 2, top, width, height);
  });
  const last = bars[bars.length - 1];
  const fmt = value => Number(value).toLocaleString('en-US', {minimumFractionDigits:Number(element.dataset.digits),maximumFractionDigits:Number(element.dataset.digits)});
  const timeframe = data.interval_ms === 14400000 ? 'four-hour' : `${data.interval_ms / 60000}-minute`;
  const detail = `${element.dataset.asset}, ${bars.length} ${timeframe} candles. Latest: open ${fmt(last.o)}, high ${fmt(last.h)}, low ${fmt(last.l)}, close ${fmt(last.c)} USD. ${last.current ? (data.degraded ? 'Current candle uses the latest quote while OHLC retries.' : 'Final candle is still forming.') : ''}`;
  canvas.setAttribute('aria-label', detail);
  canvas.title = detail;
}
async function updateChart(element) {
  const status = element.querySelector('.chart-state');
  try {
    const response = await fetch('/api/chart/' + encodeURIComponent(element.dataset.asset), {cache:'no-store',signal:AbortSignal.timeout(15000)});
    const data = await response.json();
    if (data.candles && data.candles.length) {
      chartData.set(element, data);
      drawCandles(element, data);
    }
    if (!response.ok || data.stale || !data.candles?.length) throw new Error(data.error || 'Candle feed unavailable');
    updateLivePrice(element, data);
    element.classList.remove('stale');
    element.classList.toggle('degraded', Boolean(data.degraded));
    element.dataset.degraded = data.degraded ? '1' : '0';
    element.dataset.asof = String(data.asof_ms);
    element.dataset.end = String(data.candles[data.candles.length-1].t + data.interval_ms);
    status.textContent = data.degraded ? 'Live quote · OHLC retrying' : 'Live · 15s refresh';
    status.title = 'Updated ' + new Date(data.asof_ms).toISOString() + (data.error ? ' · ' + data.error : '');
  } catch (error) {
    element.classList.add('stale');
    status.textContent = chartData.has(element) ? 'Stale · retrying' : 'Feed unavailable';
    status.title = error.message;
  }
}
async function refreshCharts() {
  if (!document.hidden) await Promise.allSettled(chartElements.map(updateChart));
  window.setTimeout(refreshCharts, 15000);
}
refreshCharts();
window.setInterval(() => {
  document.querySelectorAll('[data-neural-expires]').forEach(element => {
    if (Date.now() > Number(element.dataset.neuralExpires)) {
      element.textContent = 'Execution window elapsed; showing last classification.';
    }
  });
  chartElements.forEach(element => {
    updateLivePrice(element);
    if (!element.dataset.asof || element.classList.contains('stale')) return;
    const remaining = Math.floor((Number(element.dataset.end) - Date.now()) / 1000);
    if (Date.now() - Number(element.dataset.asof) > 45000) {
      element.classList.add('stale');
      element.querySelector('.chart-state').textContent = 'Stale · retrying';
    } else {
      const hours = Math.floor(Math.max(0,remaining)/3600);
      const minutes = Math.floor(Math.max(0,remaining)%3600/60);
      const seconds = Math.max(0,remaining)%60;
      const label = element.dataset.degraded === '1' ? 'Live quote' : 'Live';
      element.querySelector('.chart-state').textContent = remaining > 0 ? `${label} · ${hours}:${String(minutes).padStart(2,'0')}:${String(seconds).padStart(2,'0')}` : (element.dataset.degraded === '1' ? 'Live quote · OHLC retrying' : 'Awaiting next candle');
    }
  });
}, 1000);
const chartContainer = document.querySelector('.assets');
if (chartContainer) new ResizeObserver(() => chartElements.forEach(element => {
  if (chartData.has(element)) drawCandles(element, chartData.get(element));
})).observe(chartContainer);
