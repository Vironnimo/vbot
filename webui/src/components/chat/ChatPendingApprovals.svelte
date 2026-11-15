<script>
  import { t } from '$lib/i18n.js';
  import { qualifyAgentAddress } from '$lib/agentAddress.js';
  import { approvalSessionKey } from '$lib/permissionApprovals.js';
  import ChatApprovalCard from './ChatApprovalCard.svelte';

  let { requests, sessionKey, onOpenSession } = $props();
</script>

{#if requests.length}
  <details class="pending-approvals" aria-label={t('approvals.title')} open>
    <summary aria-live="polite">
      {requests.length === 1
        ? t('approvals.pendingOne')
        : t('approvals.pendingMany', { count: requests.length })}
    </summary>
    <div class="pending-approvals__requests">
      {#each requests as request (request.id)}
        {@const agentId = qualifyAgentAddress(
          request.agent_id,
          request.project_id,
        )}
        {@const own = approvalSessionKey(request) === sessionKey}
        <ChatApprovalCard
          {request}
          subAgent={own ? '' : agentId}
          onOpenSession={own
            ? null
            : () => onOpenSession({ agentId, sessionId: request.session_id })}
        />
      {/each}
    </div>
  </details>
{/if}

<style>
  .pending-approvals {
    border-left: 3px solid var(--amber);
    background: var(--surface-1);
  }

  summary {
    padding: 10px 14px;
    color: var(--text-hi);
    font-weight: 600;
    cursor: pointer;
  }

  .pending-approvals__requests {
    display: grid;
    gap: 8px;
    max-height: min(40vh, 360px);
    overflow-y: auto;
    padding: 0 10px 10px;
  }
</style>
