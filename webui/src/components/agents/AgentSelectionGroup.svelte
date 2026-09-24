<script>
  // One allow-list of the Agent editor (Skills, Identity Agents, Project
  // Agents) as a shared Checkbox group (styles/settings/sections.css): a head
  // with the group checkbox, the title and the selection count, then one
  // labelled checkbox row per member. The caller
  // owns the selection policy (wildcards and what selecting a whole group
  // means) and the filter text shared by the groups of a section.
  import Checkbox from '../ui/Checkbox.svelte';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let {
    title = '',
    titleId,
    items = [],
    query = '',
    allLabel = '',
    toggleLabel = (name) => name,
    emptyLabel = '',
    collapsible = false,
    open = true,
    toggleId = undefined,
    contentId = undefined,
    onOpenChange = noop,
    onToggle = noop,
    onSetAll = noop,
    class: className = '',
  } = $props();

  let selectedCount = $derived(items.filter((item) => item.allowed).length);
  let groupState = $derived(
    items.length > 0 && selectedCount === items.length
      ? 'on'
      : selectedCount > 0
        ? 'mixed'
        : 'off',
  );
  let visibleItems = $derived(filterItems(items, query));

  function filterItems(list, text) {
    const needle = text.trim().toLocaleLowerCase();
    if (!needle) return list;
    return list.filter((item) =>
      [item.name, item.detail]
        .filter(Boolean)
        .join(' ')
        .toLocaleLowerCase()
        .includes(needle),
    );
  }
</script>

<section
  class={['s-group', 's-check-group', className].filter(Boolean).join(' ')}
  aria-labelledby={titleId}
>
  <div class="s-check-group__head">
    {#if items.length > 0}
      <Checkbox
        checked={groupState === 'on'}
        indeterminate={groupState === 'mixed'}
        ariaLabel={allLabel}
        onChange={onSetAll}
      />
    {/if}
    {#if collapsible}
      <button
        type="button"
        class="agents-selection__toggle"
        id={toggleId}
        aria-expanded={open}
        aria-controls={contentId}
        onclick={() => onOpenChange(!open)}
      >
        <span
          class="disclosure-chevron"
          class:disclosure-chevron--open={open}
          aria-hidden="true"
        ></span>
        <span class="s-check-group__title" id={titleId}>{title}</span>
        <span class="s-check-group__count">
          {selectedCount}/{items.length}
        </span>
      </button>
    {:else}
      <h4 class="s-check-group__title" id={titleId}>{title}</h4>
      <span class="s-check-group__count">
        {selectedCount}/{items.length}
      </span>
    {/if}
  </div>
  <div class="s-check-group__rows" id={contentId} hidden={collapsible && !open}>
    {#if items.length === 0}
      <p class="s-check-group__note">{emptyLabel}</p>
    {:else if visibleItems.length === 0}
      <p class="s-check-group__note">
        {t('access.noMatches', 'No matches.')}
      </p>
    {:else}
      {#each visibleItems as item (item.name)}
        <Checkbox
          class="s-check-row"
          checked={item.allowed}
          ariaLabel={toggleLabel(item.name)}
          onChange={(next) => onToggle(item.name, next)}
        >
          <span class="s-check-row__text">
            <span class="s-check-row__name">{item.name}</span>
            {#if item.detail}
              <span
                class="s-check-row__detail"
                class:s-check-row__detail--warn={item.unavailable}
                >{item.detail}</span
              >
            {/if}
            {#each item.warnings ?? [] as warning, index (`${item.name}-warning-${index}`)}
              <span class="s-check-row__detail s-check-row__detail--warn"
                >{warning}</span
              >
            {/each}
          </span>
        </Checkbox>
      {/each}
    {/if}
  </div>
</section>
