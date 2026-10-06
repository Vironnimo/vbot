<script module>
  let logsEntrySequence = 0;

  function nextDetailId() {
    logsEntrySequence += 1;
    return `logs-entry-detail-${logsEntrySequence}`;
  }
</script>

<script>
  // One Logs row. The dense summary line shows the header message; a
  // multi-line entry (a traceback) adds a quiet line count. Clicking the row
  // or its disclosure button expands the whole entry below the summary as
  // selectable preformatted text; Copy carries the verbatim source lines.
  import Button from '../ui/Button.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';

  let { entry, levelLabel } = $props();

  const detailId = nextDetailId();
  let expanded = $state(false);

  let continuationLineCount = $derived(
    entry.continuation ? entry.continuation.split('\n').length : 0,
  );
  let body = $derived(
    entry.continuation
      ? `${entry.message}\n${entry.continuation}`
      : entry.message,
  );
  let summary = $derived(entry.message.replace(/\s+/g, ' ').trim());
  // Prefer the verbatim source line(s) the backend captured so the clipboard
  // gets the entry exactly as written to the file; fall back to the body only
  // if an entry somehow lacks it.
  let copyText = $derived(
    typeof entry.raw === 'string' && entry.raw ? entry.raw : body,
  );

  function levelTone(level) {
    switch (level) {
      case 'error':
      case 'critical':
        return 'logs-entry--error';
      case 'warn':
      case 'warning':
        return 'logs-entry--warn';
      case 'info':
        return 'logs-entry--info';
      default:
        return 'logs-entry--neutral';
    }
  }

  function toggleFromRow(event) {
    // Controls act on their own, the expanded text stays selectable, and a
    // drag that selected text is not a toggle.
    if (
      event.target.closest('button, a, input, .logs-entry__detail') ||
      window.getSelection()?.toString()
    ) {
      return;
    }
    expanded = !expanded;
  }
</script>

<!-- svelte-ignore a11y_click_events_have_key_events (Pointer shortcut for the row; the disclosure button is the keyboard control.) -->
<article
  class={`logs-entry ${levelTone(entry.level)}`}
  class:logs-entry--multiline={continuationLineCount > 0}
  class:logs-entry--expanded={expanded}
  role="listitem"
  onclick={toggleFromRow}
