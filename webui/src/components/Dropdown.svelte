<script>
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
  let isOpen = $state(false);

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

  function setOpen(nextOpen) {
    if (disabled) {
      return;
    }

    isOpen = nextOpen;
    onOpenChange(nextOpen);
  }

  function toggleOpen() {
    setOpen(!isOpen);
  }

  function close() {
    if (!isOpen) {
      return;
    }

    isOpen = false;
    onOpenChange(false);
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

  function selectOption(option) {
    if (option.disabled) {
      return;
    }

    onValueChange(option.value, option);
    close();
  }
</script>

<svelte:document
  onmousedown={handleDocumentMouseDown}
  onkeydown={handleDocumentKeyDown}
/>

<div
  bind:this={rootElement}
  class="dropdown dropdown-primitive {triggerClass}"
  class:open={isOpen}
>
  {#if name}
    <input type="hidden" {name} {value} />
  {/if}

  <button
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
    <svg class="dropdown-chevron" viewBox="0 0 12 12" aria-hidden="true">
      <path d="M2 4l4 4 4-4" />
    </svg>
  </button>

  <div
    class="dropdown-list dropdown-primitive__list {listClass}"
    role="listbox"
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
</div>
