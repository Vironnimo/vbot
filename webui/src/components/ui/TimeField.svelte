<script>
  // Time field: a text field that shows its time in the app language
  // ("9:30 AM", or "09:30" in a 24-hour language), reads typed times ("9",
  // "930", "9:30", "9:30 pm"; lib/dateTimeFields.js) and offers a list of
  // times every `step` minutes, opened on the time it holds. The value is a
  // wall-clock time "HH:MM" ('' when empty); `onChange(next)` reports each new
  // time as soon as the typed text reads as one. `required` keeps the last
  // time when the text is cleared.
  //
  // Focus stays in the field: ArrowDown/ArrowUp open the list and move
  // through it, Enter picks the highlighted time, Escape closes. Other props
  // (`id`, `aria-*`, `onkeydown`) reach the input.
  import { tick } from 'svelte';

  import {
    formatTimeValue,
    nearestTimeOption,
    parseTimeText,
    timeOptions,
  } from '$lib/dateTimeFields.js';
  import { computePanelPosition, portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';
  import Button from './Button.svelte';
  import TextField from './TextField.svelte';

  const noop = () => {};
  const componentId = $props.id();
  const PANEL_MIN_WIDTH = 112;

  let {
    value = '',
    onChange = noop,
    step = 30,
    required = false,
    id = '',
    variant = 'default',
    placeholder = '',
    disabled = false,
    invalid = false,
    ariaLabel = '',
    class: className = '',
    onkeydown: callerKeydown = undefined,
    onblur: callerBlur = undefined,
    ...rest
  } = $props();

  let rootElement = $state();
  let listElement = $state();
  let open = $state(false);
  let listStyle = $state('');
  let activeIndex = $state(-1);
  // Typed text while the field is being edited; null shows the value.
  let draft = $state(null);

  let options = $derived(timeOptions(step));
  let displayText = $derived(draft ?? formatTimeValue(value));
  let listboxId = $derived(`${id || componentId}-times`);
  let fieldClass = $derived(
    ['time-field', className].filter(Boolean).join(' '),
  );

  const optionId = (index) => `${listboxId}-${index}`;

  function inputElement() {
    return rootElement?.querySelector('input') ?? null;
  }

  // Scrolls only the list: the page behind it must not move.
  function scrollToOption(index, block = 'nearest') {
    const option = document.getElementById(optionId(index));
    if (index < 0 || !listElement || !option) return;
    const top = option.offsetTop;
    const bottom = top + option.offsetHeight;
    const height = listElement.clientHeight;
    if (block === 'center') {
      listElement.scrollTop = top - (height - option.offsetHeight) / 2;
    } else if (top < listElement.scrollTop) {
      listElement.scrollTop = top;
    } else if (bottom > listElement.scrollTop + height) {
      listElement.scrollTop = bottom - height;
    }
  }

  async function openList({ activate = false } = {}) {
    if (disabled || open) return;
    open = true;
    const anchor = nearestTimeOption(options, value);
    const index = anchor ? options.indexOf(anchor) : -1;
    activeIndex = activate ? Math.max(index, 0) : -1;
    await tick();
    positionList();
    // The list opens on the field's time, not at midnight.
    scrollToOption(index >= 0 ? index : options.indexOf('09:00'), 'center');
  }

  function closeList() {
    if (!open) return;
    open = false;
    activeIndex = -1;
    listStyle = '';
  }

  function positionList() {
    const input = inputElement();
    if (!open || !input || !listElement) return;
    const { left, width, verticalRule, optionsMaxHeight } =
      computePanelPosition(input, {
        contentHeight: listElement.scrollHeight,
        minWidth: PANEL_MIN_WIDTH,
      });
    listStyle = [
      `left: ${left}px`,
      verticalRule,
      `width: ${width}px`,
      `max-height: ${optionsMaxHeight}px`,
    ].join('; ');
  }

  function choose(option) {
    draft = null;
    if (option !== value) onChange(option);
    closeList();
  }

  function moveActive(delta) {
    const count = options.length;
    activeIndex =
      activeIndex < 0
        ? Math.max(options.indexOf(nearestTimeOption(options, value)), 0)
        : (activeIndex + delta + count) % count;
    void tick().then(() => scrollToOption(activeIndex));
  }

  function handleInput(text) {
    draft = text;
    activeIndex = -1;
    const parsed = parseTimeText(text);
    if (parsed === '') {
      if (!required && value !== '') onChange('');
      return;
    }
    if (parsed) {
      if (parsed !== value) onChange(parsed);
      if (open) {
        const near = nearestTimeOption(options, parsed);
        void tick().then(() => scrollToOption(options.indexOf(near), 'center'));
      }
    }
  }

  function handleKeydown(event) {
    if (event.isComposing || event.ctrlKey || event.metaKey) {
      callerKeydown?.(event);
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!open) {
        void openList({ activate: true });
      } else {
        moveActive(event.key === 'ArrowDown' ? 1 : -1);
      }
      return;
    }
    if (event.key === 'Escape' && open) {
      // Also tells an enclosing Modal or card that Escape closed only this.
      event.preventDefault();
      closeList();
      return;
    }
    if (event.key === 'Enter') {
      if (open && activeIndex >= 0) {
        event.preventDefault();
        choose(options[activeIndex]);
        return;
      }
      // The typed time is already the value; show it in the field's format.
      draft = null;
      if (open) {
        event.preventDefault();
        closeList();
        return;
      }
    }
    callerKeydown?.(event);
  }

  function handleBlur(event) {
    // Text that is no time gives way to the value it left untouched.
    draft = null;
    closeList();
    callerBlur?.(event);
  }

  function handleDocumentMouseDown(event) {
    if (!open) return;
    const target = event.target;
    if (
      target instanceof Node &&
      (rootElement?.contains(target) || listElement?.contains(target))
    ) {
      return;
    }
    closeList();
  }

  function handleWindowScroll(event) {
    if (event.target instanceof Node && listElement?.contains(event.target)) {
      return;
    }
    closeList();
  }

  function toggleList() {
    if (open) {
      closeList();
      return;
    }
    inputElement()?.focus();
    void openList();
  }

  $effect(() => {
    if (disabled) closeList();
  });

  $effect(() => {
    if (!open) return undefined;
    window.addEventListener('scroll', handleWindowScroll, true);
    return () => window.removeEventListener('scroll', handleWindowScroll, true);
  });
