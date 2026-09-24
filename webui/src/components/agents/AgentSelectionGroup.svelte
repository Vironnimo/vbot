<script>
  // One allow-list of the Agent editor (Skills, Identity Agents, Project
  // Agents) as a group panel: a head with the group checkbox, the title and
  // the selection count, then one labelled checkbox row per member. The caller
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
  class={['s-group', 'agents-selection', className].filter(Boolean).join(' ')}
  aria-labelledby={titleId}
>
  <div class="agents-selection__head">
    {#if items.length > 0}
      <Checkbox
        class="agents-selection__all"
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
        <span class="agents-selection__title" id={titleId}>{title}</span>
        <span class="agents-selection__count">
          {selectedCount}/{items.length}
        </span>
      </button>
    {:else}
      <h4 class="agents-selection__title" id={titleId}>{title}</h4>
      <span class="agents-selection__count">
        {selectedCount}/{items.length}
      </span>
    {/if}
  </div>
  <div
    class="agents-selection__body"
    id={contentId}
    hidden={collapsible && !open}
  >
    {#if items.length === 0}
      <p class="agents-selection__note">{emptyLabel}</p>
    {:else if visibleItems.length === 0}
      <p class="agents-selection__note">
        {t('access.noMatches', 'No matches.')}
      </p>
    {:else}
      {#each visibleItems as item (item.name)}
        <Checkbox
          class="agents-selection__row"
          checked={item.allowed}
          ariaLabel={toggleLabel(item.name)}
          onChange={(next) => onToggle(item.name, next)}
        >
          <span class="agents-selection__text">
            <span class="agents-selection__name">{item.name}</span>
            {#if item.detail}
              <span
                class="agents-selection__detail"
                class:agents-selection__detail--warn={item.unavailable}
                >{item.detail}</span
              >
            {/if}
            {#each item.warnings ?? [] as warning, index (`${item.name}-warning-${index}`)}
              <span
                class="agents-selection__detail agents-selection__detail--warn"
                >{warning}</span
              >
            {/each}
          </span>
        </Checkbox>
      {/each}
    {/if}
  </div>
</section>
