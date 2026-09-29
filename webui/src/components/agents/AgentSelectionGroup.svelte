<script module>
  // The filter every selection list applies to its rows: all members whose
  // name or detail contains the text.
  export function filterSelectionItems(list, text) {
    const needle = text.trim().toLocaleLowerCase();
    if (!needle) return list;
    return list.filter((item) =>
      [
        item.name,
        item.detailTitle,
        item.detail,
        ...(item.detailRows ?? []).map((row) => row.value),
      ]
        .filter(Boolean)
        .join(' ')
        .toLocaleLowerCase()
        .includes(needle),
    );
  }
</script>

<script>
  // One selection list (Skills, Identity Agents, Project Agents, Skill access)
  // as a shared Checkbox group (styles/settings/sections.css): a head with the
  // group checkbox, the title and the selection count, then one single-line
  // row per member. The caller owns the selection policy (wildcards and what
  // selecting a whole group means) and the filter text shared by the groups
  // of a section.
  //
  // A row shows the member's name and at most one short state at its end
  // (`{text, tone}`). The member's `detail` (a description) is never rendered
  // inline: it appears only in the row's tooltip on hover or keyboard focus,
  // headed by an optional `detailTitle` (a display name beside an id) and
  // followed by optional `detailRows` (`{label, value, mono}`) and
  // `lockedReason` for a locked member (a fixed grant: shown, not
  // changeable). The filter matches all of them. A member may carry one inline action. With `onOpen`, the
  // member's name opens it elsewhere and only the box toggles. With
  // `onContextMenu(item, event)`, a right click on the row or the context
  // menu key on its controls asks the caller for the member's menu (the
  // event's default is already prevented).
  import { isContextMenuKey } from '../ui/contextMenu.js';
  import { tooltip } from '$lib/tooltip.js';
  import Button from '../ui/Button.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
  import { t } from '$lib/i18n.js';

  const noop = () => {};
  const uid = $props.id();

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
    groupToggle = true,
    plainNames = false,
    onOpenChange = noop,
    onToggle = noop,
    onSetAll = noop,
    onOpen = null,
    onAction = noop,
    onContextMenu = null,
    class: className = '',
  } = $props();

  // A row may count differently from its checkbox (a Skill shared with an Agent
  // whose own selection blocks it is checked but not counted).
  let selectedCount = $derived(
    items.filter((item) => item.counted ?? item.allowed).length,
  );
  let changeable = $derived(items.filter((item) => !item.locked));
  let changeableSelected = $derived(
    changeable.filter((item) => item.allowed).length,
  );
  let groupState = $derived(
    changeable.length > 0 && changeableSelected === changeable.length
      ? 'on'
      : changeableSelected > 0
        ? 'mixed'
        : 'off',
  );
  let visibleItems = $derived(filterSelectionItems(items, query));

  function stateId(index) {
    return `${uid}-state-${index}`;
  }

  // Beside the name: a card headed by the member's complete name (or its
  // display name) with the description, detail rows and why a locked member
  // cannot change; a member without details shows only a clipped name.
  function rowTooltip(item) {
    const text = item.detail || '';
    const rows = [
      ...(item.detailRows ?? []),
      ...(item.locked && item.lockedReason
        ? [{ value: item.lockedReason }]
        : []),
    ];
    const placement = { placement: 'right', alignTo: '.s-check-row__name' };
    if (!item.detailTitle && !text && rows.length === 0) {
      return {
        text: item.name,
        mono: !plainNames,
        whenTruncated: true,
        ...placement,
      };
    }
    return { title: item.detailTitle || item.name, text, rows, ...placement };
  }

  function rowContextMenu(item) {
    if (!onContextMenu) return undefined;
    return (event) => {
      event.preventDefault();
      onContextMenu(item, event);
    };
  }

  function controlKeydown(item) {
    if (!onContextMenu) return undefined;
    return (event) => {
      if (!isContextMenuKey(event)) return;
      event.preventDefault();
      onContextMenu(item, event);
    };
  }
</script>

{#snippet memberName(item)}
  <span class="s-check-row__name" class:s-check-row__name--plain={plainNames}
    >{item.name}</span
  >
{/snippet}

{#snippet memberState(item, index)}
  {#if item.state?.text}
    <span
      id={stateId(index)}
      class="s-check-row__state"
      class:s-check-row__state--warn={item.state.tone === 'warn'}
      >{item.state.text}</span
    >
  {/if}
{/snippet}

<section
  class={['s-group', 's-check-group', className].filter(Boolean).join(' ')}
  aria-labelledby={titleId}
>
  <div class="s-check-group__head">
    {#if groupToggle && changeable.length > 0}
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
        {t('access.noMatches')}
      </p>
    {:else}
      {#each visibleItems as item, index (item.key ?? item.name)}
        <!-- svelte-ignore a11y_no_static_element_interactions (Right click anywhere on the row, including a locked row's disabled box; the context menu key on the row's controls is the keyboard path.) -->
        <div
          class="s-check-item"
          class:s-check-item--inert={item.locked && !onOpen && !item.action}
          use:tooltip={rowTooltip(item)}
          oncontextmenu={rowContextMenu(item)}
        >
          {#if onOpen}
            <Checkbox
              class="s-check-item__box"
              checked={item.allowed}
              disabled={item.locked}
              ariaLabel={toggleLabel(item.name, item)}
              aria-describedby={item.state?.text ? stateId(index) : undefined}
              onkeydown={controlKeydown(item)}
              onChange={(next) => onToggle(item.name, next, item)}
            />
            <button
              type="button"
              class="s-check-item__open"
              data-item-key={item.key ?? item.name}
              onclick={() => onOpen(item)}
              onkeydown={controlKeydown(item)}
            >
              {@render memberName(item)}
            </button>
            {@render memberState(item, index)}
          {:else}
            <Checkbox
              class="s-check-row"
              checked={item.allowed}
              disabled={item.locked}
              ariaLabel={toggleLabel(item.name, item)}
              aria-describedby={item.state?.text ? stateId(index) : undefined}
              onkeydown={controlKeydown(item)}
              onChange={(next) => onToggle(item.name, next, item)}
            >
              {@render memberName(item)}
              {@render memberState(item, index)}
            </Checkbox>
            {#if item.action}
              <Button
                variant="tertiary"
                class="s-check-item__action"
                ariaLabel={item.action.ariaLabel || undefined}
                onClick={() => onAction(item)}>{item.action.label}</Button
              >
            {/if}
          {/if}
        </div>
      {/each}
    {/if}
  </div>
</section>
