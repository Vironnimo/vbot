<script>
  import { autosaveInput } from '$lib/autosave.js';
  // Shared multi-line form control. Callers own the value and receive edits
  // through the same callback contract as TextField. The default variant is a
  // bordered form field; inset is the borderless editor embedded in a bounded
  // parent surface. Code mode owns JSON/Markdown-friendly wrapping behavior.
  // `autoGrow` fits the field's height to its text, from its `rows` up to its
  // max-height (half the viewport unless the caller sets one), past which it
  // scrolls.

  const noop = () => {};

  let {
    value = '',
    onInput = noop,
    variant = 'default',
    code = false,
    invalid = false,
    disabled = false,
    readonly = false,
    autoGrow = false,
    rows = undefined,
    placeholder = '',
    ariaLabel = '',
    class: className = '',
    ...rest
  } = $props();

  const VARIANTS = new Set(['default', 'inset']);

  let element = $state();
  // The text and width the height was last fitted to.
  let fittedText = null;
  let fittedWidth = null;

  let variantClass = $derived(VARIANTS.has(variant) ? variant : 'default');
  let textAreaClass = $derived(
    [
      'text-area',
      `text-area--${variantClass}`,
      code ? 'text-area--code' : '',
      invalid ? 'text-area--invalid' : '',
      autoGrow ? 'text-area--grow' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );

  function fit() {
    if (!element) return;
    fittedText = element.value;
    fittedWidth = element.clientWidth;
    element.style.height = 'auto';
    // The field is border-box sized: its height covers the borders too.
    const border = element.offsetHeight - element.clientHeight;
    element.style.height = `${element.scrollHeight + border}px`;
  }

  // A value set by the caller (a cleared or restored draft) fits again; an
  // edit has fitted already.
  $effect(() => {
    if (!autoGrow) {
      if (element) element.style.height = '';
      return;
    }
    if (value !== fittedText) fit();
  });

  // A narrower or wider field wraps its text anew.
  $effect(() => {
    const node = element;
    if (!autoGrow || !node || typeof ResizeObserver !== 'function') return;
    const observer = new ResizeObserver(() => {
      if (node.clientWidth !== fittedWidth) fit();
    });
    observer.observe(node);
    return () => observer.disconnect();
  });

  function handleInput(event) {
    if (autoGrow) fit();
    onInput(event.currentTarget.value, event);
  }
</script>

<textarea
  bind:this={element}
  use:autosaveInput
  {...rest}
  class={textAreaClass}
  {value}
  {rows}
  {placeholder}
  {disabled}
  {readonly}
  aria-label={ariaLabel || undefined}
  aria-invalid={invalid ? 'true' : 'false'}
  oninput={handleInput}></textarea>
