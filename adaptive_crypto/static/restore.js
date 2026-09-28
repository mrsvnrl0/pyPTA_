'use strict';
// Settings' shared control lock prevents overlapping edits and maintenance.
(() => {
  const preview = document.getElementById('preview-restore');
  const dialog = document.getElementById('restore-dialog');
  const form = document.getElementById('confirm-restore-form');
  const status = document.getElementById('restore-result');
  const confirmation = document.getElementById('restore-confirm-result');
  if (!preview || !dialog || !form) return;
  async function restoreRequest(url, body) {
    const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', Accept: 'application/json'}, body: JSON.stringify(body)});
    if (response.status === 404) throw new Error('Restore controls require the updated dashboard. Restart it and try again.');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Unable to complete this restore request.');
    return data;
  }
  document.getElementById('cancel-restore').addEventListener('click', () => { if (!busy) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  preview.addEventListener('click', async () => {
    if (busy) return;
    status.classList.remove('danger');
    if (dirty) { status.textContent = 'Save or discard your edits before reviewing a backup.'; return; }
    const unlock = lockControls();
    status.textContent = 'Verifying the backup, saved settings and complete trading history…';
    try {
      const data = await restoreRequest(preview.dataset.url, {csrf_token: editor.elements.csrf_token.value});
      form.elements.token.value = data.token;
      document.getElementById('restore-date').textContent = `Backup created ${new Date(data.created_ms).toLocaleString()} · ${data.action}`;
      const counts = document.getElementById('restore-counts');
      counts.replaceChildren();
      for (const [name, records] of Object.entries(data.records)) {
        const line = document.createElement('p');
        line.textContent = `${name}: ${records.paper_trades} paper trades (${records.open_paper_trades} open), ${records.positions} positions, ${records.buy_watches} buy watches, ${records.queued_alerts} queued alerts to cancel.`;
        counts.appendChild(line);
      }
      document.getElementById('restore-settings').textContent = JSON.stringify(data.settings, null, 2);
      document.getElementById('restore-settings-note').textContent = data.saved_settings_differ
        ? 'This backup contains saved edits that were not active. Restore keeps the archived active settings; use Apply saved settings afterward to activate those edits.'
        : `Active paper strategy: ${data.active_strategy}. The preview expires in 10 minutes.`;
      confirmation.textContent = '';
      confirmation.classList.remove('danger');
      status.textContent = 'Backup verified. Review the settings and counts before confirming.';
      unlock();
      dialog.showModal();
    } catch (error) { showError(status, error); }
    finally { unlock(); }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    const payload = Object.fromEntries(new FormData(form));
    const unlock = lockControls();
    confirmation.classList.remove('danger');
    confirmation.textContent = 'Waiting for scans and alert deliveries, then backing up and restoring the study…';
    try {
      const data = await restoreRequest(form.action, payload);
      unlock();
      window.location.replace('/settings?restored=' + data.at_ms + '#restore-controls');
    } catch (error) { showError(confirmation, error); }
    finally { unlock(); }
  });
})();
