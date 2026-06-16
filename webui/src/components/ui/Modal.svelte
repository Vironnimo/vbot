<script>
  // Shared modal shell. It owns the dialog semantics every modal needs — the
  // dimmed overlay, overlay-click-to-close, Escape-to-close, `role="dialog"` /
  // `aria-modal`, the header with title + close button, and moving focus into
  // the dialog on open — so each caller supplies only its own body (and an
  // optional footer) content. Caller text arrives already translated; the shell
  // only translates its own close-button label, like `Dropdown` does for its
  // placeholder.

  import { onMount } from 'svelte';

  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let {
    title = '',
    labelledById = '',
    closeDisabled = false,
    closeLabel = t('common.close', 'Close'),
    class: className = '',
    onClose = noop,
    body,
    footer,
  } = $props();

  let modalElement = $state();

  let modalClass = $derived(['modal', className].filter(Boolean).join(' '));

  function requestClose() {
    if (closeDisabled) {
      return;
    }

    onClose();
  }

  function handleDocumentKeydown(event) {
    if (event.key === 'Escape') {
      requestClose();
    }
  }

  function handleOverlayClick(event) {
    // Only a click on the backdrop itself closes; clicks inside the box bubble
    // up to the overlay but have a different target.
    if (event.target === event.currentTarget) {
      requestClose();
    }
  }

  onMount(() => {
    // Programmatic focus on the tabindex=-1 box does not trigger :focus-visible,
    // so no focus ring appears — it just lands keyboard focus inside the dialog.
    modalElement?.focus();
  });
</script>

<svelte:document onkeydown={handleDocumentKeydown} />

<div
  class="modal-overlay open"
  role="presentation"
  onclick={handleOverlayClick}
>
  <div
    bind:this={modalElement}
    class={modalClass}
    role="dialog"
    aria-modal="true"
    aria-labelledby={labelledById || undefined}
    tabindex="-1"
  >
    <div class="modal-header">
      <h3 id={labelledById || undefined} class="modal-title">{title}</h3>
      <button
        type="button"
        class="modal-close"
        aria-label={closeLabel}
        disabled={closeDisabled}
        onclick={requestClose}
      >
        ×
      </button>
    </div>

    {@render body?.()}

    {#if footer}
      <div class="modal-footer">
        {@render footer()}
      </div>
    {/if}
  </div>
</div>
