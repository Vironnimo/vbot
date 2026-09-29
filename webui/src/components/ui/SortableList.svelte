<script>
  // Shared reorderable list. A row is dragged by pressing anywhere on it that
  // is not text entry and moving; the other rows slide aside while it moves.
  // Alt+ArrowUp/Alt+ArrowDown move the row that holds keyboard focus. The
  // owner keeps data and persistence: it receives one `onReorder(from, to)`
  // per completed move and supplies the resulting `items`. Until the returned
  // promise settles the list shows the moved order and accepts no new move.
  import { tick } from 'svelte';
  import { DragDropProvider } from '@dnd-kit/svelte';
  import { move } from '@dnd-kit/helpers';

  import { t } from '$lib/i18n.js';
  import SortableItem from './SortableItem.svelte';
  import {
    SORTABLE_PLUGINS,
    SORTABLE_SENSORS,
    createSortableManager,
    keepDragFeedbackVisible,
    keyboardMoveTarget,
    moveItem,
    orderByKeys,
  } from './sortable.js';

  let {
    items = [],
    getKey = (entry) => entry.id,
    getLabel = (entry) => String(getKey(entry)),
    onReorder = () => {},
    onDragActiveChange = () => {},
    disabled = false,
    tag = 'ul',
    itemTag = 'li',
    class: className = '',
    itemClass = '',
    itemFocusable = false,
    item,
    ...rest
  } = $props();

  const listId = $props.id();
  const manager = createSortableManager();
  $effect(() => () => manager.destroy());

  // Key order shown while a drag runs or a completed move is being saved.
  let draftKeys = $state(null);
  let dragStartKeys = null;
  let pending = $state(false);
  let announcement = $state('');

  let rows = $derived(
    draftKeys ? orderByKeys(items, draftKeys, getKey) : items,
  );
  let locked = $derived(disabled || pending || items.length < 2);

  function handleDragStart() {
    dragStartKeys = items.map(getKey);
    draftKeys = dragStartKeys;
    onDragActiveChange(true);
  }

  function handleDragOver(event) {
    if (!draftKeys) {
      return;
    }
    const next = move(draftKeys, event);
    if (next !== draftKeys) {
      draftKeys = next;
      const element = event.operation.source?.element;
      void tick().then(() => keepDragFeedbackVisible(element));
    }
  }

  function handleDragEnd(event) {
    const startKeys = dragStartKeys;
    const endKeys = draftKeys;
    dragStartKeys = null;
    onDragActiveChange(false);
    const key = event.operation.source?.id;
    const from = startKeys?.indexOf(key) ?? -1;
    const to = endKeys?.indexOf(key) ?? -1;
    // The owner's list changed during the drag: its indices no longer match.
    const stale = items.map(getKey).join('\n') !== startKeys?.join('\n');
    if (event.canceled || stale || from < 0 || to < 0 || from === to) {
      draftKeys = null;
      return;
    }
    void commit(from, to, endKeys);
  }

  async function handleKeydown(index, event) {
    const to = keyboardMoveTarget(event, index);
    if (to === null) {
      return;
    }
    event.preventDefault();
    if (locked || to < 0 || to >= items.length) {
      return;
    }
    const focused = document.activeElement;
    const saved = commit(index, to, moveItem(items.map(getKey), index, to));
    await tick();
    // Moving a row re-inserts its DOM nodes, which drops focus.
    if (focused instanceof HTMLElement && focused.isConnected) {
      focused.focus();
    }
    await saved;
  }

  async function commit(from, to, keys) {
    const moved = items[from];
    draftKeys = keys;
    pending = true;
    announcement = t('common.sortable.moved', {
      name: getLabel(moved),
      position: to + 1,
      total: items.length,
    });
    try {
      await onReorder(from, to);
    } finally {
      draftKeys = null;
      pending = false;
    }
  }
</script>

<DragDropProvider
  {manager}
  sensors={SORTABLE_SENSORS}
  plugins={SORTABLE_PLUGINS}
  onDragStart={handleDragStart}
  onDragOver={handleDragOver}
  onDragEnd={handleDragEnd}
>
  <svelte:element
    this={tag}
    {...rest}
    class={['sortable-list', className]}
    aria-describedby={locked ? undefined : `${listId}-instructions`}
  >
    {#each rows as entry, index (getKey(entry))}
      <SortableItem
        id={getKey(entry)}
        {index}
        group={listId}
        disabled={locked}
        tag={itemTag}
        class={typeof itemClass === 'function' ? itemClass(entry) : itemClass}
        focusable={itemFocusable}
        onkeydown={(event) => handleKeydown(index, event)}
      >
        {#snippet children(state)}
          {@render item?.(entry, index, state)}
        {/snippet}
      </SortableItem>
    {/each}
  </svelte:element>
</DragDropProvider>
<span id={`${listId}-instructions`} class="sortable-list__sr-only">
  {t('common.sortable.instructions')}
</span>
<span class="sortable-list__sr-only" role="status" aria-live="polite">
  {announcement}
</span>

<style>
  .sortable-list__sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }
</style>
