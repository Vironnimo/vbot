<script>
  // One occurrence's details as a card beside the calendar entry that opened
  // it, like a pinned hover card: a non-modal dialog portaled to <body> and
  // placed with the shared floating geometry (it flips and stays inside the
  // window). Focus moves into it; Escape and the close button close it and
  // return focus to the entry, a press outside it closes it where the press
  // lands. Dialogs opened from the card (an Agent job, the delete question)
  // stack above it, and presses inside them or inside other floating layers
  // leave it open. Without its entry in view (a new event on another day) it
  // floats near the top of the window. Phones show the same content in the
  // Modal bottom sheet.
  //
  // The caller owns the content: `details` renders everything between the
  // title and the actions.
  import { onDestroy, onMount, tick } from 'svelte';

  import { portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';
  import { useNavigation } from '$lib/navigation.svelte.js';
  import { positionFloating } from '$lib/tooltip/geometry.js';
  import Button from '../ui/Button.svelte';
  import Modal from '../ui/Modal.svelte';

  const noop = () => {};
  const PHONE_QUERY = '(max-width: 640px)';
  // The card's distance from the window's edges.
  const EDGE = 8;
  const FOCUSABLE = [
    'a[href]',
    'button:not([disabled])',
    'input:not([disabled])',
    'textarea:not([disabled])',
    '[tabindex]:not([tabindex="-1"])',
  ].join(',');
  const componentId = $props.id();
  const titleId = `${componentId}-title`;

  let {
    title = '',
    // The occurrence's id: the entry carrying it in `data-occurrence-id`
    // anchors the card when `anchor` is missing or was re-rendered.
    occurrenceId = '',
    anchor = null,
    onClose = noop,
    onEdit = noop,
    onDelete = noop,
    details,
  } = $props();

  const navigation = useNavigation();
  let cardElement = $state();
  let phone = $state(matchesPhone());
  let appRoot = null;
  let sizeObserver = null;
  let releaseLayer = noop;
  // Whether closing hands focus back to the entry; a press elsewhere keeps it
  // where that press put it.
  let returnFocus = true;

  function matchesPhone() {
    return (
      typeof window.matchMedia === 'function' &&
      window.matchMedia(PHONE_QUERY).matches
    );
  }

  function anchorElement() {
    if (anchor?.isConnected) return anchor;
    if (!occurrenceId) return null;
    const id = globalThis.CSS?.escape?.(occurrenceId) ?? occurrenceId;
    return document.querySelector(`[data-occurrence-id="${id}"]`);
  }

  function position() {
    if (!cardElement) return;
    const target = anchorElement();
    if (target) {
      positionFloating(target, cardElement, 'right');
      // Beside its entry the card starts level with it and grows downward,
      // so loading its Agent jobs does not move what is already shown.
      const side = cardElement.dataset.floatingSide;
      if (side === 'left' || side === 'right') {
        const room = window.innerHeight - cardElement.offsetHeight - EDGE;
        const top = Math.min(target.getBoundingClientRect().top, room);
        cardElement.style.top = `${Math.max(EDGE, top)}px`;
      }
      return;
    }
    const width = cardElement.offsetWidth;
    cardElement.style.left = `${Math.max(EDGE, (window.innerWidth - width) / 2)}px`;
    cardElement.style.top = `${Math.round(window.innerHeight * 0.12)}px`;
    cardElement.dataset.floatingSide = 'bottom';
  }

  function close({ restoreFocus = true } = {}) {
    returnFocus = restoreFocus;
    onClose();
  }

  // The card's own key handling: Escape closes it unless a control inside
  // (an info popover) consumed the key; Tab stays within the card.
  function handleKeydown(event) {
    if (event.key === 'Escape' && !event.defaultPrevented) {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = [...cardElement.querySelectorAll(FOCUSABLE)];
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable.at(-1);
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === cardElement)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  // The card leaves the app's own tree for <body>; it remembers that tree so
  // that a press in it counts as outside. Menus, pickers and tooltips are
  // portaled next to it and dialogs open in a modal overlay: pressing there
  // is not pressing outside the card.
  function placeOnBody(node) {
    appRoot = node.parentElement?.closest('body > *') ?? null;
    return portal(node);
  }

  function pressedElsewhere(target) {
    if (!(target instanceof Element)) return false;
    if (cardElement?.contains(target)) return false;
    if (anchorElement()?.contains(target)) return false;
    if (target.closest('.modal-overlay')) return false;
    const root = appRoot ?? anchorElement()?.closest('body > *');
    return Boolean(root?.contains(target));
  }

  function handlePointerDown(event) {
    if (pressedElsewhere(event.target)) close({ restoreFocus: false });
  }

  function handleScroll(event) {
    if (event.target instanceof Node && cardElement?.contains(event.target)) {
      return;
    }
    position();
  }

  function handleMediaChange() {
    phone = matchesPhone();
  }

  onMount(() => {
    const media =
      typeof window.matchMedia === 'function'
        ? window.matchMedia(PHONE_QUERY)
        : null;
    media?.addEventListener?.('change', handleMediaChange);
    return () => media?.removeEventListener?.('change', handleMediaChange);
  });

  // Placed beside its entry, and again when it opens for another one.
  $effect(() => {
    if (!phone && cardElement) position();
  });

  $effect(() => {
    if (phone || !cardElement) return undefined;
    void tick().then(() => cardElement?.focus({ preventScroll: true }));
    releaseLayer = navigation?.registerLayer({ close }) ?? noop;
    window.addEventListener('pointerdown', handlePointerDown, true);
    window.addEventListener('scroll', handleScroll, true);
    window.addEventListener('resize', position);
    if (typeof ResizeObserver === 'function') {
      sizeObserver = new ResizeObserver(() => position());
      sizeObserver.observe(cardElement);
    }
    return () => {
      releaseLayer();
      releaseLayer = noop;
      window.removeEventListener('pointerdown', handlePointerDown, true);
      window.removeEventListener('scroll', handleScroll, true);
      window.removeEventListener('resize', position);
      sizeObserver?.disconnect();
      sizeObserver = null;
    };
  });

  onDestroy(() => {
    if (!phone && returnFocus) anchorElement()?.focus({ preventScroll: true });
  });
</script>

{#snippet deleteButton()}
  <Button
    variant="tertiary"
    icon
    class="calendar-event-card__delete"
    ariaLabel={t('calendar.deleteEvent')}
    tooltip={t('calendar.deleteEvent')}
    onClick={onDelete}
  >
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
      <path
        d="M2.75 4.25h10.5M6.25 4.25V2.75h3.5v1.5M4.25 4.25l.6 8.6c.05.7.6 1.15 1.25 1.15h3.8c.65 0 1.2-.45 1.25-1.15l.6-8.6M6.75 7v4.25M9.25 7v4.25"
        fill="none"
        stroke="currentColor"
        stroke-width="1.25"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    </svg>
  </Button>
{/snippet}

{#snippet editButton()}
  <Button variant="secondary" onClick={onEdit}>{t('common.edit')}</Button>
{/snippet}

{#if phone}
  <Modal {title} size="md" class="calendar-event-sheet" {onClose}>
    {#snippet body()}
      <div class="modal-body">
        <div class="calendar-detail">{@render details?.()}</div>
      </div>
    {/snippet}
    {#snippet footer()}
      <div class="modal-footer__start">{@render deleteButton()}</div>
      {@render editButton()}
    {/snippet}
  </Modal>
{:else}
  <div
    bind:this={cardElement}
    use:placeOnBody
    class="floating-card calendar-event-card"
    role="dialog"
    aria-labelledby={titleId}
    tabindex="-1"
    data-floating-open="true"
    onkeydown={handleKeydown}
  >
    <div class="calendar-event-card__head">
      <h2 id={titleId} class="calendar-event-card__title">{title}</h2>
      <Button
        variant="tertiary"
        icon
        class="calendar-event-card__close"
        ariaLabel={t('common.close')}
        onClick={() => close()}
      >
        <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
          <path
            d="m6 6 12 12M18 6 6 18"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
          />
        </svg>
      </Button>
    </div>
    <div class="calendar-detail">{@render details?.()}</div>
    <div class="calendar-event-card__actions">
      {@render deleteButton()}
      {@render editButton()}
    </div>
  </div>
{/if}
