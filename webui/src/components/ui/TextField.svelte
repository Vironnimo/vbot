<script>
  // Shared text field. Covers the default input, the modal-contrast variant,
  // and the read-only value-box presentation. It uses the callback-prop pattern
  // (`value` in, `onInput(next, event)` out) rather than `bind:` so it follows
  // the same convention as the other primitives. Placeholder/label text arrives
  // already translated.

  const noop = () => {};

  let {
    value = '',
    onInput = noop,
    type = 'text',
    variant = 'default',
    readonly = false,
    invalid = false,
    disabled = false,
    inputmode = undefined,
    placeholder = '',
    ariaLabel = '',
    class: className = '',
    ...rest
  } = $props();

  const VARIANT_CLASS = {
    default: 's-input',
    modal: 'modal-input',
  };

  let variantClass = $derived(VARIANT_CLASS[variant] ?? 's-input');
  let inputClass = $derived(
    [variantClass, invalid ? 's-input--invalid' : '', className]
      .filter(Boolean)
      .join(' '),
  );
  let valueBoxClass = $derived(
    ['s-value-box', className].filter(Boolean).join(' '),
  );
</script>

{#if readonly}
  <div {...rest} class={valueBoxClass}>{value}</div>
{:else}
  <input
    {...rest}
    class={inputClass}
    {type}
    {value}
    {placeholder}
    {disabled}
    {inputmode}
    aria-label={ariaLabel || undefined}
    aria-invalid={invalid || undefined}
    oninput={(event) => onInput(event.currentTarget.value, event)}
  />
{/if}
