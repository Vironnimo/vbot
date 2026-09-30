<script>
  // One labelled section of expanded Tool details (Args, Stdout, Stderr,
  // Result): a Copy action, image thumbnails, and the value in a scroll box
  // of bounded height, so long output never floods the timeline.
  import {
    toolDetailImages,
    toolDetailPresentation,
  } from '$lib/chatToolDetails.js';
  import { boundedScroll } from '$lib/boundedScroll.js';
  import { t } from '$lib/i18n.js';
  import CopyButton from '../ui/CopyButton.svelte';

  let {
    label,
    value,
    isError = false,
    preferPayload = false,
    // Show arguments with the keys the Tool's display hides.
    raw = false,
    toolName = '',
    tool = null,
    // Keep showing the end of growing content, such as live command output.
    follow = false,
  } = $props();

  let images = $derived(toolDetailImages(value, { preferPayload, tool }));
  let presentation = $derived(
    toolDetailPresentation(value, { preferPayload, raw, toolName, tool }),
  );
</script>

<div
  class="teb-row teb-section"
  class:teb-section--error={isError}
  class:teb-section--success={preferPayload && !isError}
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
  {#if images.length > 0}
    <div class="tool-image-previews">
      {#each images as image (image.src)}
        <a
          class="tool-image-preview"
          href={image.src}
          target="_blank"
          rel="noreferrer"
          aria-label={image.filename}
        >
          <img
            src={image.src}
            alt={image.filename}
            loading="lazy"
            onerror={(event) => {
              event.currentTarget.hidden = true;
            }}
          />
          <span
            class="image-unavailable"
            role="img"
            aria-label={t('chat.image.unavailable')}
          >
            <svg viewBox="0 0 32 24" aria-hidden="true"
              ><rect x="1" y="1" width="30" height="22" rx="2" /><circle
                cx="10"
                cy="8"
                r="2"
              /><path d="m3 20 8-8 6 6 4-4 8 6M3 2l26 20" /></svg
            >
            <span>{t('chat.image.unavailable')}</span>
          </span>
          <span>{image.filename}</span>
        </a>
      {/each}
    </div>
  {/if}
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
