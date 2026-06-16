<script>
  // Shared status chip: a pill carrying one semantic color. Callers pass a
  // `variant` and the already-translated label as the default `children`
  // snippet. The component emits the canonical `chip <variant>` classes; the
  // color-named aliases (chip-green/amber/orange/red) were collapsed away.

  let {
    variant = 'neutral',
    class: className = '',
    children,
    ...rest
  } = $props();

  const VARIANTS = new Set(['success', 'warn', 'info', 'error', 'neutral']);

  let variantClass = $derived(VARIANTS.has(variant) ? variant : 'neutral');
  let chipClass = $derived(
    ['chip', variantClass, className].filter(Boolean).join(' '),
  );
</script>

<span {...rest} class={chipClass}>{@render children?.()}</span>
