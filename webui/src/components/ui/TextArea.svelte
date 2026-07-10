<script>
  // Shared multi-line form control. Callers own the value and receive edits
  // through the same callback contract as TextField. The default variant is a
  // bordered form field; inset is the borderless editor embedded in a bounded
  // parent surface. Code mode owns JSON/Markdown-friendly wrapping behavior.

  const noop = () => {};

  let {
    value = '',
    onInput = noop,
    variant = 'default',
    code = false,
    invalid = false,
    disabled = false,
    readonly = false,
    rows = undefined,
    placeholder = '',
    ariaLabel = '',
    class: className = '',
    ...rest
  } = $props();

  const VARIANTS = new Set(['default', 'inset']);

  let variantClass = $derived(VARIANTS.has(variant) ? variant : 'default');
  let textAreaClass = $derived(
    [
      'text-area',
      `text-area--${variantClass}`,
      code ? 'text-area--code' : '',
      invalid ? 'text-area--invalid' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
</script>

<textarea
  {...rest}
  class={textAreaClass}
  {value}
  {rows}
  {placeholder}
  {disabled}
  {readonly}
  aria-label={ariaLabel || undefined}
  aria-invalid={invalid ? 'true' : 'false'}
  oninput={(event) => onInput(event.currentTarget.value, event)}
></textarea>
