<script>
  import { onDestroy, untrack } from 'svelte';
  import {
    createAutosaveParticipant,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { setSessionNotificationPolicy } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import NotificationPolicySelect from './NotificationPolicySelect.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import Banner from '../ui/Banner.svelte';

  let { agentId, sessionId, policy = 'all', onCommit = () => {} } = $props();
  let baseline = $state(untrack(() => policy));
  let draft = $state(untrack(() => policy));
  let saving = $state(false);
  let error = $state('');
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
  <h4>{t('notifications.label')}</h4>
  <NotificationPolicySelect value={draft} onChange={change} />
  <p>{t('notifications.sessionHelp')}</p>
  {#if error}<Banner variant="error">{error}</Banner>{/if}
  <SaveStatus
    {saving}
    pending={participant.hasChanges()}
    onClick={() => participant.runSave('manual')}
  />
</section>

<style>
  .session-notifications {
    padding: 12px 0;
    display: grid;
    gap: 8px;
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
