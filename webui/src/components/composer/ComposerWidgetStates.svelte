<script>
  // The Agent widgets of the displayed Session whose state changed since the
  // Agent last received it. The next send attaches them (at most the eight
  // most recently changed); removing a chip keeps its state back until the
  // widget changes again. Laid out like the attachment tray above it.
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import { widgetStatePreview } from '$lib/chatTimelinePresentation.js';
  import {
    changedWidgetStates,
    dismissWidgetState,
  } from '$lib/widgetStates.svelte.js';

  // The composer's draft key: a Session key, or a draft's key, which never
  // holds widget states.
  let { sessionKey = '' } = $props();

  let widgets = $derived(changedWidgetStates(sessionKey));
</script>

{#if widgets.length > 0}
  <div class="attachment-tray" aria-label={t('chat.widgetState.trayLabel')}>
    {#each widgets as widget (widget.widgetId)}
      {@const name = widget.title || t('chat.widget.untitled')}
      {@const preview = widgetStatePreview(widget.state)}
      <div class="attachment-item composer-widget-state">
        <span class="attachment-file-icon" aria-hidden="true">
          <svg viewBox="0 0 16 16">
            <path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" />
            <circle cx="6" cy="4.5" r="1.5" />
            <circle cx="10.5" cy="8" r="1.5" />
            <circle cx="5" cy="11.5" r="1.5" />
          </svg>
        </span>
        <div class="attachment-meta">
          <span
            class="attachment-name"
            use:tooltip={{ text: name, whenTruncated: true }}>{name}</span
          >
          <span
            class="attachment-status composer-widget-state__preview"
            use:tooltip={{ text: preview, whenTruncated: true }}>{preview}</span
          >
        </div>
        <button
          type="button"
          class="attachment-remove"
          aria-label={t('chat.widgetState.removeNamed', { name })}
          use:tooltip={t('chat.widgetState.remove')}
          onclick={() => dismissWidgetState(sessionKey, widget.widgetId)}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path d="M4 4l8 8M12 4l-8 8" />
          </svg>
        </button>
      </div>
    {/each}
  </div>
{/if}

<style>
  .composer-widget-state .attachment-file-icon svg circle {
    fill: var(--surface-3);
  }

  .composer-widget-state__preview {
    overflow: hidden;
    font-family: var(--font-mono);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
