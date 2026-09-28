// Reconcile server-rendered readings while retaining browser-owned UI state.
(() => {
  const preserve = 'dialog, .mini-chart, .gex, #market-brief, #desktop-alert-status, #position-refresh-hint, #refresh-status';
  function key(node) {
    if (node.nodeType !== 1) return null;
    return node.id || node.getAttribute('data-refresh-key') || node.getAttribute('data-position-id') ||
      node.getAttribute('data-panel') || node.getAttribute('data-warning-id') ||
      (node.matches('.gex, .mini-chart') ? node.classList.contains('gex') ? 'gex:' + node.dataset.asset : 'chart:' + node.dataset.asset : null) ||
      (node.hasAttribute('data-live-price') ? 'live-price' : null) ||
      (node.tagName === 'FORM' ? node.getAttribute('action') : null) ||
      (node.tagName === 'DETAILS' ? node.querySelector('summary')?.textContent : null);
  }
  function compatible(a, b) { return a.nodeType === b.nodeType && a.nodeName === b.nodeName && key(a) === key(b); }
  function sync(current, next) {
    if (current.nodeType !== 1) { if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue; return; }
    if (current.matches(preserve)) return;
    if (current.hasAttribute('data-live-price')) {
      const stamp = Number(next.dataset.asof), age = Date.now() - stamp;
      // A healthy scanner can recover a quote when the separate chart feed is
      // down. Never roll a newer chart quote back to an older server snapshot.
      if (!Number.isFinite(stamp) || stamp <= Number(current.dataset.asof || 0) || age < -2000 || age > 45000) return;
    }
    for (const attr of Array.from(current.attributes)) {
      if (attr.name === 'open' && current.tagName === 'DETAILS') continue;
      if (!next.hasAttribute(attr.name)) current.removeAttribute(attr.name);
    }
    for (const attr of next.attributes) {
      if (attr.name === 'open' && current.tagName === 'DETAILS') continue;
      if (current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name, attr.value);
    }
    let cursor = current.firstChild;
    for (const wanted of next.childNodes) {
      let found = cursor;
      while (found && !compatible(found, wanted)) found = found.nextSibling;
      if (!found) {
        found = wanted.cloneNode(true);
        current.insertBefore(found, cursor);
      } else {
        if (found !== cursor) current.insertBefore(found, cursor);
        sync(found, wanted);
      }
      cursor = found.nextSibling;
    }
    while (cursor) { const removed = cursor; cursor = cursor.nextSibling; removed.remove(); }
    // Clean forms can reflect edits saved in another tab. Dirty forms are gated
    // before reconciliation, including after the request completes.
    if (current.tagName === 'INPUT') {
      current.value = next.value;
      current.checked = next.checked;
    } else if (current.tagName === 'SELECT') current.value = next.value;
  }
  window.pyptaRefresh = async function (canUpdate) {
    if (document.hidden || !canUpdate()) return;
    const status = document.getElementById('refresh-status');
    try {
      const response = await fetch(window.location.pathname, {cache:'no-store', signal:AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error('Dashboard response ' + response.status);
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (!page.querySelector('main') || !page.body.dataset.generation) throw new Error('Invalid dashboard response');
      if (page.body.dataset.generation !== document.body.dataset.generation) {
        // This response may have rotated the session's form token. Preserve
        // edits, but never present an expired form as still saveable.
        if (!canUpdate()) {
          document.body.dataset.refreshInvalidated = '1';
          document.querySelectorAll('.position-form button[type="submit"], .model-selection-form button[type="submit"]').forEach(button => { button.disabled = true; });
          if (status) status.textContent = 'Settings or trading data changed. Copy your unsaved edits, then reload this page before saving.';
          return;
        }
        window.location.reload(); // Configuration changes invalidate forms and asset subscriptions.
        return;
      }
      // Editing may begin while a request is in flight.
      if (!canUpdate()) return;
      sync(document.querySelector('main'), page.querySelector('main'));
      document.dispatchEvent(new Event('pypta:refreshed'));
      if (status) status.textContent = '';
    } catch (_) {
      if (status) status.textContent = 'Dashboard update unavailable. Retrying automatically; timestamps continue to age.';
    }
  };
})();
