<script>
  import { resolvePermissionApproval } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import Modal from './ui/Modal.svelte';

  let { snapshot = { requests: [] } } = $props();
  let selectedId = $state('');
  let busy = $state(false);
  let error = $state('');
  const requests = $derived(snapshot?.requests ?? []);
  const selected = $derived(
    requests.find((request) => request.id === selectedId),
  );

  async function decide(decision) {
    if (!selected || busy) return;
    busy = true;
    error = '';
    try {
      await resolvePermissionApproval(selected.id, decision);
      selectedId = '';
    } catch (failure) {
      if (failure?.code === 'approval_not_found') selectedId = '';
      else error = t('approvals.error');
    } finally {
      busy = false;
    }
  }

  function review(request) {
    error = '';
    selectedId = request.id;
  }
</script>

{#if requests.length}
  <Banner variant="warn" role="status">
    <span
      >{requests.length === 1
        ? t('approvals.waitingOne')
        : t('approvals.waitingMany', { count: requests.length })}</span
    >
    <Button variant="primary" onClick={() => review(requests[0])}
      >{t('approvals.review')}</Button
    >
  </Banner>
{/if}

{#if selected}
  <Modal
    title={t('approvals.title')}
    closeDisabled={busy}
    onClose={() => (selectedId = '')}
  >
    {#snippet body()}
      <div class="modal-body approval-review">
        {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
        <p>{t('approvals.explanation')}</p>
        {#if selected.display_redacted}
          <p>{t('approvals.redacted')}</p>
        {/if}
        <dl>
          <dt>{t('approvals.agent')}</dt>
          <dd>
            {selected.agent_id}{selected.project_id
              ? `@${selected.project_id}`
              : ''}
          </dd>
          <dt>{t('approvals.session')}</dt>
          <dd>{selected.session_id}</dd>
          <dt>{t('approvals.tool')}</dt>
          <dd>{selected.tool_name}</dd>
          <dt>{t('approvals.computer')}</dt>
          <dd>{selected.execution?.computer_id ?? 'server'}</dd>
          <dt>{t('approvals.reason')}</dt>
          <dd>{selected.reason}</dd>
          {#if selected.cwd}<dt>{t('approvals.cwd')}</dt>
            <dd>{selected.cwd}</dd>{/if}
        </dl>
        <h3>{t('approvals.access')}</h3>
        {#each selected.requirements ?? [] as requirement (requirement)}
          <div class="approval-review__requirement">
            <strong>{requirement.access} · {requirement.target}</strong>
            {#if requirement.resolved && requirement.resolved !== requirement.target}<div
              >
                {t('approvals.resolved')}: {requirement.resolved}
              </div>{/if}
            {#if requirement.filtered}<div>{t('approvals.filtered')}</div>
            {:else if requirement.subtree}<div>
                {t('approvals.subtree')}
              </div>{/if}
            {#if requirement.host_aliases?.length}<div>
                {t('approvals.hostAliases')}: {requirement.host_aliases.join(
                  ', ',
                )}
              </div>{/if}
            {#if requirement.glob}<div>
                {t('approvals.glob')}: {requirement.glob}
              </div>{/if}
            {#if requirement.pruned?.length}<div>
                {t('approvals.excluded')}: {requirement.pruned.join(', ')}
              </div>{/if}
            {#if requirement.git}<div>{t('approvals.git')}</div>{/if}
          </div>
        {/each}
        <h3>{t('approvals.invocation')}</h3>
        <pre>{JSON.stringify(selected.arguments, null, 2)}</pre>
        {#if requests.length > 1}
          <div class="approval-review__others">
            {#each requests.filter((request) => request.id !== selected.id) as request (request.id)}
              <Button
                variant="tertiary"
                disabled={busy}
                onClick={() => review(request)}
                >{t('approvals.other', {
                  agent: request.agent_id,
                  tool: request.tool_name,
                })}</Button
              >
            {/each}
          </div>
        {/if}
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={busy}
        onClick={() => decide('reject')}>{t('approvals.reject')}</Button
      >
      <Button variant="primary" disabled={busy} onClick={() => decide('allow')}
        >{t('approvals.allowOnce')}</Button
      >
    {/snippet}
  </Modal>
{/if}

<style>
  .approval-review {
    display: grid;
    gap: 10px;
  }
  .approval-review p,
  .approval-review h3,
  .approval-review dl {
    margin: 0;
  }
  .approval-review dl {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: 4px 12px;
  }
  .approval-review dt {
    color: var(--text-lo);
  }
  .approval-review dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .approval-review__requirement {
    overflow-wrap: anywhere;
  }
  .approval-review pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 18rem;
    overflow: auto;
  }
  .approval-review__others {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
</style>
