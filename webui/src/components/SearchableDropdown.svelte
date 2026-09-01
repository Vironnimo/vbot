<script>
  import { tick, untrack } from 'svelte';

  import { computePanelPosition, portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';

  const SEARCH_HEADER_HEIGHT = 44;
  const noop = () => {};
  const componentId = $props.id();

  let {
    id = '',
    name = '',
    value = '',
    options = [],
    placeholder = t('dropdown.placeholder', 'Select an option'),
    searchPlaceholder = t('dropdown.searchPlaceholder', 'Filter options…'),
    emptyLabel = t('dropdown.empty', 'No options match'),
    // Default filter the search box carries: the panel opens with this term
    // already applied and returns to it on close. Empty for every caller that
    // does not pass it, so the historical "opens unfiltered" behavior is kept.
    searchText = '',
    disabled = false,
    ariaLabel = '',
    ariaDescribedby = undefined,
    triggerClass = '',
    panelClass = '',
    // Optional action row pinned under the options (e.g. the model pickers'
    // "show all models" toggle). Clicking it keeps the panel open so the
    // revealed options appear in place.
    footerActionLabel = '',
    onFooterAction = noop,
    onValueChange = noop,
    onOpenChange = noop,
  } = $props();

  let rootElement = $state();
  let triggerElement = $state();
  let panelElement = $state();
  let searchInputElement = $state();
  let isOpen = $state(false);
  // Seeded from the prop at creation (the dropdown is recreated per use); the
  // close() reset re-applies the current default filter reactively.
  let searchQuery = $state(untrack(() => searchText));
  let panelStyle = $state('');
  let panelPlacement = $state('bottom');
  let activeOptionValue = $state('');

  let normalizedOptions = $derived(normalizeOptions(options));
  let filteredOptions = $derived(filterOptions(normalizedOptions, searchQuery));
  let selectedOption = $derived(
    normalizedOptions.find((option) => option.value === value) ?? null,
  );
  let triggerLabel = $derived(selectedOption?.label || placeholder);
  let hasSelection = $derived(Boolean(selectedOption));
  let listboxId = $derived(id ? `${id}-listbox` : `${componentId}-listbox`);
  let activeOptionIndex = $derived(
    filteredOptions.findIndex(
      (option) => option.value === activeOptionValue && !option.disabled,
    ),
  );
  let activeDescendantId = $derived(
    activeOptionIndex >= 0
      ? `${listboxId}-option-${activeOptionIndex}`
      : undefined,
  );

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

  async function open({ focus = 'selected' } = {}) {
    if (disabled) {
      return;
    }

    isOpen = true;
    setInitialActiveOption(focus);
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
    searchQuery = searchText;
    panelStyle = '';
    panelPlacement = 'bottom';
    activeOptionValue = '';
    onOpenChange(false);
  }

  async function toggleOpen() {
    if (isOpen) {
      close();
      return;
    }

    await open();
  }

  function enabledFilteredOptions() {
    return filteredOptions.filter((option) => !option.disabled);
  }

  function setInitialActiveOption(focus) {
    const enabledOptions = enabledFilteredOptions();
    if (enabledOptions.length === 0) {
      activeOptionValue = '';
      return;
    }
    if (focus === 'last') {
      activeOptionValue = enabledOptions.at(-1).value;
      return;
    }
    const selectedEnabled = enabledOptions.find(
      (option) => option.value === value,
    );
    activeOptionValue = (selectedEnabled ?? enabledOptions[0]).value;
  }

  async function moveActiveOption(direction) {
    const enabledOptions = enabledFilteredOptions();
    if (enabledOptions.length === 0) {
      activeOptionValue = '';
      return;
    }
    const currentIndex = enabledOptions.findIndex(
      (option) => option.value === activeOptionValue,
    );
    let nextIndex;
    if (direction === 'first') {
      nextIndex = 0;
    } else if (direction === 'last') {
      nextIndex = enabledOptions.length - 1;
    } else if (direction === 1) {
      nextIndex =
        (currentIndex + 1 + enabledOptions.length) % enabledOptions.length;
    } else {
      nextIndex =
        (currentIndex - 1 + enabledOptions.length) % enabledOptions.length;
    }
    activeOptionValue = enabledOptions[nextIndex].value;
    await tick();
    document
      .getElementById(activeDescendantId)
      ?.scrollIntoView?.({ block: 'nearest' });
  }

  function handleTriggerKeyDown(event) {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      return;
    }
    event.preventDefault();
    open({
      focus:
        event.key === 'ArrowUp' || event.key === 'End' ? 'last' : 'selected',
    });
  }

  function handleSearchInput() {
    setInitialActiveOption('selected');
  }

  async function handleSearchKeyDown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      triggerElement?.focus();
      return;
    }
    if (event.key === 'Tab') {
      close();
      return;
    }
    if (event.key === 'Enter') {
      const activeOption = filteredOptions[activeOptionIndex];
      if (activeOption) {
        event.preventDefault();
        selectOption(activeOption);
        triggerElement?.focus();
      }
      return;
    }
    const directions = {
      ArrowDown: 1,
      ArrowUp: -1,
      Home: 'first',
      End: 'last',
    };
    if (!(event.key in directions)) {
      return;
    }
    event.preventDefault();
    await moveActiveOption(directions[event.key]);
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
    aria-describedby={ariaDescribedby}
    aria-haspopup="listbox"
    aria-expanded={isOpen}
    aria-controls={isOpen ? listboxId : undefined}
    onclick={toggleOpen}
    onkeydown={handleTriggerKeyDown}
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
          aria-label={searchPlaceholder}
          role="combobox"
          aria-autocomplete="list"
          aria-expanded="true"
          aria-controls={listboxId}
          aria-activedescendant={activeDescendantId}
          oninput={handleSearchInput}
          onkeydown={handleSearchKeyDown}
        />
      </div>

      <div
        class="s-dropdown-options searchable-dropdown__options"
        id={listboxId}
        role="listbox"
        tabindex="-1"
        aria-label={ariaLabel || placeholder}
      >
        {#if filteredOptions.length > 0}
          {#each filteredOptions as option (option.value)}
            <button
              class="s-dropdown-opt searchable-dropdown__option"
              class:selected={option.value === value}
              type="button"
              role="option"
              id={`${listboxId}-option-${filteredOptions.indexOf(option)}`}
              tabindex="-1"
              disabled={option.disabled}
              aria-selected={option.value === value}
              class:active={option.value === activeOptionValue}
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

      {#if footerActionLabel}
        <button
          class="s-dropdown-footer searchable-dropdown__footer"
          type="button"
          onclick={() => onFooterAction()}
        >
          {footerActionLabel}
        </button>
      {/if}
    </div>
  {/if}
</div>
