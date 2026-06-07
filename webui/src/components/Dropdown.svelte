<script>
  import { tick } from 'svelte';

  import { computePanelPosition, portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let {
    id = '',
    name = '',
    value = '',
    options = [],
    placeholder = t('dropdown.placeholder', 'Select an option'),
    disabled = false,
    ariaLabel = '',
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

  let normalizedOptions = $derived(normalizeOptions(options));
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

  async function open() {
    if (disabled) {
      return;
    }

    isOpen = true;
    onOpenChange(true);
    await tick();
    updateListPosition();
  }

  function close() {
    if (!isOpen) {
      return;
    }

    isOpen = false;
    listStyle = '';
    listPlacement = 'bottom';
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
    if (!isOpen || !triggerElement) {
      return;
    }

    const { placement, left, width, verticalRule } =
      computePanelPosition(triggerElement);

    listPlacement = placement;
    listStyle = [`left: ${left}px`, verticalRule, `width: ${width}px`].join(
      '; ',
    );
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
    aria-haspopup="listbox"
    aria-expanded={isOpen}
    onclick={toggleOpen}
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
      role="listbox"
      data-placement={listPlacement}
      data-positioning="fixed"
      style={listStyle}
    >
      {#each normalizedOptions as option (option.value)}
        <button
          class="dropdown-option dropdown-primitive__option"
          class:selected={option.value === value}
          type="button"
          role="option"
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
