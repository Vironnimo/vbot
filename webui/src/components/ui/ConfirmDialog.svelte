<script>
  // Shared confirm dialog. It builds on the `Modal` shell — which already owns
  // the overlay, Escape/×, focus, and aria — and adds only the confirm/cancel
  // decision every destructive action needs. Callers own the open state
  // (conditionally render this dialog), pass already-translated copy, and route
  // both close paths through `onCancel` so overlay-click, Escape, and × all
  // resolve as a cancel. The confirm button carries the action verb and renders
  // in the `danger` variant for irreversible actions.

  import Button from './Button.svelte';
  import Modal from './Modal.svelte';

  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let {
    title,
    // The public prop is `body`; it is aliased here because the Modal shell
    // expects a snippet literally named `body`, and that snippet name would
    // otherwise shadow the prop.
    body: bodyText,
    confirmLabel,
    cancelLabel = t('common.cancel', 'Cancel'),
    danger = true,
    onConfirm = noop,
    onCancel = noop,
  } = $props();

  // Ties the dialog title to the Modal's aria-labelledby so screen readers
  // announce the confirm heading, mirroring how the other modal callers wire it.
  const titleId = 'confirm-dialog-title';

  let confirmVariant = $derived(danger ? 'danger' : 'primary');
</script>

<Modal {title} labelledById={titleId} onClose={onCancel}>
  {#snippet body()}
    <div class="modal-body">
      <p>{bodyText}</p>
    </div>
  {/snippet}

  {#snippet footer()}
    <Button variant="secondary" onClick={onCancel}>{cancelLabel}</Button>
    <Button variant={confirmVariant} onClick={onConfirm}>{confirmLabel}</Button>
  {/snippet}
</Modal>
