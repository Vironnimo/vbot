<script>
  import { tick } from 'svelte';

  import { t } from '$lib/i18n.js';

  const PANEL_EDGE_PADDING = 8;
  const PANEL_OFFSET = 4;
  const PANEL_MIN_OPTIONS_HEIGHT = 96;
  const noop = () => {};

  let {
    id = '',
    name = '',
    value = '',
    options = [],
    placeholder = t('dropdown.placeholder', 'Select an option'),
    searchPlaceholder = t('dropdown.searchPlaceholder', 'Filter options…'),
    emptyLabel = t('dropdown.empty', 'No options match'),
    disabled = false,
    ariaLabel = '',
    triggerClass = '',
    panelClass = '',
    onValueChange = noop,
    onOpenChange = noop,
  } = $props();

  let rootElement = $state();
  let triggerElement = $state();
  let searchInputElement = $state();
  let isOpen = $state(false);
  let searchQuery = $state('');
  let panelStyle = $state('');
  let panelPlacement = $state('bottom');

  let normalizedOptions = $derived(normalizeOptions(options));
  let filteredOptions = $derived(filterOptions(normalizedOptions, searchQuery));
  let selectedOption = $derived(
    normalizedOptions.find((option) => option.value === value) ?? null,
  );
  let triggerLabel = $derived(selectedOption?.label || placeholder);
  let hasSelection = $derived(Boolean(selectedOption));

  function normalizeOptions(items) {
    return items.map((option) => {
      if (typeof option === 'string') {
        return {
          value: option,
          label: option,
          searchText: option,
          disabled: false,
        };
      }

      const label = option?.label ?? option?.value ?? '';
      const secondaryLabel = option?.secondaryLabel ?? '';

      return {
        value: option?.value ?? '',
        label,
        disabled: Boolean(option?.disabled),
        secondaryLabel,
        searchText: option?.searchText ?? `${label} ${secondaryLabel}`.trim(),
      };
    });
  }

  function filterOptions(items, query) {
    const normalizedQuery = query.trim().toLowerCase();

    if (!normalizedQuery) {
      return items;
    }

    return items.filter((option) =>
      option.searchText.toLowerCase().includes(normalizedQuery),
    );
  }

  async function open() {
    if (disabled) {
      return;
    }

    isOpen = true;
    onOpenChange(true);
    await tick();
    updatePanelPosition();
    searchInputElement?.focus();
  }

  function close() {
    if (!isOpen) {
      return;
    }

    isOpen = false;
    searchQuery = '';
    panelStyle = '';
    panelPlacement = 'bottom';
    onOpenChange(false);
  }

  async function toggleOpen() {
    if (isOpen) {
      close();
      return;
    }

    await open();
  }

  function updatePanelPosition() {
    if (!isOpen || !triggerElement) {
      return;
    }

    const rect = triggerElement.getBoundingClientRect();
    const width = rect.width;
    const availableBelow =
      window.innerHeight - rect.bottom - PANEL_OFFSET - PANEL_EDGE_PADDING;
    const availableAbove = rect.top - PANEL_OFFSET - PANEL_EDGE_PADDING;
    const useAbove = availableBelow < 200 && availableAbove > availableBelow;
    const maxOptionsHeight = Math.max(
      PANEL_MIN_OPTIONS_HEIGHT,
      Math.min(useAbove ? availableAbove - 44 : availableBelow - 44, 240),
    );
    const left = Math.min(
      Math.max(PANEL_EDGE_PADDING, rect.left),
      Math.max(
        PANEL_EDGE_PADDING,
        window.innerWidth - width - PANEL_EDGE_PADDING,
      ),
    );
    const top = useAbove
      ? Math.max(PANEL_EDGE_PADDING, rect.top - PANEL_OFFSET)
      : Math.min(
          window.innerHeight - PANEL_EDGE_PADDING,
          rect.bottom + PANEL_OFFSET,
        );

    panelPlacement = useAbove ? 'top' : 'bottom';
    panelStyle = [
      `left: ${left}px`,
      useAbove
        ? `bottom: ${window.innerHeight - rect.top + PANEL_OFFSET}px`
        : `top: ${top}px`,
      `width: ${width}px`,
      `--searchable-dropdown-options-max-height: ${maxOptionsHeight}px`,
    ].join('; ');
  }

  function handleDocumentMouseDown(event) {
    if (!isOpen || !rootElement?.contains(event.target)) {
      close();
    }
  }

  function handleDocumentKeyDown(event) {
    if (event.key === 'Escape') {
      close();
    }
  }

  function handleWindowResize() {
    updatePanelPosition();
  }

  function selectOption(option) {
    if (option.disabled) {
      return;
    }

    onValueChange(option.value, option);
    close();
  }

  $effect(() => {
    if (!isOpen) {
      return undefined;
    }

    const closeOnScroll = () => close();
    window.addEventListener('scroll', closeOnScroll, true);

    return () => {
      window.removeEventListener('scroll', closeOnScroll, true);
    };
  });
</script>

<svelte:document
  onmousedown={handleDocumentMouseDown}
  onkeydown={handleDocumentKeyDown}
/>

<svelte:window onresize={handleWindowResize} />

<div
  bind:this={rootElement}
  class="s-dropdown searchable-dropdown {triggerClass}"
  class:open={isOpen}
  data-state={isOpen ? 'open' : 'closed'}
>
  {#if name}
    <input type="hidden" {name} {value} />
  {/if}

  <button
    bind:this={triggerElement}
    {id}
    class="s-dropdown-trigger searchable-dropdown__trigger"
    type="button"
    {disabled}
    aria-label={ariaLabel || placeholder}
    aria-haspopup="listbox"
    aria-expanded={isOpen}
    onclick={toggleOpen}
  >
    <span
      class="searchable-dropdown__trigger-label"
      class:searchable-dropdown__trigger-label--placeholder={!hasSelection}
    >
      {triggerLabel}
    </span>
    <svg class="dropdown-chevron" viewBox="0 0 12 12" aria-hidden="true">
      <path d="M2 4l4 4 4-4" />
    </svg>
  </button>

  <div
    class="s-dropdown-panel searchable-dropdown__panel {panelClass}"
    role="listbox"
    aria-hidden={!isOpen}
    data-placement={panelPlacement}
    data-positioning="fixed"
    style={panelStyle}
  >
    <div class="s-dropdown-search searchable-dropdown__search">
      <svg viewBox="0 0 12 12" aria-hidden="true">
        <circle cx="5" cy="5" r="3.5" />
        <path d="M8 8l2.5 2.5" />
      </svg>
      <input
        bind:this={searchInputElement}
        type="text"
        bind:value={searchQuery}
        placeholder={searchPlaceholder}
      />
    </div>

    <div class="s-dropdown-options searchable-dropdown__options">
      {#if filteredOptions.length > 0}
        {#each filteredOptions as option (option.value)}
          <button
            class="s-dropdown-opt searchable-dropdown__option"
            class:selected={option.value === value}
            type="button"
            role="option"
            disabled={option.disabled}
            aria-selected={option.value === value}
            onclick={() => selectOption(option)}
          >
            <span class="searchable-dropdown__option-label">{option.label}</span
            >
            {#if option.secondaryLabel}
              <span class="searchable-dropdown__option-meta">
                {option.secondaryLabel}
              </span>
            {/if}
          </button>
        {/each}
      {:else}
        <div class="s-dropdown-empty searchable-dropdown__empty">
          {emptyLabel}
        </div>
      {/if}
    </div>
  </div>
</div>
