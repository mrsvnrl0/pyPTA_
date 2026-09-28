'use strict';
(() => {
  const form = document.getElementById('deribit-form');
  if (!form) return;
  const status = document.getElementById('deribit-status');
  const outcome = document.getElementById('deribit-result');
  const publicButton = document.getElementById('deribit-public');
  const discard = document.getElementById('deribit-discard');
  const controls = Array.from(form.querySelectorAll('input, button'));
  let connectionDirty = false;
  let editable = false;
  let updating = false;
  let revision = 0;
  let polling = false;
  const hasEdits = () => Array.from(form.querySelectorAll('input')).some(input => input.value.length > 0);
  const clear = () => { form.reset(); connectionDirty = false; };
  form.addEventListener('input', () => { connectionDirty = hasEdits(); });
  document.addEventListener('pypta:connection-action', event => {
    if (connectionDirty && event.target !== form) {
      event.preventDefault();
      outcome.textContent = 'Save or discard your Deribit connection edits first.';
    }
  });
  window.addEventListener('beforeunload', event => {
    if (connectionDirty) { event.preventDefault(); event.returnValue = ''; }
  });
  // Existing settings actions must preserve edits to this independent form too.
  document.addEventListener('submit', event => {
    if (event.target !== form && connectionDirty) {
      event.preventDefault();
      event.stopImmediatePropagation();
      outcome.textContent = 'Save or discard your connection edits before changing other settings.';
    }
  }, true);
  for (const id of ['open-purge', 'preview-restore']) {
    document.getElementById(id)?.addEventListener('click', event => {
      if (!connectionDirty) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      outcome.textContent = 'Save or discard your connection edits before database maintenance.';
    }, true);
  }
  discard.addEventListener('click', () => {
    if (busy) return;
    clear();
    outcome.classList.remove('danger');
    outcome.textContent = 'Unsaved connection edits discarded.';
  });
  function render(data) {
    editable = data.editable === true;
    document.getElementById('deribit-local-only').hidden = editable;
    if (!busy) controls.forEach(control => { control.disabled = !editable; });
    status.classList.toggle('danger', data.state === 'error');
    if (data.error) status.textContent = data.error;
    else if (data.authenticated) status.textContent = 'Authenticated · Deribit accepted the connection and market request.';
    else if (data.configured) status.textContent = (data.source === 'environment' ? 'Environment credentials configured. ' : 'Saved credentials configured. ') + (data.state === 'connecting' ? 'Checking authentication…' : 'Waiting for the next options request to confirm authentication.');
    else status.textContent = 'Public requests · no credentials in use.';
  }
  async function refresh() {
    if (updating || busy || polling || document.hidden) return;
    const requestedRevision = revision;
    polling = true;
    try {
      const response = await fetch(form.action, {cache: 'no-store', headers: {Accept: 'application/json'}, signal: AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error('Connection status unavailable. Reload after the dashboard has restarted.');
      const data = await response.json();
      if (requestedRevision === revision && !updating) render(data);
    } catch (error) {
      if (requestedRevision !== revision || updating) return;
      controls.forEach(control => { control.disabled = true; });
      editable = false;
      status.textContent = 'Connection status unavailable. Reload after the dashboard has restarted.';
    } finally { polling = false; }
  }
  async function change(publicOnly) {
    if (busy || !editable) return;
    if (!form.dispatchEvent(new CustomEvent('pypta:connection-action', {bubbles:true, cancelable:true}))) return;
    if (dirty) { outcome.textContent = 'Save or discard your settings edits before changing this connection.'; return; }
    const payload = {csrf_token: editor.elements.csrf_token.value};
    if (!publicOnly) {
      payload.client_id = form.elements.client_id.value;
      payload.client_secret = form.elements.client_secret.value;
    }
    revision += 1;
    updating = true;
    const unlock = lockControls();
    outcome.classList.remove('danger');
    outcome.textContent = publicOnly ? 'Enabling public requests…' : 'Saving connection…';
    try {
      const data = await post(publicOnly ? publicButton.dataset.url : form.action, payload);
      clear();
      render(data);
      outcome.textContent = data.message;
    } catch (error) { showError(outcome, error); }
    finally {
      // Clear payload references promptly; secrets never become default field values.
      delete payload.client_id;
      delete payload.client_secret;
      unlock();
      controls.forEach(control => { control.disabled = !editable; });
      updating = false;
    }
  }
  form.addEventListener('submit', event => { event.preventDefault(); change(false); });
  publicButton.addEventListener('click', () => change(true));
  refresh();
  setInterval(refresh, 5000);
})();
