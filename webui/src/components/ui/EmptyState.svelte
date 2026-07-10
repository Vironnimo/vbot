<script>
  // Shared empty-content surface. It represents a known absence of content —
  // never loading or failure — with one title/description hierarchy across
  // full views, master-list panes, and compact cards. Callers provide already-
  // translated copy plus optional icon/actions snippets. Placement stays at
  // the call site through `class`; `fill` lets the surface own remaining space.

  let {
    title = '',
    description = '',
    density = 'default',
    fill = false,
    class: className = '',
    icon,
    actions,
    ...rest
  } = $props();

  const DENSITIES = new Set(['default', 'compact']);

  let densityClass = $derived(DENSITIES.has(density) ? density : 'default');
  let emptyStateClass = $derived(
    [
      'empty-state',
      `empty-state--${densityClass}`,
      fill ? 'empty-state--fill' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
</script>

<div {...rest} class={emptyStateClass}>
  {#if icon}
    <div class="empty-state__icon" aria-hidden="true">{@render icon()}</div>
  {/if}
  {#if title}
    <p class="empty-state__title">{title}</p>
  {/if}
  {#if description}
    <p class="empty-state__description">{description}</p>
  {/if}
  {#if actions}
    <div class="empty-state__actions">{@render actions()}</div>
  {/if}
</div>
