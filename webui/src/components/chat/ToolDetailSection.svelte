<script>
  // One labelled section of expanded Tool details (Args, Stdout, Stderr,
  // Result): a Copy action and the value in a scroll box
  // of bounded height, so long output never floods the timeline.
  import { toolDetailPresentation } from '$lib/chatToolDetails.js';
  import { boundedScroll } from '$lib/boundedScroll.js';
  import { t } from '$lib/i18n.js';
  import CopyButton from '../ui/CopyButton.svelte';

  let {
    label,
    value,
    isError = false,
    // Arguments hide display-owned keys; raw keeps them, literal skips JSON
    // parsing, and result selects the Tool's user-facing result payload.
    mode = 'arguments',
    toolName = '',
    tool = null,
    // Keep showing the end of growing content, such as live command output.
    follow = false,
  } = $props();

  let presentation = $derived(
    toolDetailPresentation(value, {
      mode,
      toolName,
      tool,
    }),
  );
</script>

<div
  class="teb-row teb-section"
  class:teb-section--error={isError}
  class:teb-section--success={mode === 'result' && !isError}
>
  <div class="teb-section-header">
    <span class="teb-label">{label}</span>
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
          class:error={isError}
          class={`teb-code teb-text teb-text--${presentation.kind}`}
          >{presentation.text}</span
        >
      {/if}
    </div>
  {/if}
</div>
