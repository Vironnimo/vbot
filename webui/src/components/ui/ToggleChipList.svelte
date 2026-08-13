<script>
  // Shared compact allow-list for Project tools and skills. Renders each item as a
  // toggle chip (the chip itself is the on/off control — accent-filled = allowed)
  // in a wrapping cloud, with an always-present toolbar: a live search filter, an
  // "on / total" tally, and "all on" / "all off" bulk actions. The item's
  // description (plus any not-ready hint or skill warnings) shows on plain hover.
  //
  // Used by the Project Tool- and Skill-Whitelist editors and the Agent Skill
  // editor, so those surfaces scale to large, ever-changing lists (skills can
  // grow past 100). Tool rows can optionally be grouped by registry family.
  //
  // Item shape: { name, allowed, family?, description?, ready?, readiness_hint?,
  // extension?, warnings? }. The chip is a plain toggle; wildcard/ceiling
  // semantics stay in the caller (it decides each item's `allowed` and handles
  // the toggle/set-all callbacks).

  import Button from './Button.svelte';
  import ToolReadinessNotice from './ToolReadinessNotice.svelte';

  import { countAllowed, filterChipsByQuery } from '$lib/accessChips.js';
  import { t } from '$lib/i18n.js';
  import { floatingHoverCard } from '$lib/tooltip.js';

  const noop = () => {};

  let {
    items = [],
    disabled = false,
    searchPlaceholder = '',
    emptyLabel = '',
    note = '',
    grouped = false,
    groupLabel = (family) => family ?? '',
    ariaToggleLabel = (name) => t('access.toggle', 'Toggle {name}', { name }),
    onToggle = noop,
    onSetAll = noop,
    onOpenExtensions = noop,
    headerActions = undefined,
  } = $props();

  let query = $state('');

  let normalizedItems = $derived(Array.isArray(items) ? items : []);
  // Locked items (e.g. the memory tool, shown as a display-only "auto" chip) are
  // chips but never toggles — excluded from the on/total tally and all-on/all-off.
  let toggleableItems = $derived(
    normalizedItems.filter((item) => !item?.locked),
  );
  let totalCount = $derived(toggleableItems.length);
  let onCount = $derived(countAllowed(toggleableItems));
  let visibleItems = $derived(filterChipsByQuery(normalizedItems, query));
  let visibleGroups = $derived(groupVisibleItems());
  let placeholder = $derived(
    searchPlaceholder || t('access.searchPlaceholder', 'Filter…'),
  );

  function attentionNeeded(item) {
    return item?.ready === false || (item?.warnings?.length ?? 0) > 0;
  }

  function groupVisibleItems() {
    if (!grouped) {
      return [{ family: null, items: visibleItems }];
    }
    const familyCounts = Object.create(null);
    for (const item of normalizedItems) {
      if (!item?.family) continue;
      familyCounts[item.family] = (familyCounts[item.family] ?? 0) + 1;
    }
    const groups = [];
    for (const item of visibleItems) {
      const family =
        item?.family && familyCounts[item.family] >= 2 ? item.family : null;
      const group = groups.find((candidate) => candidate.family === family);
      if (group) {
        group.items.push(item);
      } else {
        groups.push({ family, items: [item] });
      }
    }
    return groups;
  }

  // A chip gets a hover card only when there is something to show — its
  // description, a locked-state note, a not-ready hint, or skill warnings.
  function hasTip(item) {
    return (
      Boolean(item?.description) ||
      Boolean(item?.lockedNote) ||
      attentionNeeded(item)
    );
  }
</script>

