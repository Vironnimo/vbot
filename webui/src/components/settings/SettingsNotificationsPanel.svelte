<script>
  import { onDestroy, untrack } from 'svelte';

  import InfoHint from '../ui/InfoHint.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
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
    },
    {
      key: 'run_failed',
      label: () => t('settings.notifications.runFailed'),
    },
    {
      key: 'automation_failed',
      label: () => t('settings.notifications.automationFailed'),
    },
    {
      key: 'update_result',
      label: () => t('settings.notifications.updateResult'),
    },
    {
      key: 'server_stopped',
      label: () => t('settings.notifications.serverStopped'),
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

  let { settings = null, onCommit = noop, onError = noop } = $props();

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

  async function saveNotificationSettings() {
    if (
      notificationSettingsMatch(
        notificationSettings,
        getNotificationSettings(settings),
      )
    ) {
      return true;
    }

    return runSettingsSave({
      onCommit,
      onError,
      setSaving: (value) => (saving = value),
      buildPayload: () => ({ notifications: { ...notificationSettings } }),
      getDraftSnapshot: () => notificationSettings,
      applyResult: (next) =>
        (notificationSettings = getNotificationSettings(next)),
    });
  }
</script>

<!-- Which app shows these is the one fact a reader needs up front; what each
     kind covers is in the "?". -->
<div class="s-subhead__desc notifications-intro">
  {t('settings.notifications.intro')}
  <InfoHint text={t('settings.notifications.help')} />
</div>

<div class="s-group">
  {#each NOTIFICATION_KINDS as kind (kind.key)}
    <div class="s-row s-row--compact">
      <div class="s-row-info">
        <div class="s-row-label">{kind.label()}</div>
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
  <SaveStatus
    {saving}
    pending={notificationAutosave.participant.hasChanges()}
    onClick={() => notificationAutosave.participant.runSave('manual')}
  />
</div>

<style>
  /* The intro line sits directly under the section heading. */
  .notifications-intro {
    display: flex;
    align-items: center;
    gap: 6px;
    margin: -4px 0 0;
  }
</style>
