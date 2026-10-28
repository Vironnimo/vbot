<script>
  import { onDestroy, untrack } from 'svelte';
  import {
    createAutosaveParticipant,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { setSessionNotificationPolicy } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import NotificationPolicySelect, {
    policyLabel,
  } from './NotificationPolicySelect.svelte';
  import Button from '../ui/Button.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import Banner from '../ui/Banner.svelte';

  let { agentId, sessionId, policy = 'all', onCommit = () => {} } = $props();
  let baseline = $state(untrack(() => policy));
  let draft = $state(untrack(() => policy));
  let saving = $state(false);
  let error = $state('');
  // The setting stays folded: it is rarely changed. The fold lives here, not
  // in the Session panel, so the autosave participant stays registered while
  // it is closed. A failed save keeps it open so its error stays in view.
  let open = $state(false);
  let expanded = $derived(open || error !== '');
  const id = $props.id();
  const bodyId = `${id}-body`;
  $effect(() => {
    const next = policy;
    untrack(() => {
      if (draft === baseline) draft = next;
      baseline = next;
    });
  });
  const participant = createAutosaveParticipant({
    getSnapshot: () => ({ agentId, sessionId, policy: draft }),
    hasChanges: () => draft !== baseline,
    save: async () => {
      const submitted = draft;
      saving = true;
      try {
        const result = await setSessionNotificationPolicy(
          agentId,
          sessionId,
          submitted,
        );
        baseline = result.session.notification_policy;
        onCommit(result.session);
        error = '';
        return true;
      } catch (failure) {
        error = failure.message;
        return false;
      } finally {
        saving = false;
      }
    },
  });
  const unregister = useAutosaveContext().register(participant);
  onDestroy(unregister);
  function change(value) {
    draft = value;
    void participant.runSave('auto');
  }
</script>

<section class="session-notifications">
  <Button
    variant="tertiary"
    class="session-notifications__toggle"
    aria-expanded={expanded}
    aria-controls={expanded ? bodyId : undefined}
    onClick={() => (open = !open)}
  >
    <svg
      class="session-notifications__chevron"
      class:open={expanded}
      viewBox="0 0 16 16"
      width="10"
      height="10"
      aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg
    >
    {t('notifications.advanced')}
    <!-- A policy other than the default must not hide behind the fold. -->
    {#if !expanded && draft !== 'all'}
      <span class="session-notifications__hint"
        >{t('notifications.hint', { policy: policyLabel(draft) })}</span
      >
    {/if}
  </Button>
  {#if expanded}
    <div id={bodyId} class="session-notifications__body">
      <h4>{t('notifications.label')}</h4>
      <NotificationPolicySelect value={draft} onChange={change} />
      <p>{t('notifications.sessionHelp')}</p>
      {#if error}<Banner variant="error">{error}</Banner>{/if}
      <SaveStatus
        {saving}
        pending={participant.hasChanges()}
        onClick={() => participant.runSave('manual')}
      />
    </div>
  {/if}
</section>

<style>
  .session-notifications {
    padding: 4px 0 0;
    display: grid;
    gap: 8px;
  }
  .session-notifications :global(.btn-tertiary.session-notifications__toggle) {
    min-height: 26px;
    width: 100%;
    justify-content: flex-start;
    gap: 6px;
    padding: 0 4px;
    border: 0;
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    font-weight: 500;
  }
  .session-notifications
    :global(.btn-tertiary.session-notifications__toggle:hover) {
    background: var(--surface-2);
    color: var(--text-hi);
  }
  .session-notifications__chevron {
    fill: none;
    stroke: currentColor;
    stroke-width: 1.5;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .session-notifications__chevron.open {
    transform: rotate(90deg);
  }
  .session-notifications__hint {
    margin-left: auto;
    color: var(--text-lo);
    font-weight: 400;
  }
  .session-notifications__body {
    display: grid;
    gap: 8px;
    padding: 0 4px;
  }
  h4,
  p {
    margin: 0;
  }
  p {
    color: var(--text-lo);
    font-size: 12px;
  }
</style>
