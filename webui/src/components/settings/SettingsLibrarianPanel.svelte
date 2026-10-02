<script>
  import { onDestroy, untrack } from 'svelte';

  import InfoHint from '../ui/InfoHint.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import { createSettingsDraft } from '$lib/settingsSave.js';

  // The `librarian` settings section: scheduled Skill maintenance, its
  // interval, when unused background-made Skills are retired, and whether a
  // pass merges overlapping Skills. A pass started by hand from the Skills
  // manager uses the last two even while the schedule is off.

  const noop = () => {};

  const LIBRARIAN_SETTING_DEFAULTS = Object.freeze({
    enabled: true,
    interval_days: 7,
    archive_after_days: 90,
    consolidate: true,
  });
  const SWITCH_FIELDS = ['enabled', 'consolidate'];
  const DAY_FIELDS = ['interval_days', 'archive_after_days'];

  function positiveIntegerOr(value, fallback) {
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed >= 1 ? parsed : fallback;
  }

  function getLibrarianSettings(rawSettings) {
    const librarian = rawSettings?.librarian ?? {};
    const result = {};
    for (const field of SWITCH_FIELDS)
      result[field] =
        typeof librarian[field] === 'boolean'
          ? librarian[field]
          : LIBRARIAN_SETTING_DEFAULTS[field];
    for (const field of DAY_FIELDS)
      result[field] = positiveIntegerOr(
        librarian[field],
        LIBRARIAN_SETTING_DEFAULTS[field],
      );
    return result;
  }

  let { settings = null, onCommit = noop, onError = noop } = $props();

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let librarianSettings = $state(untrack(() => getLibrarianSettings(settings)));
  let saving = $state(false);
  const librarianDraft = createSettingsDraft({
    settings: untrack(() => settings),
    fromSettings: getLibrarianSettings,
    read: () => librarianSettings,
    write: (next) => (librarianSettings = next),
    toPayload: (values) => ({
      librarian: getLibrarianSettings({ librarian: values }),
    }),
  });

  let saveDisabled = $derived(
    saving ||
      librarianSettingsMatch(librarianSettings, getLibrarianSettings(settings)),
  );
  const autosaveContext = useAutosaveContext();
  const librarianAutosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...librarianSettings }),
    hasChanges: () =>
      !librarianSettingsMatch(
        librarianSettings,
        getLibrarianSettings(settings),
      ),
    save: saveLibrarianSettings,
  });
  const unregisterLibrarianAutosave = autosaveContext.register(
    librarianAutosave.participant,
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    librarianAutosave.scheduleRun();

    return () => {
      librarianAutosave.cancelPendingTimer();
    };
  });

  onDestroy(() => {
    unregisterLibrarianAutosave();
    librarianAutosave.cancelPendingTimer();
  });

  function librarianSettingsMatch(left, right) {
    const normalizedLeft = getLibrarianSettings({ librarian: left });
    const normalizedRight = getLibrarianSettings({ librarian: right });

    return [...SWITCH_FIELDS, ...DAY_FIELDS].every(
      (field) => normalizedLeft[field] === normalizedRight[field],
    );
  }

  function setSwitch(field, next) {
    librarianSettings = { ...librarianSettings, [field]: next };
    onError('');
  }

  function handleDaysInput(field, next) {
    if (next === '') {
      librarianSettings = { ...librarianSettings, [field]: next };
      onError('');
      return;
    }
    const numberValue = Number(next);
    if (Number.isInteger(numberValue) && numberValue >= 1) {
      librarianSettings = { ...librarianSettings, [field]: numberValue };
      onError('');
    }
  }

  async function saveLibrarianSettings() {
    if (
      librarianSettingsMatch(librarianSettings, getLibrarianSettings(settings))
    ) {
      return true;
    }

    return librarianDraft.save({
      onCommit,
      onError,
      setSaving: (value) => (saving = value),
    });
  }
</script>

<div class="s-group">
  <div class="s-row s-row--compact">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.librarian.enabled')}
        <InfoHint text={t('settings.librarian.enabledHelp')} />
      </div>
      <div class="s-row-desc">
        {t('settings.librarian.enabledDescription')}
      </div>
    </div>
    <div class="s-row-control">
      <Toggle
        checked={librarianSettings.enabled === true}
        ariaLabel={t('settings.librarian.enabled')}
        onChange={(next) => setSwitch('enabled', next)}
      />
    </div>
  </div>

  <!-- The interval only matters while the schedule is on. The hidden row
       stays mounted so settings search still finds it. A pass started by
       hand uses the rows below, so they stay visible. -->
  <div class="s-row s-row--compact" hidden={librarianSettings.enabled !== true}>
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.librarian.interval')}
        <InfoHint text={t('settings.librarian.intervalHelp')} />
      </div>
      <div class="s-row-desc">
        {t('settings.librarian.intervalDescription')}
      </div>
    </div>
    <div class="s-row-control s-row-control--number">
      <TextField
        id="settings-librarian-interval"
        type="number"
        min="1"
        step="1"
        value={librarianSettings.interval_days}
        ariaLabel={t('settings.librarian.interval')}
        onInput={(next) => handleDaysInput('interval_days', next)}
      />
    </div>
  </div>

  <div class="s-row s-row--compact">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.librarian.archiveAfter')}
        <InfoHint text={t('settings.librarian.archiveAfterHelp')} />
      </div>
      <div class="s-row-desc">
        {t('settings.librarian.archiveAfterDescription')}
      </div>
    </div>
    <div class="s-row-control s-row-control--number">
      <TextField
        id="settings-librarian-archive-after"
        type="number"
        min="1"
        step="1"
        value={librarianSettings.archive_after_days}
        ariaLabel={t('settings.librarian.archiveAfter')}
        onInput={(next) => handleDaysInput('archive_after_days', next)}
      />
    </div>
  </div>

  <div class="s-row s-row--compact">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.librarian.consolidate')}
        <InfoHint text={t('settings.librarian.consolidateHelp')} />
      </div>
      <div class="s-row-desc">
        {t('settings.librarian.consolidateDescription')}
      </div>
    </div>
    <div class="s-row-control">
      <Toggle
        checked={librarianSettings.consolidate === true}
        ariaLabel={t('settings.librarian.consolidate')}
        onChange={(next) => setSwitch('consolidate', next)}
      />
    </div>
  </div>
</div>

<div class="s-footer">
  <SaveStatus
    {saving}
    pending={librarianAutosave.participant.hasChanges()}
    onClick={() => librarianAutosave.participant.runSave('manual')}
  />
</div>
