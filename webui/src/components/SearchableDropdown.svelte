<script>
  import { tick } from 'svelte';

  import { computePanelPosition, portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';

  const SEARCH_HEADER_HEIGHT = 44;
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
  let panelElement = $state();
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

    const { placement, left, width, verticalRule, optionsMaxHeight } =
      computePanelPosition(triggerElement, {
        reservedHeight: SEARCH_HEADER_HEIGHT,
      });

    panelPlacement = placement;
    panelStyle = [
      `left: ${left}px`,
      verticalRule,
      `width: ${width}px`,
      `--searchable-dropdown-options-max-height: ${optionsMaxHeight}px`,
    ].join('; ');
  }

  function handleDocumentMouseDown(event) {
    if (!isOpen) {
      return;
    }

    // The panel is portaled out of `rootElement`, so check both.
    if (
      rootElement?.contains(event.target) ||
      panelElement?.contains(event.target)
    ) {
      return;
    }

    close();
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

  function handleWindowScroll(event) {
    if (!isOpen) {
      return;
    }

    if (event.target instanceof Node && panelElement?.contains(event.target)) {
      return;
    }

    close();
  }

  $effect(() => {
    if (!isOpen) {
      return undefined;
    }

    window.addEventListener('scroll', handleWindowScroll, true);

    return () => {
      window.removeEventListener('scroll', handleWindowScroll, true);
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
    <svg
      class="dropdown-chevron"
      viewBox="0 0 12 12"
      width="10"
      height="10"
      aria-hidden="true"
    >
      <path d="M2 4l4 4 4-4" />
    </svg>
  </button>

  {#if isOpen}
    <div
      bind:this={panelElement}
      use:portal
      class="s-dropdown-panel searchable-dropdown__panel {panelClass}"
      role="listbox"
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
              <span class="searchable-dropdown__option-label"
                >{option.label}</span
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
  {/if}
</div>
