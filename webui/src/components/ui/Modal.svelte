<script>
  // Shared modal shell. It owns the dialog semantics every modal needs — the
  // dimmed overlay, overlay-click-to-close, Escape-to-close, `role="dialog"` /
  // `aria-modal`, the header with title + close button, and moving focus into
  // the dialog on open — so each caller supplies only its own body (and an
  // optional footer) content. Caller text arrives already translated; the shell
  // only translates its own close-button label, like `Dropdown` does for its
  // placeholder.

  import { onDestroy, onMount } from 'svelte';

  import { t } from '$lib/i18n.js';

  const noop = () => {};
  const FOCUSABLE_SELECTOR = [
    'a[href]',
    'button:not([disabled])',
    'input:not([disabled]):not([type="hidden"])',
    'select:not([disabled])',
    'textarea:not([disabled])',
    '[tabindex]:not([tabindex="-1"])',
  ].join(',');

  const componentId = $props.id();

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
  let overlayElement = $state();
  let previouslyFocusedElement;
  let restoredInertElements = [];

  let modalClass = $derived(['modal', className].filter(Boolean).join(' '));
  let titleId = $derived(labelledById || `${componentId}-title`);

  function requestClose() {
    if (closeDisabled) {
      return;
    }

    onClose();
  }

  function handleDocumentKeydown(event) {
    if (event.key === 'Escape') {
      requestClose();
      return;
    }

    if (event.key !== 'Tab' || !modalElement) {
      return;
    }

    const focusableElements = Array.from(
      modalElement.querySelectorAll(FOCUSABLE_SELECTOR),
    );
    if (focusableElements.length === 0) {
      event.preventDefault();
      modalElement.focus();
      return;
    }

    const firstElement = focusableElements[0];
    const lastElement = focusableElements.at(-1);
    const activeElement = document.activeElement;
    if (
      event.shiftKey &&
      (activeElement === modalElement ||
        activeElement === firstElement ||
        !modalElement.contains(activeElement))
    ) {
      event.preventDefault();
      lastElement.focus();
    } else if (
      !event.shiftKey &&
      (activeElement === modalElement ||
        activeElement === lastElement ||
        !modalElement.contains(activeElement))
    ) {
      event.preventDefault();
      firstElement.focus();
    }
  }

  function isolateBackground() {
    let activeBranch = overlayElement;
    while (activeBranch?.parentElement) {
      const parent = activeBranch.parentElement;
      for (const sibling of parent.children) {
        if (sibling === activeBranch || sibling.inert) {
          continue;
        }
        sibling.inert = true;
        restoredInertElements.push(sibling);
      }
      activeBranch = parent;
    }
  }

  function restoreBackground() {
    for (const element of restoredInertElements) {
      element.inert = false;
    }
    restoredInertElements = [];
  }

  function handleOverlayClick(event) {
    // Only a click on the backdrop itself closes; clicks inside the box bubble
    // up to the overlay but have a different target.
    if (event.target === event.currentTarget) {
      requestClose();
    }
  }

  onMount(() => {
    previouslyFocusedElement =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : undefined;
    isolateBackground();
    // Programmatic focus on the tabindex=-1 box does not trigger :focus-visible,
    // so no focus ring appears — it just lands keyboard focus inside the dialog.
    modalElement?.focus();
  });

  onDestroy(() => {
    restoreBackground();
    if (
      previouslyFocusedElement?.isConnected &&
      typeof previouslyFocusedElement.focus === 'function'
    ) {
      previouslyFocusedElement.focus();
    }
  });
</script>

<svelte:document onkeydown={handleDocumentKeydown} />

<div
  bind:this={overlayElement}
  class="modal-overlay open"
  role="presentation"
  onclick={handleOverlayClick}
>
  <div
    bind:this={modalElement}
    class={modalClass}
    role="dialog"
    aria-modal="true"
    aria-labelledby={titleId}
    tabindex="-1"
  >
    <div class="modal-header">
      <h3 id={titleId} class="modal-title">{title}</h3>
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
