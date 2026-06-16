<script>
  // Shared switch toggle. Renders the `role="switch"` button + knob in the two
  // design-system sizes (large for settings rows, small for tool/skill lists)
  // so every on/off control shares one accessible implementation. The label is
  // supplied already translated via `ariaLabel`.

  const noop = () => {};

  let {
    checked = false,
    onChange = noop,
    size = 'lg',
    disabled = false,
    ariaLabel = '',
    class: className = '',
    ...rest
  } = $props();

  const SIZE_CLASS = {
    lg: 'toggle',
    sm: 'tl-toggle',
  };

  let sizeClass = $derived(SIZE_CLASS[size] ?? SIZE_CLASS.lg);
  let toggleClass = $derived([sizeClass, className].filter(Boolean).join(' '));
</script>

<button
  {...rest}
  type="button"
  class={toggleClass}
  class:on={checked}
  role="switch"
  aria-checked={checked}
  aria-label={ariaLabel || undefined}
  {disabled}
  onclick={() => onChange(!checked)}
>
  <span class="t-knob"></span>
</button>
