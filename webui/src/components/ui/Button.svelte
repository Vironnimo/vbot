<script>
  // Shared button primitive. It owns the single canonical class per visual
  // level so every button in the app is correct by construction instead of
  // hand-assembling the global `btn-*` classes. Callers pass already-translated
  // label/icon content as the default `children` snippet — the primitive never
  // calls `t(...)` itself.

  const noop = () => {};

  let {
    variant = 'secondary',
    type = 'button',
    icon = false,
    disabled = false,
    loading = false,
    ariaLabel = '',
    title = '',
    class: className = '',
    onClick = noop,
    children,
    ...rest
  } = $props();

  const VARIANT_CLASS = {
    primary: 'btn-primary',
    secondary: 'btn-secondary',
    tertiary: 'btn-tertiary',
    danger: 'btn-danger',
  };

  let variantClass = $derived(
    VARIANT_CLASS[variant] ?? VARIANT_CLASS.secondary,
  );
  let isDisabled = $derived(disabled || loading);
  let buttonClass = $derived(
    [variantClass, icon ? 'btn-icon' : '', className].filter(Boolean).join(' '),
  );
</script>

<button
  {...rest}
  {type}
  class={buttonClass}
  disabled={isDisabled}
  aria-label={ariaLabel || undefined}
  aria-busy={loading || undefined}
  title={title || undefined}
  onclick={onClick}
>
  {@render children?.()}
</button>
