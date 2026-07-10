<script>
  // Shared metadata badge: a small pill carrying a metadata tag — a kind marker,
  // origin, version, or scope — as opposed to StatusChip, which carries a
  // semantic status. Callers pass a `variant` and the already-translated label
  // (optionally preceded by an inline icon) as the default `children` snippet.
  // The component emits the canonical `badge badge--<variant>` classes; the
  // colors live once in the primitives layer of styles/app.css.
  //
  // Svelte actions cannot be spread onto a component, so a call site that needs
  // `use:tooltip` wraps the Badge in a `<span class="tooltip-anchor" use:tooltip>`
  // (the same pattern the Button primitive documents for disabled tooltips).

  let {
    variant = 'neutral',
    class: className = '',
    children,
    ...rest
  } = $props();

  const VARIANTS = new Set(['neutral', 'info', 'success', 'warn', 'error']);

  let variantClass = $derived(VARIANTS.has(variant) ? variant : 'neutral');
  let badgeClass = $derived(
    ['badge', `badge--${variantClass}`, className].filter(Boolean).join(' '),
  );
</script>

<span {...rest} class={badgeClass}>{@render children?.()}</span>
