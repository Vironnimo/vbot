<script>
  // Shared context menu. The caller owns the open state: it builds `menu`
  // from `contextMenuAnchor(event)` (./contextMenu.js) plus a label and items,
  // and clears it in `onClose`. The primitive owns everything else - the
  // portal to <body>, viewport clamping, keyboard navigation, dismissal and
  // focus return - so every context menu in the app behaves and looks the
  // same. Back closes an open menu instead of navigating.
  //
  // menu: null | {
  //   x, y,              viewport coordinates of the anchor
  //   above?,            top edge of the anchor's element: a menu without
  //                      room below opens above it instead of covering it
  //   returnFocus,       element focused again after Escape or an action
  //   label,             accessible name of the menu
  //   items: [{ id, label, onSelect, danger?, disabled?, group?, hint? }]
  // }
  // A `group` change between consecutive items renders a separator. `hint`
  // is short muted text after the label, e.g. why an item is disabled.
  // The optional `icon` snippet receives each item and renders its leading
  // icon.
  //
  // Selecting an item closes the menu, returns focus, and only then runs
  // `onSelect`, so an action that opens a dialog keeps the dialog's focus.
  import { tick } from 'svelte';

  import { portal } from '$lib/dropdownPanel.js';
  import { useNavigation } from '$lib/navigation.svelte.js';

  import Button from './Button.svelte';

  const VIEWPORT_MARGIN = 8;

  let { menu = null, onClose = () => {}, icon = undefined } = $props();

  const navigation = useNavigation();
  let element = $state(null);
  let placement = $state.raw(null);

  const items = $derived(Array.isArray(menu?.items) ? menu.items : []);
  const placed = $derived(placement !== null && placement.menu === menu);
  const left = $derived(placed ? placement.x : (menu?.x ?? 0));
  const top = $derived(placed ? placement.y : (menu?.y ?? 0));

  function clamp(value, size, viewport) {
    const maximum = Math.max(
      VIEWPORT_MARGIN,
      viewport - size - VIEWPORT_MARGIN,
    );
    const start = Number.isFinite(value) ? value : VIEWPORT_MARGIN;
    return Math.min(Math.max(start, VIEWPORT_MARGIN), maximum);
  }

  function enabledItems() {
    return Array.from(
      element?.querySelectorAll('[role="menuitem"]:not(:disabled)') ?? [],
    );
  }

  function focusElement(target) {
    if (target instanceof HTMLElement && target.isConnected) {
      target.focus({ preventScroll: true });
    }
  }

  function close({ restoreFocus = false } = {}) {
    const current = menu;
    if (!current) return;
    onClose();
    if (restoreFocus) focusElement(current.returnFocus);
  }

  function select(item) {
    if (!item || item.disabled) return;
    const current = menu;
    onClose();
    focusElement(current?.returnFocus);
    item.onSelect?.();
  }

  $effect(() => {
    if (!menu) return;
    return navigation?.registerLayer({
      close: () => close({ restoreFocus: true }),
    });
  });

  function itemForButton(button) {
    const index = Number(button?.dataset.contextMenuIndex);
    return Number.isInteger(index) ? items[index] : null;
  }

  function handleKeydown(event) {
    const buttons = enabledItems();
    const index = buttons.indexOf(document.activeElement);
    let next;
    if (event.key === 'ArrowDown') {
      next = index < 0 ? 0 : (index + 1) % buttons.length;
    } else if (event.key === 'ArrowUp') {
      next =
        index < 0
          ? buttons.length - 1
          : (index - 1 + buttons.length) % buttons.length;
    } else if (event.key === 'Home') {
      next = 0;
    } else if (event.key === 'End') {
      next = buttons.length - 1;
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (index >= 0) select(itemForButton(buttons[index]));
      return;
    } else if (event.key === 'Tab') {
      event.preventDefault();
      close({ restoreFocus: true });
      return;
    } else {
      return;
    }
    event.preventDefault();
    if (buttons.length > 0) focusElement(buttons[next]);
  }

  // A right click on the menu itself must neither show the native menu nor
  // reach the Desktop context menu.
  function handleContextMenu(event) {
    event.preventDefault();
  }

  // Measure after render, clamp into the viewport, then focus the first
  // enabled item once the menu is visible.
  $effect(() => {
    const current = menu;
    const node = element;
    if (!current || !node) return undefined;
    const bounds = node.getBoundingClientRect();
    const fitsBelow =
      current.y + bounds.height <= window.innerHeight - VIEWPORT_MARGIN;
    const fitsAbove =
      Number.isFinite(current.above) &&
      current.above - bounds.height >= VIEWPORT_MARGIN;
    placement = {
      menu: current,
      x: clamp(current.x, bounds.width, window.innerWidth),
      y: clamp(
        !fitsBelow && fitsAbove ? current.above - bounds.height : current.y,
        bounds.height,
        window.innerHeight,
      ),
    };
    let cancelled = false;
    void tick().then(() => {
      if (cancelled || menu !== current) return;
      focusElement(enabledItems()[0] ?? element);
    });
    return () => {
      cancelled = true;
    };
  });

  // Dismissal while open. Escape is taken in the capture phase so it closes
  // only the menu, never a dialog or overlay underneath.
  $effect(() => {
    if (!menu) return undefined;
    const outsideMenu = (target) =>
      !(target instanceof Node && element?.contains(target));
    const handlePointerDown = (event) => {
      if (outsideMenu(event.target)) close();
    };
    const handleScroll = (event) => {
      if (outsideMenu(event.target)) close();
    };
    const handleWindowKeydown = (event) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      close({ restoreFocus: true });
    };
    const dismiss = () => close();
    window.addEventListener('pointerdown', handlePointerDown, true);
    window.addEventListener('scroll', handleScroll, true);
    window.addEventListener('keydown', handleWindowKeydown, true);
    window.addEventListener('resize', dismiss);
    window.addEventListener('blur', dismiss);
    return () => {
      window.removeEventListener('pointerdown', handlePointerDown, true);
      window.removeEventListener('scroll', handleScroll, true);
      window.removeEventListener('keydown', handleWindowKeydown, true);
      window.removeEventListener('resize', dismiss);
      window.removeEventListener('blur', dismiss);
    };
  });
</script>

{#if menu}
  <div
    use:portal
    bind:this={element}
    class="context-menu"
    role="menu"
    tabindex="-1"
    aria-label={menu.label || undefined}
    style={`left: ${left}px; top: ${top}px; visibility: ${placed ? 'visible' : 'hidden'};`}
    onkeydown={handleKeydown}
    oncontextmenu={handleContextMenu}
  >
    {#each items as item, index (item.id)}
      {#if index > 0 && items[index - 1].group !== item.group}
        <div class="context-menu__separator" role="separator"></div>
      {/if}
      <Button
        variant="tertiary"
        class={item.danger
          ? 'context-menu__item context-menu__item--danger'
          : 'context-menu__item'}
        role="menuitem"
        tabindex="-1"
        disabled={Boolean(item.disabled)}
        data-context-menu-index={index}
        onClick={() => select(item)}
      >
        {#if icon}{@render icon(item)}{/if}
        <span class="context-menu__label">{item.label}</span>
        {#if item.hint}
          <span class="context-menu__hint">{item.hint}</span>
        {/if}
      </Button>
    {/each}
  </div>
{/if}
