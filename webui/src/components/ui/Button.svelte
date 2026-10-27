<script>
  // Shared button primitive. It owns the single canonical class per visual
  // level so every button in the app is correct by construction instead of
  // hand-assembling the global `btn-*` classes. Callers pass already-translated
  // label/icon content as the default `children` snippet — the primitive never
  // calls `t(...)` itself.
  //
  // `tooltip` renders through the shared quick tooltip (lib/tooltip.js), not
  // the native `title`: a string label or the action's content object (e.g.
  // with a `placement`). Browsers do not fire pointer events on a disabled
  // button, so `disabledReason` (why the action is unavailable right now)
  // moves the tooltip onto a wrapping <span class="tooltip-anchor">, which
  // shows the reason while disabled and `tooltip` otherwise.
  import { tooltip as tooltipAction } from '../../lib/tooltip.js';

  const noop = () => {};

  let {
    variant = 'secondary',
    type = 'button',
    icon = false,
    disabled = false,
    loading = false,
    ariaLabel = '',
    tooltip = '',
    disabledReason = '',
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
  const componentId = $props.id();
  let buttonClass = $derived(
    [variantClass, icon ? 'btn-icon' : '', className].filter(Boolean).join(' '),
  );
</script>

{#snippet control(buttonTooltip)}
  <button
    {...rest}
    id={rest.id || (disabledReason ? `${componentId}-control` : undefined)}
    {type}
    class={buttonClass}
    disabled={isDisabled}
    aria-label={ariaLabel || undefined}
    aria-busy={loading || undefined}
    aria-hidden={isDisabled && disabledReason ? true : undefined}
    use:tooltipAction={buttonTooltip}
    onclick={onClick}
  >
    {@render children?.()}
  </button>
{/snippet}

{#if disabledReason}
  <!-- The focusable disabled wrapper represents the unavailable control;
       its native button stays disabled and is hidden from the accessibility tree. -->
  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
  <span
    class="tooltip-anchor"
    role={isDisabled ? 'button' : undefined}
    tabindex={isDisabled ? 0 : undefined}
    aria-disabled={isDisabled || undefined}
    aria-label={isDisabled && ariaLabel ? ariaLabel : undefined}
    aria-labelledby={isDisabled && !ariaLabel
      ? rest.id || `${componentId}-control`
      : undefined}
    aria-describedby={isDisabled ? `${componentId}-disabled-reason` : undefined}
    use:tooltipAction={isDisabled ? disabledReason : tooltip}
    >{@render control('')}<span id={`${componentId}-disabled-reason`} hidden
      >{disabledReason}</span
    ></span
  >
{:else}
  {@render control(tooltip)}
{/if}
