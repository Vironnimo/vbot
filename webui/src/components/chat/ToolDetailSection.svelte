<script>
  // One labelled section of expanded Tool details (Args, Stdout, Stderr,
  // Result): a Copy action and the value in a scroll box
  // of bounded height, so long output never floods the timeline. A section
  // with `clampLines` (a Bash command) shows only that many lines until the
  // reader asks for the rest; Copy always takes the complete value.
  import { toolDetailPresentation } from '$lib/chatToolDetails.js';
  import { boundedScroll } from '$lib/boundedScroll.js';
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import CopyButton from '../ui/CopyButton.svelte';

  const FALLBACK_LINE_HEIGHT_PX = 20;

  let {
    label,
    value,
    isError = false,
    // Arguments hide display-owned keys; raw keeps them, literal skips JSON
    // parsing, result selects the Tool's user-facing result payload and
    // raw-result keeps every field the Tool returned.
    mode = 'arguments',
    toolName = '',
    tool = null,
    // Keep showing the end of growing content, such as live command output.
    follow = false,
    // Lines a text value shows while collapsed; 0 shows it whole.
    clampLines = 0,
    expanded = false,
    onExpandedChange = () => {},
  } = $props();

  let presentation = $derived(
    toolDetailPresentation(value, {
      mode,
      toolName,
      tool,
    }),
  );

  let textNode = $state();
  let clampable = $state(false);
  let clamped = $derived(clampable && !expanded);

  // Wrapped lines count: a one-line command wider than the box clamps too.
  function measure() {
    if (!textNode || clampLines <= 0) return;
    const lineHeight =
      parseFloat(getComputedStyle(textNode).lineHeight) ||
      FALLBACK_LINE_HEIGHT_PX;
    clampable = textNode.scrollHeight > lineHeight * clampLines + 1;
  }

  $effect(() => {
    presentation;
    const node = textNode;
    if (!node || clampLines <= 0) return undefined;
    measure();
    if (typeof ResizeObserver !== 'function') return undefined;
    // The clamp keeps scrollHeight at the full text, so it cannot oscillate.
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  });
</script>

<div
  class="teb-row teb-section"
  class:teb-section--error={isError}
  class:teb-section--success={(mode === 'result' || mode === 'raw-result') &&
    !isError}
>
  <div class="teb-section-header">
    <span class="teb-label">{label}</span>
    {#if clampable}
      <Button
        variant="tertiary"
        class="teb-clamp-toggle"
        aria-expanded={expanded}
        onClick={() => onExpandedChange(!expanded)}
      >
        {expanded ? t('chat.showLess') : t('chat.showMore')}
      </Button>
    {/if}
    {#if presentation.kind !== 'empty'}
      <CopyButton
        text={presentation.copyText}
        class="chat-copy-action tool-detail-copy"
        label={t('chat.copyToolField', { label })}
      />
    {/if}
  </div>
  {#if presentation.kind === 'empty'}
    <span class="teb-code teb-text teb-text--empty">{presentation.text}</span>
  {:else}
    <div
      class="teb-scroll"
      role="group"
      aria-label={label}
      use:boundedScroll={{ follow }}
    >
      {#if presentation.kind === 'fields'}
        <div class:error={isError} class="teb-code teb-fields">
          {#each presentation.fields as field (field.key)}
            <div class="teb-field">
              <span class="teb-field-key">{field.key}</span>
              <span
                class:error={isError}
                class={`teb-field-value teb-field-value--${field.kind}`}
                >{field.text}</span
              >
            </div>
          {/each}
        </div>
      {:else}
        <span
          bind:this={textNode}
          class:error={isError}
          class:teb-text--clamped={clamped}
          style:--teb-clamp-lines={clampLines > 0 ? clampLines : null}
          class={`teb-code teb-text teb-text--${presentation.kind}`}
          >{presentation.text}</span
        >
      {/if}
    </div>
  {/if}
</div>
