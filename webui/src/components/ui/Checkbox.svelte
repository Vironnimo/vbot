<script>
  // Shared checkbox for selecting members of a list. It renders a
  // `role="checkbox"` button so a label passed as children makes the whole
  // row the click target, and supports the mixed state of a group checkbox.
  // Choosing a mixed checkbox selects it. Immediate on/off settings use
  // Toggle instead.

  const noop = () => {};

  let {
    checked = false,
    indeterminate = false,
    onChange = noop,
    disabled = false,
    ariaLabel = '',
    class: className = '',
    children,
    ...rest
  } = $props();

  let checkboxClass = $derived(
    ['checkbox', children && 'checkbox--labelled', className]
      .filter(Boolean)
      .join(' '),
  );
</script>

<button
  {...rest}
  type="button"
  class={checkboxClass}
  role="checkbox"
  aria-checked={indeterminate ? 'mixed' : checked}
  aria-label={ariaLabel || undefined}
  {disabled}
  onclick={() => onChange(indeterminate ? true : !checked)}
>
  <span class="checkbox__box" aria-hidden="true">
    {#if indeterminate}
      <svg viewBox="0 0 16 16"><path d="M4 8h8" /></svg>
    {:else if checked}
      <svg viewBox="0 0 16 16"><path d="m3.5 8.5 3 3 6-7" /></svg>
    {/if}
  </span>
  {#if children}
    <span class="checkbox__label">{@render children()}</span>
  {/if}
</button>
