<script module>
  // Open modals, oldest first. A dialog opened from inside another one (a
  // path picker's Browse dialog in a form dialog) is the only one that
  // answers Escape and keeps Tab inside itself until it closes.
  const openModals = [];
</script>

<script>
  // Shared modal shell. It owns the dialog semantics every modal needs — the
  // dimmed overlay, overlay-click-to-close, Escape-to-close, `role="dialog"` /
  // `aria-modal`, the header with title, optional description and close
  // button, and moving focus into the dialog on open, and closing on Back
  // instead of navigating — plus its geometry: one of four widths (`size`), a
  // centered dialog or a `sheet` docked at the window's end, a bottom sheet on
  // phones, and a bounded height in which the caller's `.modal-body` scrolls
  // while header and footer stay. Each caller supplies only its own body (and
  // an optional footer) content. Caller text arrives already translated; the
  // shell only translates its own close-button label, like `Dropdown` does
  // for its placeholder.
  //
  // Once the user has typed or changed a value inside the dialog, a stray
  // click on the backdrop no longer discards it: the dialog nudges instead.
  // Escape, Back and the close button still close it deliberately.

  import { onDestroy, onMount } from 'svelte';

  import { t } from '$lib/i18n.js';
  import { isImeComposing } from '$lib/keyboard.js';
  import { useNavigation } from '$lib/navigation.svelte.js';

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
    // Optional one-line lead under the title; it becomes the dialog's
    // accessible description.
    description = '',
    labelledById = '',
    // sm: confirmations and single questions; md: short forms (default);
    // lg: forms with sections or lists; xl: catalogs and browsers.
    size = 'md',
    // `dialog` floats centered near the top; `sheet` docks at the window's
    // end at full height, for long editors.
    variant = 'dialog',
    closeDisabled = false,
    closeLabel = t('common.close'),
    class: className = '',
    onClose = noop,
    body,
    footer,
  } = $props();

  const navigation = useNavigation();
  const stackEntry = {};
  let releaseLayer = noop;
  let modalElement = $state();
  let overlayElement = $state();
  let previouslyFocusedElement;
  let restoredInertElements = [];
  let edited = false;
  let backdropPointerDown = false;
  let nudging = $state(false);

  let modalClass = $derived(
    [
      'modal',
      `modal--${size}`,
      variant === 'sheet' && 'modal--sheet',
      nudging && 'modal--nudge',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
  let titleId = $derived(labelledById || `${componentId}-title`);
  let descriptionId = $derived(
    description ? `${componentId}-description` : undefined,
  );

  function requestClose() {
    if (closeDisabled) {
      return;
    }

    onClose();
  }

  function handleDocumentKeydown(event) {
    if (openModals.at(-1) !== stackEntry) {
      return;
    }

    if (isImeComposing(event)) {
      // Keep native IME cancellation, but do not let the same Escape close
      // a background layer through the app shell's window handler.
      if (event.key === 'Escape') event.stopPropagation();
      return;
    }

    if (event.key === 'Escape') {
      // A nested picker consumes Escape when dismissing its own popup.
      if (!event.defaultPrevented) requestClose();
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

  function handleOverlayPointerDown(event) {
    backdropPointerDown = event.target === event.currentTarget;
  }

  function handleOverlayClick(event) {
    // Only a click that starts and ends on the backdrop itself closes: clicks
    // inside the box bubble up with a different target, and a text selection
    // dragged out of the box ends on the backdrop without starting there.
    const pressedBackdrop = backdropPointerDown;
    backdropPointerDown = false;
    if (event.target !== event.currentTarget) return;
    if (event.detail > 0 && !pressedBackdrop) return;
    if (edited && !closeDisabled) {
      nudging = false;
      requestAnimationFrame(() => (nudging = true));
      return;
    }
    requestClose();
  }

  function markEdited() {
    edited = true;
  }

  onMount(() => {
    previouslyFocusedElement =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : undefined;
    openModals.push(stackEntry);
    isolateBackground();
    releaseLayer = navigation?.registerLayer({ close: requestClose }) ?? noop;
    // Programmatic focus on the tabindex=-1 box does not trigger :focus-visible,
    // so no focus ring appears — it just lands keyboard focus inside the dialog.
    modalElement?.focus();
  });

  onDestroy(() => {
    const stackIndex = openModals.indexOf(stackEntry);
    if (stackIndex >= 0) openModals.splice(stackIndex, 1);
    releaseLayer();
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
  class:modal-overlay--sheet={variant === 'sheet'}
  role="presentation"
  onpointerdown={handleOverlayPointerDown}
  onclick={handleOverlayClick}
>
  <div
    bind:this={modalElement}
    class={modalClass}
    role="dialog"
    aria-modal="true"
    aria-labelledby={titleId}
    aria-describedby={descriptionId}
    tabindex="-1"
    oninput={markEdited}
    onchange={markEdited}
    onanimationend={(event) => {
      if (event.animationName === 'modal-nudge') nudging = false;
    }}
  >
    <div class="modal-header">
      <div class="modal-heading">
        <h3 id={titleId} class="modal-title">{title}</h3>
        {#if description}
          <p id={descriptionId} class="modal-description">{description}</p>
        {/if}
      </div>
      <button
        type="button"
        class="modal-close"
        aria-label={closeLabel}
        disabled={closeDisabled}
        onclick={requestClose}
      >
        <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"
          ><path d="m6 6 12 12M18 6 6 18" /></svg
        >
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
