const addDialog = document.getElementById('add-position-dialog');
document.getElementById('add-position').addEventListener('click', () => addDialog.showModal());
document.getElementById('cancel-add-position').addEventListener('click', () => {
  if (addDialog.querySelector('form').dataset.saving !== '1') addDialog.close();
});
addDialog.addEventListener('cancel', event => {
  if (addDialog.querySelector('form').dataset.saving === '1') event.preventDefault();
});

document.addEventListener('click', event => {
  const button = event.target.closest('[data-suggested-stop]');
  if (!button) return;
  const input = button.closest('form').querySelector('[name="stop_price"]');
  input.value = button.dataset.suggestedStop;
  input.dispatchEvent(new Event('input', {bubbles:true}));
  input.focus();
});
