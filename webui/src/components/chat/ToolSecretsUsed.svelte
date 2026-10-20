<script>
  // The Agent Secrets a Tool call used, on its row in the Run timeline
  // (ChatAssistantRun) and single timeline entries (ChatTimelineEntry): a key
  // and the Secret references, never a value. The tooltip names where each
  // value went (its sink).
  import { sinkLabel } from '$lib/agentSecrets.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';

  // `[{ reference, sink }]` from `toolRowPresentation(...).secretsUsed`.
  let { uses = [] } = $props();

  const references = $derived([...new Set(uses.map((use) => use.reference))]);
  const details = $derived({
    title: t('chat.secretsUsed.title'),
    rows: uses.map((use) => ({
      label: use.reference,
      value: use.sink ? sinkLabel(use.sink) : '',
      mono: true,
    })),
  });
</script>

{#if uses.length > 0}
  <!-- The references must receive focus so the sinks reach keyboard users. -->
  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
  <span
    class="te-fact te-secrets"
    tabindex="0"
    data-secrets-used={references.join(' ')}
    use:tooltip={details}
  >
    <svg
      class="te-secrets__icon"
      viewBox="0 0 16 16"
      width="12"
      height="12"
      aria-hidden="true"
      focusable="false"
    >
      <circle
        cx="5"
        cy="11"
        r="3"
        fill="none"
        stroke="currentColor"
        stroke-width="1.5"
      />
      <path
        d="M7.2 8.8 13 3m-2.2 2.2 1.6 1.6M9.4 6.6l1.2 1.2"
        fill="none"
        stroke="currentColor"
        stroke-width="1.5"
        stroke-linecap="round"
      />
    </svg>
    <span class="te-secrets__label">{references.join(', ')}</span>
  </span>
{/if}

<style>
  .te-secrets {
    display: inline-flex;
    align-items: center;
    flex-shrink: 1;
    gap: 3px;
    min-width: 0;
    max-width: 40%;
    font-family: var(--font-mono);
  }

  .te-secrets__icon {
    flex-shrink: 0;
  }

  .te-secrets__label {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
</style>
