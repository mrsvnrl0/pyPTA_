'use strict';
(() => {
  const forms = Array.from(document.querySelectorAll('form[data-connection]'));
  if (!forms.length) return;
  const edits = new Set();
  const saved = {};
  let editable = false, revision = 0, polling = false;
  const result = form => form.querySelector('[data-connection-result]');
  const controls = () => forms.flatMap(form => Array.from(form.querySelectorAll('input, select, button')));
  const ai = forms.find(form => form.dataset.connection === 'ai');
  function profile() {
    const selected = saved.ai?.profiles?.[ai.elements.provider.value] || {};
    ai.elements.api_key.required = !selected.has_key;
    document.getElementById('ai-key-help').textContent = selected.has_key
      ? 'A key is available for this provider. Leave the key blank to keep it, or enter a replacement.'
      : 'Enter a key for this provider. Keys are encrypted locally and excluded from backups.';
    return selected;
  }
  function fill(form) {
    const state = saved[form.dataset.connection] || {};
    form.querySelectorAll('input[type=password]').forEach(input => { input.value = ''; });
    if (form === ai) {
      form.elements.provider.value = state.provider || 'openai';
      form.elements.model.value = profile().model || '';
    } else {
      form.elements.bot_token.required = !state.has_token;
      form.elements.chat_id.required = !state.has_chat;
    }
  }
  function render(kind, data) {
    saved[kind] = data;
    const form = forms.find(item => item.dataset.connection === kind);
    const status = document.querySelector(`[data-connection-status="${kind}"]`);
    status.classList.toggle('danger', !!data.error);
    const labels = {working:'Latest request completed.', verified:'Credentials checked.', configured:'Configured · access has not been checked.', off:'Disabled.', not_configured:'No connection configured.'};
    status.textContent = data.error || data.detail || labels[data.state] || 'Connection status unavailable.';
    if (!edits.has(form)) fill(form);
  }
  document.addEventListener('submit', event => {
    if (Array.from(edits).some(form => form !== event.target)) {
      event.preventDefault(); event.stopImmediatePropagation();
      edits.forEach(form => { result(form).textContent = 'Save or discard these connection edits before changing other settings.'; });
    }
  }, true);
  document.addEventListener('pypta:connection-action', event => {
    if (Array.from(edits).some(form => form !== event.target)) {
      event.preventDefault();
      edits.forEach(form => { result(form).textContent = 'Save or discard these connection edits first.'; });
    }
  });
  document.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button || !edits.size) return;
    if (['open-purge', 'preview-restore', 'deribit-public'].includes(button.id)) {
      event.preventDefault(); event.stopImmediatePropagation();
      edits.forEach(form => { result(form).textContent = 'Save or discard these connection edits first.'; });
    }
  }, true);
  window.addEventListener('beforeunload', event => {
    if (edits.size) { event.preventDefault(); event.returnValue = ''; }
  });
  ai.elements.provider.addEventListener('change', () => {
    // Never carry one provider's unsaved key into a request to another provider.
    ai.elements.api_key.value = '';
    ai.elements.model.value = profile().model || '';
  });
  async function refresh() {
    if (busy || polling || document.hidden) return;
    polling = true;
    const requested = revision;
    try {
      const response = await fetch('/api/settings/connections', {cache:'no-store', signal:AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (requested !== revision || busy) return;
      editable = data.editable === true;
      forms.forEach(form => render(form.dataset.connection, data[form.dataset.connection]));
      controls().forEach(control => { control.disabled = !editable; });
      document.querySelectorAll('[data-connection-local]').forEach(node => { node.hidden = editable; });
    } catch (_) {
      if (requested !== revision || busy) return;
      editable = false;
      controls().forEach(control => { control.disabled = true; });
      document.querySelectorAll('[data-connection-status]').forEach(node => { node.textContent = 'Connection status unavailable. Reload when the dashboard is ready.'; });
    } finally { polling = false; }
  }
  async function change(form, action) {
    if (busy || !editable) return;
    const outcome = result(form);
    outcome.classList.remove('danger');
    if (action === 'discard') { edits.delete(form); fill(form); outcome.textContent = 'Connection edits discarded.'; return; }
    if (!form.dispatchEvent(new CustomEvent('pypta:connection-action', {bubbles:true, cancelable:true}))) return;
    if (dirty || Array.from(edits).some(other => other !== form) || (action !== 'save' && edits.has(form))) {
      outcome.textContent = 'Save or discard your edits before checking or changing this connection.'; return;
    }
    const payload = {csrf_token:editor.elements.csrf_token.value};
    if (action === 'save') form.querySelectorAll('input, select').forEach(input => { payload[input.name] = input.value; });
    revision += 1;
    const unlock = lockControls();
    outcome.textContent = action === 'check' ? 'Checking credentials…' : 'Saving connection…';
    try {
      const data = await post(form.action + (action === 'save' ? '' : '/'+action), payload);
      edits.delete(form);
      render(form.dataset.connection, data);
      outcome.textContent = data.message;
      outcome.classList.toggle('danger', !!data.error);
    } catch (error) { showError(outcome, error); }
    finally {
      Object.keys(payload).forEach(key => { delete payload[key]; });
      unlock(); controls().forEach(control => { control.disabled = !editable; });
    }
  }
  forms.forEach(form => {
    form.addEventListener('input', () => edits.add(form));
    form.addEventListener('change', () => edits.add(form));
    form.addEventListener('submit', event => { event.preventDefault(); change(form, 'save'); });
    form.querySelectorAll('[data-connection-action]').forEach(button => button.addEventListener('click', () => change(form, button.dataset.connectionAction)));
  });
  refresh(); setInterval(refresh, 5000);
})();
