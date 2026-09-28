<script>
  import { onDestroy, untrack } from 'svelte';

  import SaveButton from '../ui/SaveButton.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import { runSettingsSave } from '$lib/settingsSave.js';

  const noop = () => {};

  // One independent switch per notification kind. Every kind is on unless the
  // user turns it off, so a settings payload without the section (an older
  // server) reads as all defaults.
  const NOTIFICATION_KINDS = Object.freeze([
    {
      key: 'run_completed',
      label: () => t('settings.notifications.runCompleted'),
      description: () => t('settings.notifications.runCompletedDescription'),
    },
    {
      key: 'run_failed',
      label: () => t('settings.notifications.runFailed'),
      description: () => t('settings.notifications.runFailedDescription'),
    },
    {
      key: 'automation_failed',
      label: () => t('settings.notifications.automationFailed'),
      description: () =>
        t('settings.notifications.automationFailedDescription'),
    },
    {
      key: 'update_result',
      label: () => t('settings.notifications.updateResult'),
      description: () => t('settings.notifications.updateResultDescription'),
    },
    {
      key: 'server_stopped',
      label: () => t('settings.notifications.serverStopped'),
      description: () => t('settings.notifications.serverStoppedDescription'),
    },
  ]);

  function getNotificationSettings(rawSettings) {
    const notifications = rawSettings?.notifications ?? {};

    return Object.fromEntries(
      NOTIFICATION_KINDS.map(({ key }) => [
        key,
        typeof notifications[key] === 'boolean' ? notifications[key] : true,
      ]),
    );
  }

  function notificationSettingsMatch(left, right) {
    return NOTIFICATION_KINDS.every(({ key }) => left[key] === right[key]);
  }

  let {
    settings = null,
    onCommit = noop,
    onToast = noop,
    onError = noop,
  } = $props();

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let notificationSettings = $state(
    untrack(() => getNotificationSettings(settings)),
  );
  let saving = $state(false);

  let saveDisabled = $derived(
    saving ||
      notificationSettingsMatch(
        notificationSettings,
        getNotificationSettings(settings),
      ),
  );
  const autosaveContext = useAutosaveContext();
  const notificationAutosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...notificationSettings }),
    hasChanges: () =>
      !notificationSettingsMatch(
        notificationSettings,
        getNotificationSettings(settings),
      ),
    save: saveNotificationSettings,
  });
  const unregisterNotificationAutosave = autosaveContext.register(
    notificationAutosave.participant,
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    notificationAutosave.scheduleRun();

    return () => {
      notificationAutosave.cancelPendingTimer();
    };
  });

  onDestroy(() => {
    unregisterNotificationAutosave();
    notificationAutosave.cancelPendingTimer();
  });

  function handleManualNotificationSettingsSave() {
    if (saving) {
      return;
    }

    if (saveDisabled) {
      onToast({
        title: t('common.alreadySaved'),
        variant: 'success',
      });
      return;
    }

    notificationAutosave.cancelPendingTimer();
    void notificationAutosave.participant.runSave('manual');
  }

  async function saveNotificationSettings(reason) {
    if (
      notificationSettingsMatch(
        notificationSettings,
        getNotificationSettings(settings),
      )
    ) {
      return true;
    }

    return runSettingsSave({
      reason,
      onCommit,
      onToast,
      onError,
      setSaving: (value) => (saving = value),
      buildPayload: () => ({ notifications: { ...notificationSettings } }),
      successTitle: t('settings.notifications.saveSuccess'),
      getDraftSnapshot: () => notificationSettings,
      applyResult: (next) =>
        (notificationSettings = getNotificationSettings(next)),
    });
  }
</script>

<p class="s-subhead__desc">{t('settings.notifications.intro')}</p>

<div class="s-group">
  {#each NOTIFICATION_KINDS as kind (kind.key)}
    <div class="s-row s-row--compact">
      <div class="s-row-info">
        <div class="s-row-label">{kind.label()}</div>
        <div class="s-row-desc">{kind.description()}</div>
      </div>
      <div class="s-row-control">
        <Toggle
          checked={notificationSettings[kind.key]}
          ariaLabel={kind.label()}
          onChange={(next) => {
            notificationSettings = {
              ...notificationSettings,
              [kind.key]: next,
            };
            onError('');
          }}
        />
      </div>
    </div>
  {/each}
</div>

<div class="s-footer">
  <SaveButton
    class="s-save-button s-save-button--inline"
    {saving}
    pending={notificationAutosave.participant.hasChanges()}
    onClick={handleManualNotificationSettingsSave}
  />
</div>
