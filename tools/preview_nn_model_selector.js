#!/usr/bin/env node
/**
 * Isolated, repeatable desktop/mobile smoke check for the NN model selector.
 *
 * Run `node tools/preview_nn_model_selector.js`. It starts a Flask server on an
 * ephemeral localhost port with settings and JSON ledgers in a temporary
 * directory. No market scanners, background workers, or saved credentials run.
 * Screenshots and a compact JSON report go under `.qa/candidate-preview/`.
 * If a validated candidate is installed, the script exercises Save -> Apply
 * against this disposable server; otherwise it verifies unavailable options.
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');

const repo = path.resolve(__dirname, '..');
const playwrightRoot = 'C:/Users/CxN/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright';
let chromium;
try { ({chromium} = require('playwright')); }
catch (_) { ({chromium} = require(playwrightRoot)); }

const python = process.env.PYPTA_PYTHON || path.join(repo, '.venv', 'Scripts', 'python.exe');
const chrome = process.env.PYPTA_BROWSER_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const reportDir = path.join(repo, '.qa', 'candidate-preview');
const runId = new Date().toISOString().replace(/[:.]/g, '-');
const pythonServer = `
import sys
from pathlib import Path
from werkzeug.serving import make_server
from adaptive_crypto.core import load_application_settings
from adaptive_crypto.state_paths import open_stores
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.web import create_app

settings, state, port = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
assets, rules, refresh = load_application_settings(settings)
with open_stores(state, assets, rules, "json", settings_path=settings) as (paper, positions):
    runtime = DashboardRuntime(assets, rules, paper, refresh, position_store=positions, settings_path=settings)
    runtime.state_base = state
    server = make_server("127.0.0.1", port, create_app(runtime), threaded=True)
    print("PREVIEW_READY", flush=True)
    server.serve_forever()
`;

function wait(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(error => error ? reject(error) : resolve(port));
    });
  });
}

function getJSON(url) {
  return new Promise((resolve, reject) => {
    http.get(url, response => {
      let raw = '';
      response.setEncoding('utf8');
      response.on('data', data => { raw += data; });
      response.on('end', () => {
        try { resolve({status: response.statusCode, body: JSON.parse(raw)}); }
        catch (error) { reject(error); }
      });
    }).on('error', reject);
  });
}

async function startServer(settings, state, port) {
  const permitted = ['Path', 'PATH', 'SystemRoot', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA'];
  const env = Object.fromEntries(permitted.filter(name => process.env[name]).map(name => [name, process.env[name]]));
  env.PYTHONUNBUFFERED = '1';
  const child = spawn(python, ['-u', '-c', pythonServer, settings, state, String(port)],
    {cwd: repo, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe']});
  let output = '';
  child.stdout.on('data', value => { output += String(value); });
  child.stderr.on('data', value => { output += String(value); });
  for (let attempt = 0; attempt < 100; attempt++) {
    if (output.includes('PREVIEW_READY')) return child;
    if (child.exitCode !== null) throw new Error(`Preview server exited ${child.exitCode}: ${output.slice(-2500)}`);
    await wait(100);
  }
  child.kill();
  throw new Error(`Preview server did not start: ${output.slice(-2500)}`);
}

function isolateFeeds(page, base) {
  // Market fetches are unrelated to this UI check. Keep the preview offline.
  page.route(`${base}/api/chart/**`, route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({candles: [], stale: true, error: 'Market feed omitted in isolated preview'})}));
  page.route(`${base}/api/gex/**`, route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({status: 'unavailable', error: 'Options feed omitted in isolated preview'})}));
  page.route(`${base}/favicon.ico`, route => route.fulfill({status: 200, body: ''}));
}

async function inspectPage(browser, base, pathname, viewport, expected, imagePath) {
  const context = await browser.newContext({viewport, deviceScaleFactor: 1});
  const page = await context.newPage();
  isolateFeeds(page, base);
  const errors = [];
  page.on('pageerror', error => errors.push(`page: ${error.message}`));
  page.on('console', message => { if (message.type() === 'error') errors.push(`console: ${message.text()}`); });
  try {
    const response = await page.goto(base + pathname, {waitUntil: 'networkidle', timeout: 20000});
    assert.equal(response.status(), 200, `${pathname}: HTTP status`);
    await page.screenshot({path: imagePath, fullPage: true});
    let modelPickerScreenshot;
    if (pathname === '/settings') {
      modelPickerScreenshot = imagePath.replace(/-settings\.png$/, '-model-picker.png');
      await page.locator('.nn-model-picker').screenshot({path: modelPickerScreenshot});
    }
    const overflow = await page.evaluate(() => ({width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
      offenders: Array.from(document.querySelectorAll('body *')).filter(el => {
        const rect = el.getBoundingClientRect();
        return rect.width && rect.right > innerWidth + 4;
      }).slice(0, 5).map(el => `${el.tagName.toLowerCase()}#${el.id}.${el.className}`)}));
    assert.ok(overflow.scrollWidth <= overflow.width + 4,
      `${pathname} ${viewport.width}px overflow: ${JSON.stringify(overflow)}`);
    assert.deepEqual(errors, [], `${pathname} ${viewport.width}px console/page errors`);
    if (pathname !== '/settings') {
      const model = page.locator('[data-applied-nn-model]');
      assert.equal(await model.count(), 1, `${pathname}: applied-model badge`);
      assert.equal(await model.getAttribute('data-applied-nn-model'), expected.artifact_id);
      assert.match(await model.textContent(), new RegExp(expected.display_name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
    }
    return {page: pathname, viewport: viewport.width, overflow, screenshot: imagePath,
      ...(modelPickerScreenshot ? {model_picker_screenshot: modelPickerScreenshot} : {})};
  } finally { await context.close(); }
}

async function main() {
  assert.ok(fs.existsSync(python), `Python not found: ${python}`);
  assert.ok(fs.existsSync(chrome), `Browser not found: ${chrome}`);
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'pypta-nn-preview-'));
  const settingsPath = path.join(temp, 'settings.json');
  const statePath = path.join(temp, 'state.json');
  const document = JSON.parse(fs.readFileSync(path.join(repo, 'adaptive_crypto_settings.example.json'), 'utf8'));
  document.strategy.nn_limitations = false;
  document.strategy.nn_model_id = 'parente_mlp_v1';
  document.strategy.nn_model_path = '';
  document.strategy.nn_model_bundle_path = '';
  document.refresh_seconds = 300;
  fs.writeFileSync(settingsPath, JSON.stringify(document, null, 2));
  fs.mkdirSync(reportDir, {recursive: true});
  let server, browser;
  try {
    let port = await freePort();
    if (port === 5000) port = await freePort();
    assert.notEqual(port, 5000, 'Never use the live dashboard port');
    server = await startServer(settingsPath, statePath, port);
    const base = `http://127.0.0.1:${port}`;
    const initial = await getJSON(base + '/api/state');
    assert.equal(initial.status, 200);
    assert.equal(initial.body.rules.nn_model_id, 'parente_mlp_v1');
    assert.equal(initial.body.rules.nn_limitations, false);
    assert.ok(initial.body.neural_model?.artifact_id);
    browser = await chromium.launch({headless: true, executablePath: chrome, args: ['--no-sandbox']});
    const context = await browser.newContext({viewport: {width: 1280, height: 900}});
    const page = await context.newPage();
    isolateFeeds(page, base);
    const errors = [];
    page.on('pageerror', error => errors.push(`page: ${error.message}`));
    page.on('console', message => { if (message.type() === 'error') errors.push(`console: ${message.text()}`); });
    const settingsResponse = await page.goto(base + '/settings', {waitUntil: 'networkidle'});
    assert.equal(settingsResponse.status(), 200);
    const select = page.locator('#nn-model-id');
    assert.equal(await select.inputValue(), 'parente_mlp_v1');
    assert.equal(await page.locator('[data-rule="nn_limitations"]').isChecked(), false);
    let catalog = JSON.parse(await page.locator('#nn-model-catalog').textContent());
    const available = catalog.filter(entry => entry.id !== 'parente_mlp_v1' && entry.available);
    const unavailable = catalog.filter(entry => entry.id !== 'parente_mlp_v1' && !entry.available);
    for (const entry of unavailable) {
      if (entry.id === await select.inputValue()) continue;
      assert.equal(await select.locator(`option[value="${entry.id}"]`).isDisabled(), true,
        `${entry.id}: unavailable candidate must be disabled`);
    }
    const result = {run_id: runId, isolated_port: port, initial_model_id: 'parente_mlp_v1',
      available_candidates: available.map(entry => entry.id), unavailable_candidates: unavailable.map(entry => entry.id),
      views: [], applied_model_id: 'parente_mlp_v1', applied_sequence: []};
    for (const candidate of available) {
      await select.selectOption(candidate.id);
      await page.waitForFunction(id => {
        const selected = document.querySelector('#nn-model-id');
        return selected?.value === id &&
          document.querySelector('#nn-model-state')?.textContent?.startsWith('Selected:');
      }, candidate.id, {timeout: 10000});
      assert.match(await page.locator('#nn-model-state').textContent(), /Selected:.*Saved:.*Applied:/);
      assert.match(await page.locator('#nn-model-coverage').textContent(), /Asset coverage:/);
      const reportLink = page.locator('#nn-model-report');
      assert.equal(await reportLink.isVisible(), true, `${candidate.id}: report link visible`);
      const reportResponse = await page.request.get(base + await reportLink.getAttribute('href'));
      assert.equal(reportResponse.status(), 200, `${candidate.id}: report accessible`);
      await page.locator('#settings-form button[type="submit"]').click();
      await page.waitForFunction(() => document.querySelector('#settings-result')?.textContent?.includes('Settings saved.'), null,
        {timeout: 20000});
      const savedState = await getJSON(base + '/api/state');
      assert.equal(savedState.body.rules.nn_model_id, result.applied_model_id, 'Save must not apply');
      assert.equal(savedState.body.rules.nn_limitations, false);
      assert.match(await page.locator('#nn-model-state').textContent(), /Apply saved settings/);
      await page.locator('form[action="/api/settings/apply"] button[type="submit"]').click();
      await page.waitForURL(/\/settings(?:#.*)?$/, {timeout: 30000});
      await page.waitForFunction(id => document.querySelector('#nn-model-id')?.dataset.appliedModelId === id,
        candidate.id, {timeout: 30000});
      result.applied_model_id = candidate.id;
      result.applied_sequence.push(candidate.id);
    }
    assert.deepEqual(errors, [], 'Settings console/page errors');
    await context.close();
    const applied = await getJSON(base + '/api/state');
    assert.equal(applied.status, 200);
    assert.equal(applied.body.rules.nn_model_id, result.applied_model_id);
    assert.equal(applied.body.rules.nn_limitations, false, 'NN limitations changed during model switch');
    assert.ok(applied.body.neural_model?.artifact_id);
    assert.ok(applied.body.neural_model?.display_name);
    const expected = applied.body.neural_model;
    for (const viewport of [{width: 1280, height: 900}, {width: 390, height: 844}]) {
      for (const pathname of ['/', '/paper-trading', '/positions', '/settings']) {
        const safePage = pathname === '/' ? 'home' : pathname.slice(1);
        const image = path.join(reportDir, `${runId}-${viewport.width}-${safePage}.png`);
        result.views.push(await inspectPage(browser, base, pathname, viewport, expected, image));
      }
    }
    const finalState = await getJSON(base + '/api/state');
    assert.equal(finalState.body.neural_model.artifact_id, expected.artifact_id);
    result.applied_artifact_id = expected.artifact_id;
    result.applied_display_name = expected.display_name;
    const reportPath = path.join(reportDir, `${runId}-report.json`);
    fs.writeFileSync(reportPath, JSON.stringify(result, null, 2));
    console.log(JSON.stringify({status: 'passed', report: reportPath, ...result}, null, 2));
  } finally {
    if (browser) await browser.close();
    if (server && server.exitCode === null) {
      server.kill();
      await Promise.race([new Promise(resolve => server.once('exit', resolve)), wait(5000)]);
    }
    const root = path.resolve(os.tmpdir()) + path.sep;
    const resolved = path.resolve(temp);
    if (!resolved.startsWith(root) || !path.basename(resolved).startsWith('pypta-nn-preview-')) {
      throw new Error('Refusing to remove a preview directory outside the dedicated temporary root');
    }
    // Windows can retain short-lived handles to the JSON ledger after the
    // preview server exits. Node's bounded retries keep cleanup reliable.
    fs.rmSync(resolved, {recursive: true, force: true, maxRetries: 20, retryDelay: 250});
  }
}

main().catch(error => { console.error(error.stack || String(error)); process.exitCode = 1; });
