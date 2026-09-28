(function () {
  'use strict';

  function effectiveState(row, now, disconnected) {
    if (row.status !== 'ready') return row.status || 'unknown';
    if (disconnected) return 'disconnected';
    if (!Number.isFinite(row.valid_until_ms) || now >= row.valid_until_ms) return 'stale';
    return 'ready';
  }
  function eligibility(row, now) {
    if (row.refreshing) return 'Refresh in progress';
    if (Number.isFinite(row.next_eligible_ms)) {
      if (now >= row.next_eligible_ms) return 'Eligible now; refresh starts on a GEX request or eligible scan';
      return 'Refresh eligible in ' + Math.ceil((row.next_eligible_ms - now) / 1000) + 's';
    }
    return row.next_check || 'No retry time recorded';
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {effectiveState, eligibility};
  if (typeof document === 'undefined') return;
  const panel = document.querySelector('[data-feed-status]');
  if (!panel) return;
  const summary = panel.querySelector('.feed-status-summary');
  const container = panel.querySelector('.feed-status-assets');
  const labels = {ready: 'Current', unavailable: 'Unavailable', unknown: 'Not observed',
    stale: 'Stale', unsupported: 'Unsupported', disconnected: 'Unverified'};
  let payload = null, receivedAt = 0, disconnected = false, inFlight = false;
  let views = [];
  const clock = () => performance.now();
  const time = value => Number.isFinite(value) ? new Date(value).toLocaleString() : 'Not recorded';
  const element = (tag, className, text) => {
    const result = document.createElement(tag);
    result.className = className || '';
    if (text !== undefined) result.textContent = text;
    return result;
  };
  function render() {
    if (!payload) return;
    const now = payload.generated_ms + Math.max(0, clock() - receivedAt);
    summary.textContent = (disconnected ? 'Diagnostics connection lost. Last observations shown. ' : '') +
      'Last completed scan: ' + time(payload.last_scan_ms) +
      (payload.scan_error ? ' · Scanner error: ' + payload.scan_error : '') +
      (payload.paper_paused ? ' · Paper simulation paused' : '');
    const fragment = document.createDocumentFragment();
    views = [];
    payload.assets.forEach(asset => {
      const section = element('div', 'feed-status-asset');
      section.append(element('h3', '', asset.name + ' · ' + asset.symbol));
      const grid = element('div', 'feed-status-grid');
      asset.feeds.forEach(row => {
        const state = effectiveState(row, now, disconnected);
        const item = element('div', 'feed-status-item');
        item.dataset.state = state;
        const heading = element('div', 'feed-status-heading');
        const stateLabel = element('span', 'feed-status-state', labels[state] || state);
        heading.append(element('strong', '', row.label), stateLabel);
        item.append(heading, element('p', 'muted', 'Last success: ' + time(row.last_success_ms)));
        const problem = row.error || (state === 'stale' ? 'Observation aged; waiting for a fresh update' :
          state === 'unknown' ? 'No completed observation yet' : '');
        const problemLabel = element('p', '', problem);
        problemLabel.hidden = !problem;
        const retryLabel = element('p', 'muted', eligibility(row, now));
        item.append(problemLabel, retryLabel);
        views.push({row, item, stateLabel, problemLabel, retryLabel});
        if (row.last_failure && !row.error) {
          const details = element('details', '');
          details.append(element('summary', '', 'Previous failure'),
            element('p', '', time(row.last_failure_ms) + ' · ' + row.last_failure));
          item.append(details);
        }
        grid.append(item);
      });
      section.append(grid);
      fragment.append(section);
    });
    // Preserve expanded failure details across age updates.
    const opened = Array.from(container.querySelectorAll('details')).map((el, index) => el.open ? index : -1);
    container.replaceChildren(fragment);
    container.querySelectorAll('details').forEach((el, index) => { el.open = opened.includes(index); });
  }
  function age() {
    if (!payload) return;
    const now = payload.generated_ms + Math.max(0, clock() - receivedAt);
    views.forEach(({row, item, stateLabel, problemLabel, retryLabel}) => {
      const state = effectiveState(row, now, disconnected);
      item.dataset.state = state;
      const stateText = labels[state] || state;
      if (stateLabel.textContent !== stateText) stateLabel.textContent = stateText;
      const problem = row.error || (state === 'stale' ? 'Observation aged; waiting for a fresh update' :
        state === 'unknown' ? 'No completed observation yet' : '');
      if (problemLabel.textContent !== problem) problemLabel.textContent = problem;
      problemLabel.hidden = !problem;
      const retry = eligibility(row, now);
      if (retryLabel.textContent !== retry) retryLabel.textContent = retry;
    });
  }
  async function refresh() {
    if (inFlight || document.hidden) return;
    inFlight = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(panel.dataset.feedUrl, {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const next = await response.json();
      if (!Number.isFinite(next.generated_ms) || !Array.isArray(next.assets)) throw new Error('Invalid diagnostics response');
      payload = next;
      receivedAt = clock();
      disconnected = false;
    } catch (error) {
      disconnected = true;
      if (!payload) summary.textContent = 'Feed diagnostics unavailable: ' + error.message + '. Retrying in 15s.';
    } finally {
      clearTimeout(timeout);
      inFlight = false;
      render();
    }
  }
  refresh();
  setInterval(refresh, 15000);
  setInterval(age, 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
}());
