<script>
  // Date field: a text field that shows its date in the app language
  // ("Wed, Sep 23, 2026"), reads typed dates (lib/dateTimeFields.js) and opens
  // a month picker. The value is a wall-clock date "YYYY-MM-DD" ('' when
  // empty) with no time zone; `onChange(next)` reports each new date as soon
  // as the typed text reads as one. `required` keeps the last date when the
  // text is cleared. `min`/`max` (dates, inclusive) bound typed and picked
  // dates. `today` marks the current day in the picker; it defaults to today
  // in the application time zone.
  //
  // Keys: ArrowDown (or Alt+ArrowDown) opens the picker and moves into its
  // days; there the arrows move by day and week, PageUp/PageDown by month,
  // Home/End to the week's ends, Enter or Space picks, Escape closes. Other
  // props (`id`, `aria-*`, `onkeydown`) reach the input.
  import { tick } from 'svelte';

  import { dateKeyInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
  import {
    addDays,
    addMonths,
    dateInRange,
    formatDateLong,
    formatDateValue,
    formatMonthTitle,
    isDateValue,
    monthGrid,
    parseDateText,
    weekdayNames,
  } from '$lib/dateTimeFields.js';
  import { computePanelPosition, portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';
  import Button from './Button.svelte';
  import TextField from './TextField.svelte';

  const noop = () => {};
  const componentId = $props.id();
  const PANEL_WIDTH = 272;

  let {
    value = '',
    onChange = noop,
    min = '',
    max = '',
    today = '',
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
  let panelElement = $state();
  let open = $state(false);
  let panelStyle = $state('');
  let panelSide = $state('bottom');
  // The day the picker's keyboard focus rests on; its month is the one shown.
  let focusDay = $state('');
  // Typed text while the field is being edited; null shows the value.
  let draft = $state(null);

  let currentDay = $derived(today || dateKeyInApplicationZone());
  let displayText = $derived(draft ?? formatDateValue(value));
  let shownDay = $derived(
    focusDay || (isDateValue(value) ? value : currentDay),
  );
  let days = $derived(monthGrid(shownDay));
  let weeks = $derived(
    Array.from({ length: 6 }, (_, week) => days.slice(week * 7, week * 7 + 7)),
  );
  let weekdays = $derived(weekdayNames());
  let panelId = $derived(`${id || componentId}-picker`);
  let titleId = $derived(`${panelId}-title`);
  let fieldClass = $derived(
    ['date-field', className].filter(Boolean).join(' '),
  );

  const dayId = (day) => `${panelId}-${day}`;

  function inputElement() {
    return rootElement?.querySelector('input') ?? null;
  }

  function inRange(day) {
    return dateInRange(day, { min, max });
  }

  // The picker starts on the field's date, else on today kept within bounds.
  function startDay() {
    if (isDateValue(value)) return value;
    if (min && currentDay < min) return min;
    if (max && currentDay > max) return max;
    return currentDay;
  }

  async function openPicker({ focusGrid = false } = {}) {
    if (disabled) return;
    if (!open) {
      focusDay = startDay();
      open = true;
      await tick();
      positionPanel();
    }
    if (focusGrid) focusActiveDay();
  }

  function closePicker({ restoreFocus = false } = {}) {
    if (!open) return;
    open = false;
    focusDay = '';
    panelStyle = '';
    if (restoreFocus) inputElement()?.focus();
  }

  function positionPanel() {
    if (!open || !rootElement || !panelElement) return;
    const { placement, left, verticalRule } = computePanelPosition(
      rootElement,
      {
        contentHeight: panelElement.scrollHeight,
        panelWidth: PANEL_WIDTH,
        maxHeight: 400,
      },
    );
    panelSide = placement;
    panelStyle = [
      `left: ${left}px`,
      verticalRule,
      `width: ${PANEL_WIDTH}px`,
    ].join('; ');
  }

  function focusActiveDay() {
    void tick().then(() => document.getElementById(dayId(focusDay))?.focus());
  }

  function choose(day) {
    if (!inRange(day)) return;
    draft = null;
    if (day !== value) onChange(day);
    closePicker({ restoreFocus: true });
  }

  function showMonth(step) {
    focusDay = addMonths(shownDay, step);
    const gridFocused = panelElement?.contains(document.activeElement);
    if (gridFocused) focusActiveDay();
  }

  function moveFocus(next) {
    focusDay = next;
    focusActiveDay();
  }

  function handleGridKeydown(event) {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const weekday = (days.findIndex((day) => day.value === focusDay) + 7) % 7;
    const moves = {
      ArrowLeft: () => addDays(focusDay, -1),
      ArrowRight: () => addDays(focusDay, 1),
      ArrowUp: () => addDays(focusDay, -7),
      ArrowDown: () => addDays(focusDay, 7),
      Home: () => addDays(focusDay, -weekday),
      End: () => addDays(focusDay, 6 - weekday),
      PageUp: () => addMonths(focusDay, event.shiftKey ? -12 : -1),
      PageDown: () => addMonths(focusDay, event.shiftKey ? 12 : 1),
    };
    if (moves[event.key]) {
      event.preventDefault();
      moveFocus(moves[event.key]());
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      choose(focusDay);
      return;
    }
    if (event.key === 'Escape') {
      // Also tells an enclosing Modal or card that Escape closed only this.
      event.preventDefault();
      closePicker({ restoreFocus: true });
      return;
    }
    if (event.key === 'Tab') {
      // Resume the Tab order at the field before the portaled panel leaves.
      closePicker({ restoreFocus: true });
    }
  }

  function handleInput(text) {
    draft = text;
    // A date without a year takes the shown date's, or today's in the app zone.
    const parsed = parseDateText(text, {
      referenceValue: isDateValue(value) ? value : currentDay,
    });
    if (parsed === '') {
      if (!required && value !== '') onChange('');
      return;
    }
    if (parsed && inRange(parsed)) {
      if (open) focusDay = parsed;
      if (parsed !== value) onChange(parsed);
    }
  }

  function handleKeydown(event) {
    if (
      event.key === 'ArrowDown' &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.isComposing
    ) {
      event.preventDefault();
      void openPicker({ focusGrid: true });
      return;
    }
    if (event.key === 'Escape' && open) {
      event.preventDefault();
      closePicker();
      return;
    }
    if (event.key === 'Enter') {
      // The typed date is already the value; show it in the field's format.
      draft = null;
      closePicker();
    }
    callerKeydown?.(event);
  }

  function handleBlur(event) {
    const next = event.relatedTarget;
    const movesIntoPicker =
      next instanceof Node && panelElement?.contains(next);
    if (!movesIntoPicker) {
      // Text that is no date gives way to the value it left untouched.
      draft = null;
      closePicker();
    }
    callerBlur?.(event);
  }

  function handleDocumentMouseDown(event) {
    if (!open) return;
    const target = event.target;
    if (
      target instanceof Node &&
      (rootElement?.contains(target) || panelElement?.contains(target))
    ) {
      return;
    }
    closePicker();
  }

  function handleWindowScroll(event) {
    if (event.target instanceof Node && panelElement?.contains(event.target)) {
      return;
    }
    closePicker();
  }

  function togglePicker() {
    if (open) {
      closePicker({ restoreFocus: true });
      return;
    }
    inputElement()?.focus();
    void openPicker();
  }

  $effect(() => {
    if (disabled) closePicker();
  });

  $effect(() => {
    if (!open) return undefined;
    window.addEventListener('scroll', handleWindowScroll, true);
    return () => window.removeEventListener('scroll', handleWindowScroll, true);
  });
</script>

<svelte:document onmousedown={handleDocumentMouseDown} />
<svelte:window onresize={positionPanel} />

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
    class="date-field__input"
    role="combobox"
    autocomplete="off"
    spellcheck="false"
    aria-haspopup="dialog"
    aria-expanded={open}
    aria-controls={open ? panelId : undefined}
    onInput={handleInput}
    onclick={() => openPicker()}
    onkeydown={handleKeydown}
    onblur={handleBlur}
  />
  <Button
    variant="tertiary"
    icon
    class="date-field__toggle"
    tabindex="-1"
    ariaLabel={t('dateField.choose')}
    tooltip={t('dateField.choose')}
    {disabled}
    onmousedown={(event) => event.preventDefault()}
    onClick={togglePicker}
  >
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
      <rect
        x="2.25"
        y="3.25"
        width="11.5"
        height="10.5"
        rx="1.75"
        fill="none"
        stroke="currentColor"
        stroke-width="1.3"
      />
      <path
        d="M2.5 6.5h11M5.5 1.75v2.5M10.5 1.75v2.5"
        fill="none"
        stroke="currentColor"
        stroke-width="1.3"
        stroke-linecap="round"
      />
    </svg>
  </Button>
</div>

{#if open}
  <!-- Pressing inside the picker keeps focus where it is (field or day). -->
  <div
    bind:this={panelElement}
    use:portal
    id={panelId}
    class="date-field__panel"
    role="dialog"
    aria-labelledby={titleId}
    data-positioning="fixed"
    data-floating-side={panelSide}
    style={panelStyle}
    tabindex="-1"
    onmousedown={(event) => event.preventDefault()}
  >
    <div class="date-field__header">
      <Button
        variant="tertiary"
        icon
        class="date-field__step"
        tabindex="-1"
        ariaLabel={t('dateField.previousMonth')}
        tooltip={t('dateField.previousMonth')}
        onClick={() => showMonth(-1)}
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path
            d="M10 3.5 5.5 8l4.5 4.5"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
      </Button>
      <span id={titleId} class="date-field__title" aria-live="polite"
        >{formatMonthTitle(shownDay)}</span
      >
      <Button
        variant="tertiary"
        icon
        class="date-field__step"
        tabindex="-1"
        ariaLabel={t('dateField.nextMonth')}
        tooltip={t('dateField.nextMonth')}
        onClick={() => showMonth(1)}
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path
            d="M6 3.5 10.5 8 6 12.5"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
      </Button>
    </div>
    <div
      class="date-field__grid"
      role="grid"
      aria-labelledby={titleId}
      tabindex="-1"
      onkeydown={handleGridKeydown}
    >
      <div class="date-field__row" role="row">
        {#each weekdays as weekday (weekday)}
          <span class="date-field__weekday" role="columnheader">{weekday}</span>
        {/each}
      </div>
      {#each weeks as week (week[0].value)}
        <div class="date-field__row" role="row">
          {#each week as day (day.value)}
            <button
              id={dayId(day.value)}
              type="button"
              class="date-field__day"
              class:date-field__day--outside={!day.inMonth}
              class:date-field__day--today={day.value === currentDay}
              class:date-field__day--selected={day.value === value}
              role="gridcell"
              tabindex={day.value === shownDay ? 0 : -1}
              disabled={!inRange(day.value)}
              aria-selected={day.value === value}
              aria-current={day.value === currentDay ? 'date' : undefined}
              aria-label={formatDateLong(day.value)}
              onclick={() => choose(day.value)}
            >
              {day.day}
            </button>
          {/each}
        </div>
      {/each}
    </div>
  </div>
{/if}
