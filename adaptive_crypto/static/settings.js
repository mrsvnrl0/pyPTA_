'use strict';
const editor = document.getElementById('settings-form');
const result = document.getElementById('settings-result');
const unsaved = document.getElementById('unsaved-status');
const modelSelect = document.getElementById('nn-model-id');
let modelCatalog = [];
try { modelCatalog = JSON.parse(document.getElementById('nn-model-catalog')?.textContent || '[]'); }
catch (_) { modelCatalog = []; }
if (!Array.isArray(modelCatalog)) modelCatalog = [];
const modelEntry = id => modelCatalog.find(entry => entry && entry.id === id);
const modelName = id => modelEntry(id)?.label || id;
function modelReportHref(value) {
  if (typeof value !== 'string' || !value.startsWith('/')) return null;
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin && ['http:', 'https:'].includes(url.protocol)
      ? url.pathname + url.search + url.hash : null;
  } catch (_) { return null; }
}
function modelCoverage(value) {
  if (Array.isArray(value)) return value.length ? value.join(', ') : 'No assets reported';
  if (value && typeof value === 'object') {
    const entries = Object.entries(value);
    return entries.length ? entries.map(([asset, status]) => {
      if (typeof status === 'boolean') return status ? asset : `${asset} unavailable`;
      if (status && typeof status === 'object') return `${asset}: ${status.available === false ? 'unavailable' : status.status || 'supported'}`;
      return `${asset}: ${status}`;
    }).join(', ') : 'No assets reported';
  }
  return typeof value === 'string' && value ? value : 'Not reported';
}
function renderModelDetails() {
  if (!modelSelect) return;
  const selectedId = modelSelect.value;
  const candidate = modelEntry(selectedId);
  const savedId = modelSelect.dataset.savedModelId || selectedId;
  const appliedId = modelSelect.dataset.appliedModelId || savedId;
  document.getElementById('nn-model-state').textContent = `Selected: ${modelName(selectedId)} · Saved: ${modelName(savedId)} · Applied: ${modelName(appliedId)}`
    + (savedId !== appliedId ? ' · Apply saved settings to activate the saved model.' : '');
  const availabilityStatus = candidate?.status || (candidate?.available ? 'Trained and validated' : 'Unavailable');
  const availabilityReason = typeof candidate?.reason === 'string' ? candidate.reason.trim() : '';
  document.getElementById('nn-model-availability').textContent = candidate
    ? `${availabilityStatus}${availabilityReason && availabilityReason.toLowerCase() !== availabilityStatus.toLowerCase()
      ? ` · ${availabilityReason}` : ''}`
    : 'Registry status unavailable for this model.';
  document.getElementById('nn-model-coverage').textContent = `Asset coverage: ${modelCoverage(candidate?.coverage)}`;
  const stamp = value => {
    const ms = Number(value);
    const date = new Date(ms);
    return Number.isFinite(ms) && ms > 0 && Number.isFinite(date.getTime())
      ? date.toISOString().replace('Z', ' UTC') : 'not reported';
  };
  const horizon = candidate?.target_horizon ? ` · Target horizon: ${candidate.target_horizon} completed 4H bars` : '';
  const artifact = candidate?.artifact_id ? ` · Artifact: ${candidate.artifact_id}` : '';
  const eligibility = candidate?.live_eligibility_after_ms != null
    ? ` · Live eligible after: ${stamp(candidate.live_eligibility_after_ms)}` : '';
  document.getElementById('nn-model-provenance').textContent = `Training/calibration through: ${stamp(candidate?.trained_through_ms)}${eligibility}${horizon}${artifact}`;
  const report = document.getElementById('nn-model-report');
  const href = modelReportHref(candidate?.report_url);
  report.hidden = !href;
  if (href) report.href = href;
  else report.removeAttribute('href');
}
modelSelect?.addEventListener('change', renderModelDetails);
renderModelDetails();
let dirty = false;
let busy = false;
function markDirty(value = true) {
  dirty = value;
  unsaved.textContent = value ? 'Unsaved edits' : 'No unsaved edits';
}
editor.addEventListener('input', () => markDirty());
editor.addEventListener('change', () => markDirty());
window.addEventListener('beforeunload', event => {
  if (dirty || busy) { event.preventDefault(); event.returnValue = ''; }
});
const rows = document.getElementById('asset-rows');
let savedRows = rows.innerHTML;
document.getElementById('add-asset').addEventListener('click', () => {
  const row = rows.firstElementChild.cloneNode(true);
  row.querySelectorAll('input').forEach(input => { input.value = input.dataset.assetField === 'price_decimals' ? '3' : ''; });
  row.querySelector('select').value = 'true';
  row.querySelector('button').setAttribute('aria-label', 'Remove new currency pair');
  rows.append(row);
  row.querySelector('input').focus();
  markDirty();
});
rows.addEventListener('click', event => {
  const button = event.target.closest('.remove-asset');
  if (!button) return;
  if (rows.children.length === 1) { result.textContent = 'Keep at least one currency pair.'; return; }
  button.closest('.asset-settings-row').remove();
  markDirty();
});
editor.addEventListener('reset', () => {
  rows.innerHTML = savedRows;
  markDirty(false);
  result.textContent = 'Unsaved edits discarded.';
  // The browser restores select values after dispatching the reset event.
  setTimeout(renderModelDetails, 0);
});
function readValue(input) {
  if (input.type === 'checkbox') return input.checked;
  if (input.dataset.kind === 'bool' || input.dataset.assetField === 'enabled') return input.value === 'true';
  if (['int', 'number'].includes(input.dataset.kind) || input.dataset.assetField === 'price_decimals') return Number(input.value);
  return input.value;
}
function settingsDocument() {
  return {
    assets: Array.from(rows.children, row => Object.fromEntries(Array.from(row.querySelectorAll('[data-asset-field]'), input => [input.dataset.assetField, readValue(input)]))),
    refresh_seconds: Number(document.getElementById('refresh-seconds').value),
    strategy: {...JSON.parse(document.getElementById('saved-strategy').textContent), ...Object.fromEntries(Array.from(editor.querySelectorAll('[data-rule]'), input => [input.dataset.rule, readValue(input)])), strategy_model:'neural_network'},
  };
}
function lockControls() {
  busy = true;
  const controls = Array.from(document.querySelectorAll('input, select, button')).filter(control => !control.disabled);
  controls.forEach(control => { control.disabled = true; });
  return () => { busy = false; controls.forEach(control => { control.disabled = false; }); };
}
async function post(url, body) {
  const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', Accept: 'application/json'}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Unable to complete this request.');
  return data;
}
function showError(target, error) {
  target.classList.add('danger');
  target.textContent = error.name === 'TypeError' ? 'Connection interrupted. Reload to check whether the request completed before retrying.' : error.message;
}
editor.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  const payload = {settings: settingsDocument(), revision: editor.dataset.revision, csrf_token: editor.elements.csrf_token.value};
  const unlock = lockControls();
  result.classList.remove('danger');
  result.textContent = 'Validating and saving settings…';
  try {
    const data = await post(editor.action, payload);
    editor.dataset.revision = data.revision;
    editor.querySelectorAll('input').forEach(input => { input.defaultValue = input.value; if (input.type === 'checkbox') input.defaultChecked = input.checked; });
    editor.querySelectorAll('select').forEach(select => Array.from(select.options).forEach(option => { option.defaultSelected = option.selected; }));
    if (modelSelect) modelSelect.dataset.savedModelId = modelSelect.value;
    renderModelDetails();
    // Capture the enabled controls after unlocking, so discard never restores disabled fields.
    markDirty(false);
    result.textContent = data.message;
  } catch (error) { showError(result, error); }
  finally { unlock(); if (!dirty) savedRows = rows.innerHTML; }
});
const purgeDialog = document.getElementById('purge-dialog');
document.getElementById('open-purge')?.addEventListener('click', () => {
  if (dirty) { result.textContent = 'Save or discard your edits before clearing the database.'; return; }
  purgeDialog.showModal();
});
document.getElementById('cancel-purge')?.addEventListener('click', () => purgeDialog.close());
purgeDialog?.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
document.querySelectorAll('.maintenance-form').forEach(form => form.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  const status = (form.closest('dialog') || form.closest('.strategy-controls')).querySelector('.maintenance-result');
  status.classList.remove('danger');
  if (dirty) { status.textContent = 'Save or discard your edits before applying settings.'; return; }
  const payload = Object.fromEntries(new FormData(form));
  const unlock = lockControls();
  status.textContent = 'Waiting for the current scan or alert delivery to finish…';
  try {
    await post(form.action, payload);
    unlock();
    window.location.href = '/settings#settings-database';
    window.location.reload();
  } catch (error) { showError(status, error); }
  finally { unlock(); }
}));
document.getElementById('restart-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  const status = document.getElementById('restart-result');
  status.classList.remove('danger');
  if (dirty) { status.textContent = 'Save or discard your edits before restarting.'; return; }
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form));
  const unlock = lockControls();
  status.textContent = 'Checking saved settings and waiting for scans and deliveries to finish…';
  try {
    const data = await post(form.action, payload);
    status.textContent = 'Restarting dashboard. This page will reconnect automatically…';
    for (let attempt = 0; attempt < 120; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      try {
        const response = await fetch('/api/dashboard/status', {cache: 'no-store', signal: AbortSignal.timeout(2000)});
        const current = await response.json();
        if (response.ok && !current.restarting && current.generation !== data.generation) {
          unlock();
          try { sessionStorage.setItem('dashboard-restarted', '1'); } catch (_) { /* Storage is optional. */ }
          window.location.reload();
          return;
        }
      } catch (_) { /* The listener closes briefly during restart. */ }
    }
    throw new Error('The dashboard has not reconnected yet. Check the server log, then reload this page.');
  } catch (error) { showError(status, error); }
  finally { unlock(); }
});
try {
  if (sessionStorage.getItem('dashboard-restarted')) {
    document.getElementById('restart-result').textContent = 'Dashboard restarted successfully.';
    sessionStorage.removeItem('dashboard-restarted');
  }
} catch (_) { /* Storage is optional. */ }