</script>

<svelte:document onmousedown={handleDocumentMouseDown} />
<svelte:window onresize={positionList} />

<div bind:this={rootElement} class={fieldClass}>
  <TextField
    {...rest}
    id={id || undefined}
    {variant}
    value={displayText}
    {placeholder}
    {disabled}
    {invalid}
    {ariaLabel}
    class="time-field__input"
    role="combobox"
    autocomplete="off"
    spellcheck="false"
    inputmode="text"
    aria-autocomplete="none"
    aria-expanded={open}
    aria-controls={open ? listboxId : undefined}
    aria-activedescendant={open && activeIndex >= 0
      ? optionId(activeIndex)
      : undefined}
    onInput={handleInput}
    onclick={() => openList()}
    onkeydown={handleKeydown}
    onblur={handleBlur}
  />
  <Button
    variant="tertiary"
    icon
    class="time-field__toggle"
    tabindex="-1"
    ariaLabel={t('timeField.choose')}
    tooltip={t('timeField.choose')}
    {disabled}
    onmousedown={(event) => event.preventDefault()}
    onClick={toggleList}
  >
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
      <circle
        cx="8"
        cy="8"
        r="5.9"
        fill="none"
        stroke="currentColor"
        stroke-width="1.3"
      />
      <path
        d="M8 4.75V8l2.25 1.5"
        fill="none"
        stroke="currentColor"
        stroke-width="1.3"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    </svg>
  </Button>
</div>

{#if open}
  <!-- Pressing inside the list must not take focus from the field. -->
  <div
    bind:this={listElement}
    use:portal
    id={listboxId}
    class="dropdown-list time-field__list"
    role="listbox"
    tabindex="-1"
    aria-label={t('timeField.options')}
    data-positioning="fixed"
    style={listStyle}
    onmousedown={(event) => event.preventDefault()}
  >
    {#each options as option, index (option)}
      <button
        id={optionId(index)}
        type="button"
        tabindex="-1"
        class="dropdown-option time-field__option"
        class:selected={option === value}
        class:active={index === activeIndex}
        role="option"
        aria-selected={option === value}
        onmouseenter={() => (activeIndex = index)}
        onclick={() => choose(option)}
      >
        {formatTimeValue(option)}
      </button>
    {/each}
  </div>
{/if}
