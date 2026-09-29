<script>
  // One row of SortableList. It registers the row element with dnd-kit, so
  // the whole row is the drag surface, and reports the drag state to the row
  // content. Internal to SortableList.
  import { createSortable } from '@dnd-kit/svelte/sortable';

  let {
    id,
    index,
    group,
    disabled = false,
    tag = 'li',
    class: className = '',
    focusable = false,
    onkeydown,
    children,
  } = $props();

  const sortable = createSortable({
    get id() {
      return id;
    },
    get index() {
      return index;
    },
    get group() {
      return group;
    },
    get disabled() {
      return disabled;
    },
  });
</script>

<!-- Keyboard moves bubble up from the row's own focusable controls, so the
     row listens for them without becoming an interactive element itself. -->
<svelte:element
  this={tag}
  {@attach sortable.attach}
  class={['sortable-item', className]}
  class:sortable-item--draggable={!disabled}
  class:sortable-item--dragging={sortable.isDragging}
  tabindex={focusable ? 0 : undefined}
  data-sortable-key={id}
  {onkeydown}
>
  {@render children?.({ dragging: sortable.isDragging })}
</svelte:element>

<style>
  .sortable-item.sortable-item--dragging {
    background-color: var(--surface-2);
    border-radius: var(--r-md);
    box-shadow: var(--floating-elevation);
  }
</style>
