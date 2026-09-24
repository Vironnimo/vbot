<script>
  import { tick } from 'svelte';

  import {
    computePanelPosition,
    optionDecorations,
    portal,
  } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';

  const noop = () => {};
  const componentId = $props.id();
  // Typed characters within this window extend one typeahead search.
  const TYPEAHEAD_RESET_MS = 500;

  let {
    id = '',
    name = '',
    value = '',
    options = [],
    placeholder = t('dropdown.placeholder', 'Select an option'),
    disabled = false,
    ariaLabel = '',
    ariaDescribedby = undefined,
    triggerClass = '',
    // Optional quick tooltip on the trigger (e.g. details of the selection).
    triggerTooltip = '',
    // The list is at least this wide even under a narrower trigger.
    panelMinWidth = 0,
    listClass = '',
    onValueChange = noop,
    onOpenChange = noop,
  } = $props();

  let rootElement = $state();
  let triggerElement = $state();
  let listElement = $state();
  let isOpen = $state(false);
  let listStyle = $state('');
  let listPlacement = $state('bottom');
  // null = no active option; '' is a valid option value (e.g. "All").
  let activeOptionValue = $state(null);
  let typeaheadQuery = '';
  let typeaheadAt = 0;

  let normalizedOptions = $derived(normalizeOptions(options));
  let selectedOption = $derived(
    normalizedOptions.find((option) => option.value === value) ?? null,
  );
  let triggerLabel = $derived(selectedOption?.label || placeholder);
  let hasSelection = $derived(Boolean(selectedOption));
  let listboxId = $derived(id ? `${id}-listbox` : `${componentId}-listbox`);
  let activeOptionId = $derived(
    activeOptionValue !== null
      ? `${listboxId}-option-${normalizedOptions.findIndex((option) => option.value === activeOptionValue)}`
      : undefined,
  );

  function normalizeOptions(items) {
    return items.map((option) => {
      if (typeof option === 'string') {
        return {
          value: option,
          label: option,
          disabled: false,
          ...optionDecorations(null),
        };
      }

      return {
        value: option?.value ?? '',
        label: option?.label ?? option?.value ?? '',
        disabled: Boolean(option?.disabled),
        secondaryLabel: option?.secondaryLabel ?? '',
        ...optionDecorations(option),
      };
    });
  }

  // Also opens the list from a related control elsewhere (via bind:this).
  export async function open({ focus = '' } = {}) {
    if (disabled) {
      return;
    }

    isOpen = true;
    setInitialActiveOption(focus || 'selected');
    onOpenChange(true);
    await tick();
    updateListPosition();
    listElement?.focus();
  }

  function close() {
    if (!isOpen) {
      return;
    }

    isOpen = false;
    listStyle = '';
    listPlacement = 'bottom';
    activeOptionValue = null;
    typeaheadQuery = '';
    onOpenChange(false);
  }

  function toggleOpen() {
    if (disabled) {
      return;
    }

    if (isOpen) {
      close();
      return;
    }

    open();
  }

  function updateListPosition() {
    if (!isOpen || !triggerElement || !listElement) {
      return;
    }

    // Pass the natural content height so the panel flips above when it would
    // not fit below, and cap it to the room actually available on the chosen
    // side (with the list's own `overflow-y: auto`) so a trigger near the
    // viewport edge never renders half its options off-screen.
    const { placement, left, width, verticalRule, optionsMaxHeight } =
      computePanelPosition(triggerElement, {
        contentHeight: listElement.scrollHeight,
        minWidth: panelMinWidth,
      });

    listPlacement = placement;
    listStyle = [
      `left: ${left}px`,
      verticalRule,
      `width: ${width}px`,
      `max-height: ${optionsMaxHeight}px`,
    ].join('; ');
  }

  function handleDocumentMouseDown(event) {
    if (!isOpen) {
      return;
    }

    // The list is portaled out of `rootElement`, so check both.
    if (
      rootElement?.contains(event.target) ||
      listElement?.contains(event.target)
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

  function enabledOptions() {
    return normalizedOptions.filter((option) => !option.disabled);
  }

  function setInitialActiveOption(target) {
    const availableOptions = enabledOptions();
    if (availableOptions.length === 0) {
      activeOptionValue = null;
      return;
    }
    if (target === 'last') {
      activeOptionValue = availableOptions.at(-1).value;
      return;
    }
    const selectedEnabled = availableOptions.find(
      (option) => option.value === value,
    );
    activeOptionValue = (selectedEnabled ?? availableOptions[0]).value;
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

  function isTypeaheadKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey) {
      return false;
    }
    if (event.key.length !== 1) {
      return false;
    }
    // A space continues a search still inside its typeahead window;
    // otherwise it selects the active option.
    return event.key !== ' ' || typeaheadActive();
  }

  function typeaheadActive() {
    return (
      typeaheadQuery !== '' && Date.now() - typeaheadAt <= TYPEAHEAD_RESET_MS
    );
  }

  // Standard listbox typeahead: move to the next enabled option whose label
  // starts with the typed characters. Repeating one character cycles through
  // the options starting with it.
  function typeaheadMatch(availableOptions, currentIndex, key) {
    const now = Date.now();
    typeaheadQuery =
      now - typeaheadAt > TYPEAHEAD_RESET_MS
        ? key.toLowerCase()
        : `${typeaheadQuery}${key.toLowerCase()}`;
    typeaheadAt = now;
    const repeatedCharacter = [...typeaheadQuery].every(
      (character) => character === typeaheadQuery[0],
    );
    const query = repeatedCharacter ? typeaheadQuery[0] : typeaheadQuery;
    const startOffset = repeatedCharacter || currentIndex < 0 ? 1 : 0;
    for (let step = 0; step < availableOptions.length; step += 1) {
      const option =
        availableOptions[
          (currentIndex + startOffset + step + availableOptions.length) %
            availableOptions.length
        ];
      if (String(option.label).toLowerCase().startsWith(query)) {
        return option;
      }
    }
    return null;
  }

  async function setActiveOption(option) {
    activeOptionValue = option.value;
    await tick();
    document
      .getElementById(activeOptionId)
      ?.scrollIntoView?.({ block: 'nearest' });
  }

  function handleListKeyDown(event) {
    const availableOptions = enabledOptions();
    const currentIndex = availableOptions.findIndex(
      (option) => option.value === activeOptionValue,
    );
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
    if (isTypeaheadKey(event)) {
      event.preventDefault();
      const match = typeaheadMatch(availableOptions, currentIndex, event.key);
      if (match) {
        setActiveOption(match);
      }
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      const activeOption = availableOptions[currentIndex];
      if (activeOption) {
        event.preventDefault();
        selectOption(activeOption);
        triggerElement?.focus();
      }
      return;
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      return;
    }
    if (availableOptions.length === 0) {
      return;
    }
    event.preventDefault();
    typeaheadQuery = '';
    let nextIndex;
    if (event.key === 'Home') {
      nextIndex = 0;
    } else if (event.key === 'End') {
      nextIndex = availableOptions.length - 1;
    } else if (event.key === 'ArrowDown') {
      nextIndex =
        (currentIndex + 1 + availableOptions.length) % availableOptions.length;
    } else {
      nextIndex =
        (currentIndex - 1 + availableOptions.length) % availableOptions.length;
    }
    setActiveOption(availableOptions[nextIndex]);
  }

  function handleWindowResize() {
    updateListPosition();
  }

  function handleWindowScroll(event) {
    if (!isOpen) {
      return;
    }

    if (event.target instanceof Node && listElement?.contains(event.target)) {
      return;
    }

    close();
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
  class="dropdown dropdown-primitive {triggerClass}"
  class:open={isOpen}
  data-state={isOpen ? 'open' : 'closed'}
>
  {#if name}
    <input type="hidden" {name} {value} />
  {/if}

  <button
    bind:this={triggerElement}
    {id}
    class="dropdown-trigger dropdown-primitive__trigger"
    type="button"
    {disabled}
    aria-label={ariaLabel || placeholder}
    aria-describedby={ariaDescribedby}
    aria-haspopup="listbox"
    aria-expanded={isOpen}
    aria-controls={isOpen ? listboxId : undefined}
    use:tooltip={triggerTooltip}
    onclick={toggleOpen}
    onkeydown={handleTriggerKeyDown}
  >
    {#if selectedOption?.statusDot}
      <span
        class="dropdown-status-dot tab-indicator tab-indicator--{selectedOption.statusDot}"
        aria-hidden="true"
      ></span>
    {/if}
    <span
      class="dropdown-primitive__trigger-label"
      class:dropdown-primitive__trigger-label--placeholder={!hasSelection}
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
      bind:this={listElement}
      use:portal
      class="dropdown-list dropdown-primitive__list {listClass}"
      id={listboxId}
      role="listbox"
      tabindex="0"
      aria-activedescendant={activeOptionId}
      data-placement={listPlacement}
      data-positioning="fixed"
      style={listStyle}
      onkeydown={handleListKeyDown}
    >
      {#each normalizedOptions as option, optionIndex (option.value)}
        <button
          class="dropdown-option dropdown-primitive__option"
          class:selected={option.value === value}
          class:active={option.value === activeOptionValue}
          id={`${listboxId}-option-${optionIndex}`}
          type="button"
          role="option"
          tabindex="-1"
          disabled={option.disabled}
          aria-label={option.ariaLabel || undefined}
          aria-selected={option.value === value}
          onclick={() => selectOption(option)}
        >
          {#if option.statusDot}
            <span
              class="dropdown-status-dot tab-indicator tab-indicator--{option.statusDot}"
              aria-hidden="true"
            ></span>
          {/if}
          <span class="dropdown-primitive__option-label">{option.label}</span>
          {#if option.secondaryLabel}
            <span class="dropdown-primitive__option-meta">
              {option.secondaryLabel}
            </span>
          {/if}
          {#if option.badge}
            <span class="count-badge">{option.badge}</span>
          {/if}
        </button>
      {/each}
    </div>
  {/if}
</div>