>
  <Button
    variant="tertiary"
    icon
    class="logs-entry__toggle"
    ariaLabel={t('logs.entryDetails')}
    aria-expanded={expanded}
    aria-controls={expanded ? detailId : undefined}
    onClick={() => (expanded = !expanded)}
  >
    <span
      class="disclosure-chevron"
      class:disclosure-chevron--open={expanded}
      aria-hidden="true"
    ></span>
  </Button>
  <span class="logs-entry__timestamp">{entry.timestamp || '—'}</span>
  <span class="logs-entry__level">{levelLabel}</span>
  <span
    class="logs-entry__logger"
    use:tooltip={{
      text: entry.logger_name,
      mono: true,
      whenTruncated: true,
    }}>{entry.logger_name || t('common.unknown')}</span
  >
  <span class="logs-entry__message">
    <span class="logs-entry__summary">{summary}</span>
    {#if continuationLineCount > 0}
      <span class="logs-entry__more"
        >{continuationLineCount === 1
          ? t('logs.moreLinesOne')
          : t('logs.moreLines', {
              count: continuationLineCount,
            })}</span
      >
    {/if}
  </span>
  <CopyButton
    class="logs-entry__copy"
    text={copyText}
    label={t('logs.copyEntry')}
    copiedLabel={t('logs.copied')}
  />
  {#if expanded}
    <pre id={detailId} class="logs-entry__detail">{body}</pre>
  {/if}
</article>

<style>
  /* Routine rows carry no marker; only warnings and errors get a thin left
     marker in their level color, and only errors a faint row tint, so a busy
     file does not turn the whole list amber. */
  .logs-entry {
    display: grid;
    grid-template-columns:
      20px minmax(154px, auto) minmax(60px, auto) var(--logs-logger-width)
      minmax(0, 1fr) auto;
    align-items: center;
    gap: 4px 10px;
    min-width: 0;
    padding: 1px 10px 1px 2px;
    border-left: 2px solid transparent;
    cursor: pointer;
  }

  .logs-entry:hover,
  .logs-entry--expanded {
    background: var(--surface-2);
  }

  .logs-entry--warn {
    border-left-color: var(--amber);
  }

  .logs-entry--error {
    border-left-color: var(--red);
    background: var(--red-dim);
  }

  .logs-entry--error:hover,
  .logs-entry--error.logs-entry--expanded {
    background: color-mix(in srgb, var(--red-dim), var(--surface-2));
  }

  .logs-entry__timestamp,
  .logs-entry__logger,
  .logs-entry__summary {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .logs-entry__timestamp,
  .logs-entry__logger {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }

  /* Level color: DEBUG and unknown levels stay quiet, INFO is neutral, and
     only WARN/ERROR carry status color. */
  .logs-entry__level {
    justify-self: start;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    font-weight: 600;
  }

  .logs-entry--info .logs-entry__level {
    color: var(--text-med);
  }

  .logs-entry--warn .logs-entry__level {
    color: var(--amber);
  }

  .logs-entry--error .logs-entry__level {
    color: var(--red);
  }

  .logs-entry__message {
    display: flex;
    min-width: 0;
    align-items: baseline;
    gap: 8px;
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    line-height: 1.4;
  }

  .logs-entry__summary {
    color: var(--text-hi);
  }

  .logs-entry__more {
    flex: none;
    color: var(--text-lo);
    white-space: nowrap;
  }

  /* Row controls share one compact footprint; the shared button's minimum
     height is overridden too so they do not stretch dense rows. The
     disclosure stays visible where it matters (multi-line or expanded
     entries) and otherwise appears with Copy on hover or keyboard focus. */
  .logs-entry :global(.logs-entry__toggle),
  .logs-entry :global(.logs-entry__copy) {
    width: 24px;
    height: 24px;
    min-height: 24px;
    padding: 2px;
    opacity: 0;
    transition: opacity 120ms ease;
  }

  .logs-entry :global(.logs-entry__toggle) {
    width: 20px;
    height: 20px;
    min-height: 20px;
    color: var(--text-lo);
  }

  .logs-entry :global(.logs-entry__toggle .disclosure-chevron) {
    margin: 0 0 0 -2px;
    color: inherit;
  }

  .logs-entry :global(.logs-entry__toggle .disclosure-chevron--open) {
    margin: 0 0 3px;
  }

  .logs-entry :global(.logs-entry__copy) {
    justify-self: end;
  }

  .logs-entry:hover :global(.logs-entry__toggle),
  .logs-entry:hover :global(.logs-entry__copy),
  .logs-entry--multiline :global(.logs-entry__toggle),
  .logs-entry--expanded :global(.logs-entry__toggle),
  .logs-entry :global(.logs-entry__toggle:focus-visible),
  .logs-entry :global(.logs-entry__copy:focus-visible) {
    opacity: 1;
  }

  .logs-entry:hover :global(.logs-entry__toggle) {
    color: var(--text-hi);
  }

  /* The expanded entry: the complete message and continuation, line breaks
     preserved and wrapped, inset below the summary like a code block. */
  .logs-entry__detail {
    grid-column: 2 / -1;
    min-width: 0;
    margin: 2px 0 8px;
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    color: var(--text-hi);
    background: var(--bg);
    cursor: text;
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    line-height: 1.55;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
  }

  @media (prefers-reduced-motion: reduce) {
    .logs-entry :global(.logs-entry__toggle),
    .logs-entry :global(.logs-entry__copy) {
      transition: none;
    }
  }

  @media (max-width: 1080px) {
    .logs-entry {
      grid-template-columns:
        20px minmax(140px, auto) minmax(64px, auto) minmax(0, 1fr)
        auto;
    }

    .logs-entry :global(.logs-entry__toggle) {
      grid-column: 1;
      grid-row: 1 / span 2;
    }

    .logs-entry__logger {
      grid-column: 2 / span 2;
      grid-row: 2;
      color: var(--text-med);
    }

    .logs-entry__message {
      grid-column: 4;
      grid-row: 1 / span 2;
      align-self: center;
    }

    .logs-entry :global(.logs-entry__copy) {
      grid-column: 5;
      grid-row: 1 / span 2;
      align-self: center;
    }
  }

  @media (max-width: 960px) {
    .logs-entry {
      grid-template-columns: 20px minmax(0, 1fr);
      gap: 6px 10px;
      padding: 6px 10px 6px 2px;
    }

    /* The summary already wraps here, so the expanded entry replaces it
       instead of repeating it. */
    .logs-entry--expanded .logs-entry__message {
      display: none;
    }

    .logs-entry__timestamp,
    .logs-entry__level,
    .logs-entry__logger,
    .logs-entry__message,
    .logs-entry__detail,
    .logs-entry :global(.logs-entry__copy) {
      grid-column: 2;
      grid-row: auto;
    }

    .logs-entry :global(.logs-entry__toggle) {
      grid-column: 1;
      grid-row: 1;
      align-self: start;
    }

    .logs-entry :global(.logs-entry__toggle),
    .logs-entry :global(.logs-entry__copy) {
      opacity: 1;
    }

    .logs-entry :global(.logs-entry__copy) {
      order: 1;
      justify-self: start;
    }

    .logs-entry__message {
      display: block;
    }

    .logs-entry__more {
      margin-left: 8px;
    }

    .logs-entry__timestamp,
    .logs-entry__logger,
    .logs-entry__summary {
      white-space: normal;
      text-overflow: clip;
    }
  }
</style>