{#if normalizedItems.length === 0}
  <p class="access-chips__empty">{emptyLabel}</p>
{:else}
  <div class="access-chips">
    <div class="access-chips__toolbar">
      <label class="access-chips__search">
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          aria-hidden="true"
        >
          <circle cx="11" cy="11" r="7" />
          <line x1="16.5" y1="16.5" x2="21" y2="21" />
        </svg>
        <input
          class="access-chips__search-input"
          type="text"
          value={query}
          {placeholder}
          aria-label={placeholder}
          {disabled}
          oninput={(event) => (query = event.currentTarget.value)}
        />
      </label>
      <span class="access-chips__count">
        {t('access.count', '{on} / {total} on', {
          on: onCount,
          total: totalCount,
        })}
      </span>
      <div class="access-chips__actions">
        <Button variant="tertiary" {disabled} onClick={() => onSetAll(true)}>
          {t('access.allOn', 'all on')}
        </Button>
        <Button variant="tertiary" {disabled} onClick={() => onSetAll(false)}>
          {t('access.allOff', 'all off')}
        </Button>
        {@render headerActions?.()}
      </div>
    </div>

    {#if note}
      <p class="access-chips__note">{note}</p>
    {/if}

    {#if visibleItems.length === 0}
      <p class="access-chips__empty">{t('access.noMatches', 'No matches.')}</p>
    {:else}
      <div class="access-chips__groups">
        {#each visibleGroups as group (group.family ?? 'individual')}
          <section class="access-chips__group">
            {#if grouped}
              <h4 class="access-chips__group-title">
                {groupLabel(group.family, group.items)}
              </h4>
            {/if}
            <div class="access-chips__cloud">
              {#each group.items as item (item.name)}
                <div class="access-chip-wrap">
                  {#if item.locked}
                    <div
                      class="access-chip access-chip--locked"
                      class:is-on={item.allowed}
                    >
                      <span class="access-chip__name">{item.name}</span>
                      <span class="access-chip__auto">
                        {item.lockedLabel ?? t('access.lockedAuto', 'auto')}
                      </span>
                    </div>
                  {:else}
                    <button
                      type="button"
                      class="access-chip"
                      class:is-on={item.allowed}
                      class:is-attention={attentionNeeded(item)}
                      role="switch"
                      aria-checked={item.allowed}
                      aria-label={ariaToggleLabel(item.name)}
                      {disabled}
                      onclick={() => onToggle(item.name, !item.allowed)}
                    >
                      <span class="access-chip__name">{item.name}</span>
                      {#if attentionNeeded(item)}
                        <span class="access-chip__dot" aria-hidden="true"
                        ></span>
                      {/if}
                    </button>
                  {/if}
                  {#if hasTip(item)}
                    <div
                      class="access-chip__tip"
                      role="tooltip"
                      use:floatingHoverCard
                    >
                      {#if item.description}
                        <p class="access-chip__desc">{item.description}</p>
                      {/if}
                      {#if item.lockedNote}
                        <p
                          class="access-chip__locked-note"
                          data-testid="access-chip-locked-note"
                        >
                          {item.lockedNote}
                        </p>
                      {/if}
                      <ToolReadinessNotice
                        ready={item.ready}
                        readinessHint={item.readiness_hint}
                        extension={item.extension}
                        {onOpenExtensions}
                      />
                      {#if item.warnings?.length}
                        <div class="access-chip__warnings">
                          <span class="access-chip__warnings-label">
                            {t('agents.access.skillWarnings', 'Warnings')}
                          </span>
                          <ul>
                            {#each item.warnings as warning, index (`${item.name}-warning-${index}`)}
                              <li>{warning}</li>
                            {/each}
                          </ul>
                        </div>
                      {/if}
                    </div>
                  {/if}
                </div>
              {/each}
            </div>
          </section>
        {/each}
      </div>
    {/if}
  </div>
{/if}

<style>
  .access-chips {
    display: flex;
    flex-direction: column;
  }

  .access-chips__toolbar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    padding: 8px 16px 10px;
  }

  .access-chips__search {
    display: flex;
    flex: 1;
    min-width: 150px;
    align-items: center;
    gap: 7px;
    padding: 6px 10px;
    background: var(--surface-2);
    border: 1px solid var(--border-2);
    border-radius: 6px;
    color: var(--text-lo);
  }

  .access-chips__search:focus-within {
    border-color: var(--accent-40);
    box-shadow: var(--focus-ring);
  }

  .access-chips__search-input {
    flex: 1;
    min-width: 40px;
    border: 0;
    background: transparent;
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: 12px;
    outline: none;
  }

  .access-chips__search-input::placeholder {
    color: var(--text-lo);
  }

  .access-chips__count {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 11px;
    white-space: nowrap;
  }

  .access-chips__actions {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .access-chips__note,
  .access-chips__empty {
    margin: 0;
    padding: 0 16px 10px;
    color: var(--text-lo);
    font-size: 11.5px;
    line-height: 1.4;
  }

  .access-chips__empty {
    padding: 4px 16px 12px;
    font-style: italic;
  }

  .access-chips__groups {
    display: grid;
    gap: 10px;
    padding: 2px 16px 14px;
  }

  .access-chips__group-title {
    margin: 0 0 6px;
    color: var(--text-med);
    font-size: var(--fs-label-sm);
  }

  .access-chips__cloud {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .access-chip-wrap {
    position: relative;
  }

  .access-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 11px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: transparent;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 12px;
    cursor: pointer;
    transition:
      background 0.1s,
      border-color 0.1s,
      color 0.1s;
  }

  /* Hover feedback is for the interactive (button) chips only — the locked
     display-only chip (a div) must never brighten. */
  button.access-chip:hover:not(:disabled) {
    border-color: var(--border-2);
    color: var(--text-med);
  }

  .access-chip.is-on {
    color: var(--accent);
    background: var(--accent-10);
    border-color: var(--accent-30);
  }

  button.access-chip.is-on:hover:not(:disabled) {
    background: var(--accent-16);
    border-color: rgba(232, 135, 10, 0.42);
    color: var(--accent);
  }

  .access-chip:disabled {
    cursor: default;
    opacity: 0.55;
  }

  .access-chip.is-attention {
    border-style: dashed;
  }

  .access-chip__dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--amber);
  }

  /* Locked chip (e.g. memory): display-only, dashed, with an "auto" tag — never a
     toggle. It keeps only the faint on-tint (background/border) but stays MUTED
     (grey text, dim tag), so it reads as display-only, not as an active accent
     toggle. The two-class selector beats `.access-chip.is-on`, which would
     otherwise force the accent text color on. */
  .access-chip--locked {
    cursor: default;
    border-style: dashed;
  }

  .access-chip.access-chip--locked {
    color: var(--text-med);
  }

  .access-chip__auto {
    font-family: var(--font-mono);
    font-size: 9.5px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--text-lo);
  }

  .access-chip__locked-note {
    margin: 6px 0 0;
    color: var(--text-med);
    font-size: 11.5px;
    line-height: 1.4;
  }

  /* Structured hover cards are fixed and portaled to <body> by
     `floatingHoverCard`, so no ancestor overflow or stacking context can clip
     them. */
  .access-chip__tip {
    position: fixed;
    z-index: var(--z-floating);
    width: max-content;
    max-width: min(320px, calc(100vw - 16px));
    max-height: min(340px, 50vh);
    padding: 9px 11px;
    overflow: auto;
    background: var(--surface-3);
    border: 1px solid var(--border-2);
    border-radius: 6px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
    opacity: 0;
    visibility: hidden;
    pointer-events: none;
    transition:
      opacity 0.1s,
      visibility 0.1s;
  }

  .access-chip__tip:global([data-floating-open='true']) {
    opacity: 1;
    visibility: visible;
    pointer-events: auto;
  }

  .access-chip__desc {
    margin: 0;
    color: var(--text-hi);
    font-size: 12px;
    line-height: 1.45;
  }

  .access-chip__warnings {
    margin-top: 7px;
    color: var(--amber);
    font-size: 11.5px;
    line-height: 1.4;
  }

  .access-chip__warnings-label {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .access-chip__warnings ul {
    margin: 3px 0 0;
    padding-left: 16px;
  }
</style>
