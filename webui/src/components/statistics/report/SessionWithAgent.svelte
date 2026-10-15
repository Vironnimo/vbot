<script>
  import AgentName from './AgentName.svelte';
  import SessionName from './SessionName.svelte';

  let { row } = $props();
</script>

<span class="stats-stack">
  <SessionName {row} />
  <span class="stats-stack__secondary"
    ><AgentName agentId={row.agent_id} /></span
  >
</span>
