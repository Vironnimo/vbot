<script>
  import { getContext } from 'svelte';
  import ChatView from './ChatView.svelte';
  import { formatAgentAddress, parseAgentAddress } from '$lib/agentAddress.js';

  // Bind a native Chat surface to one canonical address, including hidden
  // owners. The host supplies its event feed once, at the App boundary.
  let {
    address,
    active = true,
    readOnly = false,
    monitor = null,
    hideOpenFull = false,
    getUIContext = null,
    presentation = 'conversation',
  } = $props();
  const getEvents = getContext('sessionViewEvents');
  const events = $derived(getEvents?.() ?? {});
  const agentAddress = $derived(
    formatAgentAddress(address?.agent_id, address?.project_id),
  );
  const sessionId = $derived(address?.session_id);
  function openSession({ agentId: qualified, sessionId }) {
    const { agentId, projectId } = parseAgentAddress(qualified);
    return events.onNavigateToSession?.({
      agent_id: agentId,
      session_id: sessionId,
      ...(projectId ? { project_id: projectId } : {}),
    });
  }
</script>

{#if agentAddress && sessionId}
  {#key JSON.stringify([agentAddress, sessionId, readOnly])}
    <ChatView
      {...events}
      {active}
      {readOnly}
      {monitor}
      {presentation}
      {getUIContext}
      boundSession={{ agentAddress, sessionId }}
      onOpenFull={!hideOpenFull && events.onNavigateToSession
        ? openSession
        : null}
      navigateToSubAgent={openSession}
      draftScope="embed"
      chatWidth="full"
    />
  {/key}
{/if}
