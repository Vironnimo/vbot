<script>
  import { t } from '$lib/i18n.js';
  import { reasoningMarkdownSource } from '$lib/markdown.js';
  import { reasoningSummaryTitle } from '$lib/chatTimelinePresentation.js';
  import CopyButton from '../ui/CopyButton.svelte';
  import MarkdownContent from './MarkdownContent.svelte';

  let {
    source = '',
    summary = null,
    working = false,
    open = false,
    durationLabel = '',
    onOpenChange = () => {},
  } = $props();
  const sections = $derived(
    Array.isArray(summary) ? summary.filter((text) => text.trim()) : [],
  );
  // Compatible Providers may mix a summary with additional readable reasoning.
  // Keep that complete text visible through the existing fallback.
  const isSummary = $derived(
    sections.length > 0 && source.trim() === sections.join('\n\n').trim(),
  );
  const title = $derived(
    isSummary ? reasoningSummaryTitle(sections.at(-1)) : '',
  );
  const label = $derived(
    isSummary
      ? t('chat.reasoning.summary', 'Reasoning summary')
      : working
        ? t('chat.reasoning.active', 'thinking...')
        : t('chat.reasoning.done', 'thought'),
  );
  const copyText = $derived(
    reasoningMarkdownSource(isSummary ? sections.join('\n\n') : source),
  );
</script>

<details
  class="reasoning-block"
  class:reasoning-summary={isSummary}
  {open}
  ontoggle={(event) => onOpenChange(event.currentTarget.open)}
>
  <summary
    class="reasoning-header"
    aria-label={title ? `${label}: ${title}` : label}
  >
    <svg class="reasoning-icon" viewBox="0 0 16 16" aria-hidden="true">
      <path
        d="M8 2a4 4 0 0 0-4 4c0 1.5.8 2.8 2 3.5V11h4V9.5A4 4 0 0 0 12 6a4 4 0 0 0-4-4z"
      />
      <path d="M6 13h4" />
    </svg>
    <span class="reasoning-summary__label">{label}</span>
    {#if title}<span class="reasoning-summary__title">{title}</span>{/if}
    {#if isSummary && sections.length > 1}
      <span class="reasoning-summary__count"
        >{t('chat.reasoning.sections', '{count} sections', {
          count: sections.length,
        })}</span
      >
    {:else if !isSummary && durationLabel}
      <span class="reasoning-duration">{durationLabel}</span>
    {/if}
    {#if working}<span class="streaming-caret" aria-hidden="true"></span>{/if}
    <svg
      class="r-chevron"
      viewBox="0 0 16 16"
      width="10"
      height="10"
      aria-hidden="true"
    >
      <path d="m4 6 4 4 4-4" />
    </svg>
  </summary>
  <div class="reasoning-body">
    <div class="reasoning-body__actions">
      <CopyButton
        text={copyText}
        class="chat-copy-action reasoning-copy"
        label={isSummary
          ? t('chat.reasoning.copySummary', 'Copy reasoning summary')
          : t('chat.copyReasoning', 'Copy thinking')}
        copiedLabel={isSummary
          ? t('chat.reasoning.summaryCopied', 'Summary copied')
          : t('chat.reasoningCopied', 'Thinking copied')}
      />
    </div>
    {#if isSummary}
      {#each sections as section, index (index)}
        <div class="reasoning-summary__section">
          <MarkdownContent
            source={section}
            streaming={working && index === sections.length - 1}
            reasoning
            class="reasoning-markdown"
          />
        </div>
      {/each}
    {:else}
      <MarkdownContent
        {source}
        streaming={working}
        reasoning
        class="reasoning-markdown"
      />
    {/if}
  </div>
</details>

<style>
  .reasoning-summary > .reasoning-header {
    flex-wrap: nowrap;
    gap: 7px;
  }
  .reasoning-summary__label {
    flex-shrink: 0;
  }
  .reasoning-summary .reasoning-summary__label {
    color: var(--text-med);
    font-size: 11px;
  }
  .reasoning-summary__title {
    color: var(--text-med);
    font-family: var(--font-ui);
    font-size: 12px;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .reasoning-summary__count {
    flex-shrink: 0;
    color: var(--text-med);
    font-size: 10px;
    white-space: nowrap;
  }
  .reasoning-summary__section + .reasoning-summary__section {
    margin-top: 16px;
    padding-top: 12px;
    border-top: 1px solid var(--border);
  }
  .reasoning-summary > .reasoning-body {
    max-width: 720px;
    font-style: normal;
  }
  .reasoning-header:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
    border-radius: 3px;
  }
</style>
