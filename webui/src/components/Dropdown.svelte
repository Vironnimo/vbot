<script>
  import { tick } from 'svelte';

  import { computePanelPosition, portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';

  const noop = () => {};
  const componentId = $props.id();

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
  let activeOptionValue = $state('');

  let normalizedOptions = $derived(normalizeOptions(options));
  let selectedOption = $derived(
    normalizedOptions.find((option) => option.value === value) ?? null,
  );
  let triggerLabel = $derived(selectedOption?.label || placeholder);
  let hasSelection = $derived(Boolean(selectedOption));
  let listboxId = $derived(id ? `${id}-listbox` : `${componentId}-listbox`);
  let activeOptionId = $derived(
    activeOptionValue
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
        };
      }

      return {
        value: option?.value ?? '',
        label: option?.label ?? option?.value ?? '',
        disabled: Boolean(option?.disabled),
        secondaryLabel: option?.secondaryLabel ?? '',
      };
    });
  }

  async function open({ focus = '' } = {}) {
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
    activeOptionValue = '';
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
      activeOptionValue = '';
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
    activeOptionValue = availableOptions[nextIndex].value;
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
    onclick={toggleOpen}
    onkeydown={handleTriggerKeyDown}
  >
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
          aria-selected={option.value === value}
          onclick={() => selectOption(option)}
        >
          <span class="dropdown-primitive__option-label">{option.label}</span>
          {#if option.secondaryLabel}
            <span class="dropdown-primitive__option-meta">
              {option.secondaryLabel}
            </span>
          {/if}
        </button>
      {/each}
    </div>
  {/if}
</div>
