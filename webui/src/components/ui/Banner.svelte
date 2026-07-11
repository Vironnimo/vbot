<script>
  // Shared inline banner: a padded message box carrying a 2px colored left
  // stripe — the canonical home for every in-flow feedback / notice box across
  // the WebUI (loading, form/RPC errors, warnings, non-blocking notices). A
  // known absence of content belongs to EmptyState instead. Banner is the
  // in-flow counterpart to the app-wide ToastStack
  // (transient, bottom-right) and the chat command-output surfaces; reach for
  // Banner when the message lives inside a view's own layout. Callers pass a
  // `variant` and already-translated `children` — plain text, or text plus a
  // trailing action (a Review button, a settings link): the box lays them out with
  // space-between, so a lone message sits left and an action floats right.
  //
  // `rest` is spread onto the element so each call site keeps its own
  // `role`/`aria-live`; margins and width stay at the call site via `class`.
  // The colors and box chrome live once in the primitives layer of
  // styles/app.css, mirroring the Badge and StatusChip primitives.

  let {
    variant = 'neutral',
    class: className = '',
    children,
    ...rest
  } = $props();

  const VARIANTS = new Set(['info', 'success', 'warn', 'error', 'neutral']);

  let variantClass = $derived(VARIANTS.has(variant) ? variant : 'neutral');
  let bannerClass = $derived(
    ['banner', `banner--${variantClass}`, className].filter(Boolean).join(' '),
  );
</script>

<div {...rest} class={bannerClass}>{@render children?.()}</div>
