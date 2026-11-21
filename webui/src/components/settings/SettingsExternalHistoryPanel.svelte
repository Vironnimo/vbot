<script>
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import ExternalHistoryDialog from './ExternalHistoryDialog.svelte';

  let { agents, projects, onOpenSession } = $props();
  let open = $state(false);
</script>

<div class="s-group">
  <div class="s-row s-row--compact">
    <div class="s-row-info">
      <div class="s-row-label">{t('external.contents')}</div>
    </div>
    <div class="s-row-control">
      <Button onClick={() => (open = true)}>{t('external.open')}</Button>
    </div>
  </div>
</div>

{#if open}
  <ExternalHistoryDialog
    {agents}
    {projects}
    onClose={() => (open = false)}
    onImported={({ agent_id, session_id }) => {
      open = false;
      onOpenSession(agent_id, session_id);
    }}
  />
{/if}
