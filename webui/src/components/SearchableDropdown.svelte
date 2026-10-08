<script>
  import { tick, untrack } from 'svelte';

  import {
    computePanelPosition,
    optionDecorations,
    portal,
  } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';
  import { isImeComposing } from '$lib/keyboard.js';
  import { tooltip } from '$lib/tooltip.js';
  import OptionMarker from './OptionMarker.svelte';

  const SEARCH_HEADER_HEIGHT = 44;
  const noop = () => {};
  const componentId = $props.id();

  let {
    id = '',
    name = '',
    value = '',
    options = [],
    placeholder = t('dropdown.placeholder'),
    searchPlaceholder = t('dropdown.searchPlaceholder'),
    emptyLabel = t('dropdown.empty'),
    // Default filter the search box carries: the panel opens with this term
    // already applied and returns to it on close. Empty for every caller that
    // does not pass it, so the historical "opens unfiltered" behavior is kept.
    searchText = '',
    disabled = false,
    ariaLabel = '',
    ariaDescribedby = undefined,
    triggerClass = '',
    // Optional quick tooltip on the trigger (e.g. details of the selection).
    triggerTooltip = '',
    // Optional trigger content replacing the selection label and chevron,
    // for a compact trigger such as an icon. The trigger keeps its
    // accessible name (`ariaLabel`) and its keyboard contract.
    triggerContent = undefined,
    // The panel is at least this wide even under a narrower trigger.
    panelMinWidth = 0,
    panelClass = '',
    // Optional action row pinned under the options (e.g. the model pickers'
    // "show all models" toggle). Clicking it keeps the panel open so the
    // revealed options appear in place.
    footerActionLabel = '',
    onFooterAction = noop,
    // Collapsible groups: options naming a `group` id sit under that group's
    // header row, which opens and closes them (the panel becomes a tree).
    // `groups` describes each header: `{ id, label }` plus the option
    // decorations (`statusDot`, `badge`, `ariaLabel`, `tooltip`), shown while
    // the group is closed to summarize its hidden options, and `warning`, the
    // label of a warning sign shown always. The caller owns which groups are
    // open: `expandedGroups` lists their ids, `onGroupToggle(id, open)` asks
    // for a change. A search shows every group with a match open.
    collapsibleGroups = false,
    groups = [],
    expandedGroups = [],
    onGroupToggle = noop,
    onValueChange = noop,
    onOpenChange = noop,
  } = $props();

  let rootElement = $state();
  let triggerElement = $state();
  let panelElement = $state();
  let searchInputElement = $state();
  let isOpen = $state(false);
  // Seeded from the prop at creation (the dropdown is recreated per use); the
  // close() reset re-applies the current default filter reactively.
  let searchQuery = $state(untrack(() => searchText));
  let panelStyle = $state('');
  let panelPlacement = $state('bottom');
  // The row keyboard navigation is on: `option:<value>` or `group:<id>`.
  let activeKey = $state('');

  let normalizedOptions = $derived(normalizeOptions(options));
  let groupsById = $derived(
    new Map(normalizeGroups(groups).map((group) => [group.id, group])),
  );
  let searching = $derived(searchQuery.trim().length > 0);
  let filteredOptions = $derived(filterOptions(normalizedOptions, searchQuery));
  // Consecutive options that name the same `group` render under its label.
  let optionSections = $derived(groupOptions(filteredOptions));
  let treeRows = $derived(
    collapsibleGroups ? buildTreeRows(filteredOptions) : [],
  );
  // The rows the arrow keys move through, in display order.
  let navigableRows = $derived(
    collapsibleGroups
      ? treeRows.filter((row) => row.navigable)
      : filteredOptions
          .map((option, index) => ({
            key: optionKey(option.value),
            kind: 'option',
            option,
            domId: `${listboxId}-option-${index}`,
          }))
          .filter((row) => !row.option.disabled),
  );
  let selectedOption = $derived(
    normalizedOptions.find((option) => option.value === value) ?? null,
  );
  let triggerLabel = $derived(
    selectedOption?.triggerLabel || selectedOption?.label || placeholder,
  );
  let hasSelection = $derived(Boolean(selectedOption));
  // A clipped selection or option shows in full on hover unless the caller
  // supplies its own tooltip; options sit in a vertical list, so theirs
  // opens beside the row.
  let triggerHint = $derived(
    triggerTooltip ||
      (hasSelection ? clippedLabelHint(selectedOption, 'top') : ''),
  );
  const clippedLabelHint = (option, placement) => ({
    text: [option.label, option.secondaryLabel].filter(Boolean).join('\n'),
    placement,
    whenTruncated: true,
  });
  let listboxId = $derived(id ? `${id}-listbox` : `${componentId}-listbox`);
  let activeRow = $derived(
    navigableRows.find((row) => row.key === activeKey) ?? null,
  );
  let activeDescendantId = $derived(activeRow?.domId);

  function optionKey(optionValue) {
    return `option:${optionValue}`;
  }

  function groupKey(groupId) {
    return `group:${groupId}`;
  }

  function normalizeOptions(items) {
    return items.map((option) => {
      if (typeof option === 'string') {
        return {
          value: option,
          label: option,
          searchText: option,
          disabled: false,
          labelLead: '',
          ...optionDecorations(null),
        };
      }

      const label = option?.label ?? option?.value ?? '';
      const secondaryLabel = option?.secondaryLabel ?? '';

      return {
        value: option?.value ?? '',
        label,
        disabled: Boolean(option?.disabled),
        // A leading part of `label` shown muted, e.g. a Model id's provider
        // path, so the rest of the label stands out.
        labelLead: labelLead(label, option?.labelLead),
        secondaryLabel,
        // Shown on the trigger in place of `label` while selected.
        triggerLabel: option?.triggerLabel ?? '',
        group: option?.group ?? '',
        searchText: option?.searchText ?? `${label} ${secondaryLabel}`.trim(),
        ...optionDecorations(option),
      };
    });
  }

  function labelLead(label, lead) {
    return typeof lead === 'string' &&
      lead.length < label.length &&
      label.startsWith(lead)
      ? lead
      : '';
  }

  function normalizeGroups(items) {
    return (Array.isArray(items) ? items : [])
      .filter((group) => typeof group?.id === 'string' && group.id)
      .map((group) => fallbackGroup(group.id, group));
  }

  function fallbackGroup(groupId, group = null) {
    return {
      id: groupId,
      label: group?.label ?? groupId,
      warning: typeof group?.warning === 'string' ? group.warning : '',
      ...optionDecorations(group),
    };
  }

  // One row per shown option and, in collapsible mode, one header row before
  // each group's options; a closed group's options have no rows. While a
  // search runs every group with a match is open and its header is only a
  // label, so the arrow keys move through the matches alone.
  function buildTreeRows(items) {
    const rows = [];
    let openGroup = null;
    let lastGroup = null;
    items.forEach((option) => {
      const groupId = option.group;
      if (groupId && groupId !== lastGroup) {
        const expanded = searching || expandedGroups.includes(groupId);
        rows.push({
          key: groupKey(groupId),
          kind: 'group',
          group: groupsById.get(groupId) ?? fallbackGroup(groupId),
          expanded,
          level: 1,
          navigable: !searching,
          domId: `${listboxId}-row-${rows.length}`,
        });
        openGroup = expanded ? groupId : null;
      }
      lastGroup = groupId;
      if (groupId && groupId !== openGroup) {
        return;
      }
      rows.push({
        key: optionKey(option.value),
        kind: 'option',
        option,
        groupId,
        level: groupId ? 2 : 1,
        navigable: !option.disabled,
        domId: `${listboxId}-row-${rows.length}`,
      });
    });
    return rows;
  }

  function groupOptions(items) {
    const sections = [];
    items.forEach((option, index) => {
      const last = sections.at(-1);
      if (last && last.label === option.group) {
        last.items.push({ option, index });
      } else {
        sections.push({ label: option.group, items: [{ option, index }] });
      }
    });
    return sections;
  }

  function filterOptions(items, query) {
    const normalizedQuery = query.trim().toLowerCase();

    if (!normalizedQuery) {
      return items;
    }

    // In collapsible mode a group's name matches all of its options.
    return items.filter(
      (option) =>
        option.searchText.toLowerCase().includes(normalizedQuery) ||
        (collapsibleGroups &&
          option.group &&
          String(groupsById.get(option.group)?.label ?? option.group)
            .toLowerCase()
            .includes(normalizedQuery)),
    );
  }

  // Also opens the panel from a related control elsewhere (via bind:this).
  export async function open({ focus = 'selected' } = {}) {
    if (disabled) {
      return;
    }

    isOpen = true;
    setInitialActiveOption(focus);
    onOpenChange(true);
    await tick();
    updatePanelPosition();
    searchInputElement?.focus();
  }

  function close() {
    if (!isOpen) {
      return;
    }

    isOpen = false;
    searchQuery = searchText;
    panelStyle = '';
    panelPlacement = 'bottom';
    activeKey = '';
    onOpenChange(false);
  }

  async function toggleOpen() {
    if (isOpen) {
      close();
      return;
    }

    await open();
  }

  function setInitialActiveOption(focus) {
    const rows = navigableRows;
    if (rows.length === 0) {
      activeKey = '';
      return;
    }
    if (focus === 'last') {
      activeKey = rows.at(-1).key;
      return;
    }
    const selectedRow = rows.find(
      (row) => row.kind === 'option' && row.option.value === value,
    );
    activeKey = (selectedRow ?? rows[0]).key;
  }

  async function activateKey(key) {
    activeKey = key;
    await tick();
    document
      .getElementById(activeDescendantId)
      ?.scrollIntoView?.({ block: 'nearest' });
  }

  async function moveActiveOption(direction) {
    const rows = navigableRows;
    if (rows.length === 0) {
      activeKey = '';
      return;
    }
    const currentIndex = rows.findIndex((row) => row.key === activeKey);
    let nextIndex;
    if (direction === 'first') {
      nextIndex = 0;
    } else if (direction === 'last') {
      nextIndex = rows.length - 1;
    } else if (direction === 1) {
      nextIndex = (currentIndex + 1 + rows.length) % rows.length;
    } else {
      nextIndex = (currentIndex - 1 + rows.length) % rows.length;
    }
    await activateKey(rows[nextIndex].key);
  }

  // Closing a group moves the active row from its options to its header.
  function toggleGroup(groupId) {
    onGroupToggle(groupId, !expandedGroups.includes(groupId));
    activeKey = groupKey(groupId);
  }

  function handleGroupClick(groupId) {
    toggleGroup(groupId);
    searchInputElement?.focus();
  }

  // Tree keys: Right opens a closed group or moves into an open one; Left
  // closes an open group or moves from an option to its group. Returns
  // whether the key acted.
  async function handleTreeArrow(key) {
    const row = activeRow;
    if (!row || searching) {
      return false;
    }
    if (row.kind === 'group') {
      if (key === 'ArrowRight' && row.expanded) {
        const child = navigableRows[navigableRows.indexOf(row) + 1];
        if (child?.groupId === row.group.id) {
          await activateKey(child.key);
        }
      } else if ((key === 'ArrowRight') !== row.expanded) {
        toggleGroup(row.group.id);
      }
      return true;
    }
    if (key === 'ArrowLeft' && row.groupId) {
      await activateKey(groupKey(row.groupId));
      return true;
    }
    return false;
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

  function handleSearchInput() {
    setInitialActiveOption('selected');
  }

  async function handleSearchKeyDown(event) {
    if (isImeComposing(event)) {
      return;
    }
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
    if (event.key === 'Enter') {
      const row = activeRow;
      if (row?.kind === 'group') {
        event.preventDefault();
        toggleGroup(row.group.id);
        return;
      }
      if (row) {
        event.preventDefault();
        triggerElement?.focus();
        selectOption(row.option);
      }
      return;
    }
    if (
      collapsibleGroups &&
      (event.key === 'ArrowRight' || event.key === 'ArrowLeft') &&
      (await handleTreeArrow(event.key))
    ) {
      event.preventDefault();
      return;
    }
    const directions = {
      ArrowDown: 1,
      ArrowUp: -1,
      Home: 'first',
      End: 'last',
    };
    if (!(event.key in directions)) {
      return;
    }
    event.preventDefault();
    await moveActiveOption(directions[event.key]);
  }

  function updatePanelPosition() {
    if (!isOpen || !triggerElement) {
      return;
    }

    const { placement, left, width, verticalRule, optionsMaxHeight } =
      computePanelPosition(triggerElement, {
        reservedHeight: SEARCH_HEADER_HEIGHT,
        minWidth: panelMinWidth,
      });

    panelPlacement = placement;
    panelStyle = [
      `left: ${left}px`,
      verticalRule,
      `width: ${width}px`,
      `--searchable-dropdown-options-max-height: ${optionsMaxHeight}px`,
    ].join('; ');
  }

  function handleDocumentMouseDown(event) {
    if (!isOpen) {
      return;
    }

    // The panel is portaled out of `rootElement`, so check both.
    if (
      rootElement?.contains(event.target) ||
      panelElement?.contains(event.target)
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
    updatePanelPosition();
  }

  function selectOption(option) {
    if (option.disabled) {
      return;
    }

    onValueChange(option.value, option);
    close();
  }

  function handleWindowScroll(event) {
    if (!isOpen) {
      return;
    }

    if (event.target instanceof Node && panelElement?.contains(event.target)) {
      return;
    }

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

{#snippet optionButton(option, domId, treeLevel = 0)}
  <button
    class="s-dropdown-opt searchable-dropdown__option"
    class:searchable-dropdown__option--nested={treeLevel > 1}
    class:selected={option.value === value}
    type="button"
    role={treeLevel ? 'treeitem' : 'option'}
    aria-level={treeLevel || undefined}
    id={domId}
    tabindex="-1"
    disabled={option.disabled}
    aria-label={option.ariaLabel || undefined}
    aria-selected={option.value === value}
    use:tooltip={option.tooltip || clippedLabelHint(option, 'right')}
    class:active={optionKey(option.value) === activeKey}
    onclick={() => selectOption(option)}
  >
    {#if option.statusDot}
      <span
        class="dropdown-status-dot tab-indicator tab-indicator--{option.statusDot}"
        aria-hidden="true"
      ></span>
    {/if}
    <span class="searchable-dropdown__option-label"
      >{@render labelText(option)}</span
    >{#if option.marker}<OptionMarker
        label={option.marker.label}
        tooltip={option.marker.tooltip}
      />{/if}
    {#if option.secondaryLabel}
      <span class="searchable-dropdown__option-meta">
        {option.secondaryLabel}
      </span>
    {/if}
    {#if option.badge}
      <span class="count-badge">{option.badge}</span>
    {/if}
  </button>
{/snippet}

{#snippet labelText(option)}{#if option.labelLead}<span
      class="searchable-dropdown__label-lead">{option.labelLead}</span
    >{option.label.slice(
      option.labelLead.length,
    )}{:else}{option.label}{/if}{/snippet}

{#snippet groupRow(row)}
  {@const group = row.group}
  <button
    class="s-dropdown-opt searchable-dropdown__group-row"
    type="button"
    role="treeitem"
    aria-level="1"
    aria-expanded={row.expanded}
    aria-selected="false"
    id={row.domId}
    tabindex="-1"
    aria-label={(!row.expanded && group.ariaLabel) || undefined}
    use:tooltip={(!row.expanded && group.tooltip) || ''}
    class:active={row.key === activeKey}
    disabled={!row.navigable}
    onclick={() => handleGroupClick(group.id)}
  >
    <span
      class="disclosure-chevron"
      class:disclosure-chevron--open={row.expanded}
      aria-hidden="true"
    ></span>
    <span class="searchable-dropdown__option-label">{group.label}</span>
    {#if group.warning}
      <span
        class="searchable-dropdown__group-warning"
        role="img"
        aria-label={group.warning}
        use:tooltip={{ text: group.warning, placement: 'right' }}
      >
        <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
          <path d="M6 1.5 11 10.5H1Z" />
          <path d="M6 5v2.5M6 9.2v.1" />
        </svg>
      </span>
    {/if}
    {#if !row.expanded && group.statusDot && group.statusDot !== 'idle'}
      <span
        class="dropdown-status-dot tab-indicator tab-indicator--{group.statusDot}"
        aria-hidden="true"
      ></span>
    {/if}
    {#if !row.expanded && group.badge}
      <span class="count-badge">{group.badge}</span>
    {/if}
  </button>
{/snippet}

<svelte:document
  onmousedown={handleDocumentMouseDown}
  onkeydown={handleDocumentKeyDown}
/>

<svelte:window onresize={handleWindowResize} />

<div
  bind:this={rootElement}
  class="s-dropdown searchable-dropdown {triggerClass}"
  class:open={isOpen}
  data-state={isOpen ? 'open' : 'closed'}
>
  {#if name}
    <input type="hidden" {name} {value} />
  {/if}

  <button
    bind:this={triggerElement}
    {id}
    class="s-dropdown-trigger searchable-dropdown__trigger"
    type="button"
    {disabled}
    aria-label={ariaLabel || placeholder}
    aria-describedby={ariaDescribedby}
    aria-haspopup={collapsibleGroups ? 'tree' : 'listbox'}
    aria-expanded={isOpen}
    aria-controls={isOpen ? listboxId : undefined}
    use:tooltip={triggerHint}
    onclick={toggleOpen}
    onkeydown={handleTriggerKeyDown}
  >
    {#if triggerContent}
      {@render triggerContent()}
    {:else}
      {#if selectedOption?.statusDot}
        <span
          class="dropdown-status-dot tab-indicator tab-indicator--{selectedOption.statusDot}"
          aria-hidden="true"
        ></span>
      {/if}
      <span
        class="searchable-dropdown__trigger-label"
        class:searchable-dropdown__trigger-label--placeholder={!hasSelection}
      >
        {#if selectedOption && !selectedOption.triggerLabel}{@render labelText(
            selectedOption,
          )}{:else}{triggerLabel}{/if}
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
    {/if}
  </button>

  {#if isOpen}
    <div
      bind:this={panelElement}
      use:portal
      class="s-dropdown-panel searchable-dropdown__panel {panelClass}"
      data-placement={panelPlacement}
      data-positioning="fixed"
      style={panelStyle}
    >
      <div class="s-dropdown-search searchable-dropdown__search">
        <svg viewBox="0 0 12 12" aria-hidden="true">
          <circle cx="5" cy="5" r="3.5" />
          <path d="M8 8l2.5 2.5" />
        </svg>
        <input
          bind:this={searchInputElement}
          type="text"
          bind:value={searchQuery}
          placeholder={searchPlaceholder}
          aria-label={searchPlaceholder}
          role="combobox"
          aria-autocomplete="list"
          aria-expanded="true"
          aria-controls={listboxId}
          aria-activedescendant={activeDescendantId}
          oninput={handleSearchInput}
          onkeydown={handleSearchKeyDown}
        />
      </div>

      <div
        class="s-dropdown-options searchable-dropdown__options"
        id={listboxId}
        role={collapsibleGroups ? 'tree' : 'listbox'}
        tabindex="-1"
        aria-label={ariaLabel || placeholder}
      >
        {#if filteredOptions.length > 0 && collapsibleGroups}
          {#each treeRows as row (row.key)}
            {#if row.kind === 'group'}
              {@render groupRow(row)}
            {:else}
              {@render optionButton(row.option, row.domId, row.level)}
            {/if}
          {/each}
        {:else if filteredOptions.length > 0}
          {#each optionSections as section, sectionIndex (sectionIndex)}
            {#if section.label}
              <div
                class="searchable-dropdown__group"
                role="group"
                aria-labelledby={`${listboxId}-group-${sectionIndex}`}
              >
                <div
                  class="searchable-dropdown__group-label"
                  id={`${listboxId}-group-${sectionIndex}`}
                >
                  {section.label}
                </div>
                {#each section.items as item (item.option.value)}
                  {@render optionButton(
                    item.option,
                    `${listboxId}-option-${item.index}`,
                  )}
                {/each}
              </div>
            {:else}
              {#each section.items as item (item.option.value)}
                {@render optionButton(
                  item.option,
                  `${listboxId}-option-${item.index}`,
                )}
              {/each}
            {/if}
          {/each}
        {:else}
          <div class="s-dropdown-empty searchable-dropdown__empty">
            {emptyLabel}
          </div>
        {/if}
      </div>

      {#if footerActionLabel}
        <button
          class="s-dropdown-footer searchable-dropdown__footer"
          type="button"
          onclick={() => onFooterAction()}
        >
          {footerActionLabel}
        </button>
      {/if}
    </div>
  {/if}
</div>
