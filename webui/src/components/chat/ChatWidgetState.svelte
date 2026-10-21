<script>
  // The widget states a send attached, shown before the User message they
  // went with: a Run child or a History row of type `widget_state`. Each
  // widget is one collapsed line with its title and a preview of its values;
  // expanding shows the values in full, as the Agent received them.
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import {
    widgetStateEntries,
    widgetStateJson,
    widgetStatePreview,
  } from '$lib/chatTimelinePresentation.js';
  import { timelineViewState } from './timelineViewState.svelte.js';

  let { item } = $props();

  // Disclosure state lives in the timeline's view state, so it survives this
  // row being unmounted and mounted again.
  const viewState = timelineViewState();

  let entries = $derived(widgetStateEntries(item));

  const disclosureKey = (entry, index) =>
    `widget-state:${item.id}:${entry.widgetId || index}`;
</script>

{#each entries as entry, index (entry.widgetId || index)}
  {@const key = disclosureKey(entry, index)}
  {@const preview = widgetStatePreview(entry.state)}
  <details
    class="tool-event run-tool-event widget-state-row"
    open={viewState.isOpen(key)}
    ontoggle={(event) => viewState.setOpen(key, event.currentTarget.open)}
  >
    <summary class="tool-event-line">
      <svg
        class="widget-state-row__icon"
        viewBox="0 0 16 16"
        width="13"
        height="13"
        aria-hidden="true"
      >
        <path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" />
        <circle cx="6" cy="4.5" r="1.5" />
        <circle cx="10.5" cy="8" r="1.5" />
        <circle cx="5" cy="11.5" r="1.5" />
      </svg>
      <span class="te-fn">{t('chat.widgetState.label')}</span>
      {#if entry.title}
        <span class="widget-state-row__title">{entry.title}</span>
      {/if}
      <span
        class="te-arg widget-state-row__preview"
        use:tooltip={{ text: preview, whenTruncated: true }}>{preview}</span
      >
    </summary>
    <div class="tool-event-body">
      <pre class="widget-state-row__values">{widgetStateJson(entry.state)}</pre>
    </div>
  </details>
{/each}

<style>
  .widget-state-row__icon {
    flex-shrink: 0;
    color: var(--text-lo);
    fill: var(--surface);
    stroke: currentColor;
    stroke-linecap: round;
    stroke-width: 1.3;
  }

  .widget-state-row__title {
    flex-shrink: 0;
    max-width: 40%;
    overflow: hidden;
    color: var(--text-med);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .widget-state-row__preview {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .widget-state-row__values {
    max-height: 240px;
    margin: 0;
    overflow: auto;
    color: var(--text-med);
    font: var(--fs-mono-xs) var(--font-mono);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
</style>
