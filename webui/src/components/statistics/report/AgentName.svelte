<script>
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Badge from '../../ui/Badge.svelte';
  import { agentDisplay, agentTooltip } from '$lib/statisticsView.js';

  let { agentId } = $props();
</script>

{#if agentId}
  {@const display = agentDisplay(agentId)}
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the Agent details here.) -->
  <span class="stats-agent" tabindex="0" use:tooltip={agentTooltip(agentId)}>
    <span class="stats-agent__name">{display.name}</span>
    {#if display.projectId}
      <Badge class="stats-agent__badge" variant="info"
        >{display.projectId}</Badge
      >
    {:else if display.extension}
      <Badge class="stats-agent__badge" variant="neutral"
        >{t('statistics.agent.extensionBadge')}</Badge
      >
    {/if}
  </span>
{:else}
  <span class="stats-muted">{t('statistics.cost.withoutSession')}</span>
{/if}
