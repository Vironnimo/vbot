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
  import { createSessionActions } from './actions.svelte.js';
  import { autofocusRename } from './presentation.js';
  import SessionCompactionPolicyDialog from './SessionCompactionPolicyDialog.svelte';

  let {
    agentId,
    sessionId,
    session = null,
    policy = 'all',
    onCommit = () => {},
    onChanged = () => {},
    onCompactionPolicyChange = () => {},
  } = $props();
  const actions = createSessionActions({
    get agentId() {
      return agentId;
    },
    closeMenu() {},
    loadSessions: () => onChanged(),
    onCompactionPolicyChange: (...args) => onCompactionPolicyChange(...args),
  });
  let baseline = $state(untrack(() => policy));
  let draft = $state(untrack(() => policy));
  let saving = $state(false);
  let error = $state('');
  // These settings stay folded: they are rarely changed. The fold lives here, not
  // in the Session panel, so the autosave participant stays registered while
  // it is closed. A failed save keeps it open so its error stays in view.
  let open = $state(false);
  let expanded = $derived(
    open || error !== '' || actions.editingSessionId !== null,
  );
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

<section class="session-advanced">
  <Button
    variant="tertiary"
    class="session-advanced__toggle"
    aria-expanded={expanded}
    aria-controls={expanded ? bodyId : undefined}
    onClick={() => (open = !open)}
  >
    <svg
      class="session-advanced__chevron"
      class:open={expanded}
      viewBox="0 0 16 16"
      width="10"
      height="10"
      aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg
    >
    {t('sessions.advanced')}
    <!-- A policy other than the default must not hide behind the fold. -->
    {#if !expanded && draft !== 'all'}
      <span class="session-advanced__hint"
        >{t('notifications.hint', { policy: policyLabel(draft) })}</span
      >
    {/if}
  </Button>
  {#if expanded}
    <div id={bodyId} class="session-advanced__body">
      {#if actions.editingSessionId}
        <input
          class="s-input"
          type="text"
          value={actions.editValue}
          maxlength={actions.SESSION_TITLE_MAX_LENGTH}
          placeholder={t('sessions.rename_placeholder')}
          aria-label={t('sessions.rename_label')}
          disabled={actions.renameSaving}
          oninput={(event) => (actions.editValue = event.currentTarget.value)}
          onkeydown={actions.handleRenameKeydown}
          use:autofocusRename
        />
        <div class="session-advanced__actions">
          <Button
            variant="secondary"
            disabled={actions.renameSaving}
            onClick={actions.cancelRename}
          >
            {t('common.cancel')}
          </Button>
          <Button
            variant="secondary"
            disabled={actions.renameSaving}
            onClick={actions.submitRename}
          >
            {actions.renameSaving ? t('common.saving') : t('common.save')}
          </Button>
        </div>
        {#if actions.renameError}<Banner variant="error" role="alert"
            >{actions.renameError}</Banner
          >{/if}
      {:else}
        <Button
          variant="secondary"
          disabled={!session || actions.renameSaving}
          onClick={() => actions.startRename(session)}
        >
          {t('sessions.rename')}
        </Button>
      {/if}
      <Button
        variant="secondary"
        disabled={!session || actions.policySaving}
        onClick={() => actions.startPolicyEdit(session)}
      >
        {t('sessions.compactionPolicy')}
      </Button>
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

<SessionCompactionPolicyDialog {actions} />

<style>
  .session-advanced__actions {
    display: flex;
    justify-content: flex-end;
    gap: 6px;
  }
  .session-advanced {
    padding: 4px 0 0;
    display: grid;
    gap: 8px;
  }
  .session-advanced :global(.btn-tertiary.session-advanced__toggle) {
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
  .session-advanced :global(.btn-tertiary.session-advanced__toggle:hover) {
    background: var(--surface-2);
    color: var(--text-hi);
  }
  .session-advanced__chevron {
    fill: none;
    stroke: currentColor;
    stroke-width: 1.5;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .session-advanced__chevron.open {
    transform: rotate(90deg);
  }
  .session-advanced__hint {
    margin-left: auto;
    color: var(--text-lo);
    font-weight: 400;
  }
  .session-advanced__body {
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
