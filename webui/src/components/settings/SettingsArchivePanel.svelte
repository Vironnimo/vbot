<script>
  // Settings -> Archive, above the archived items: whether and after how
  // many days vBot deletes archived Agents, Projects and Sessions
  // automatically (`archive.retention_days`, 1-3650 or null for never). The
  // Archive keeps files from older vBot versions and items that may hold the
  // user's own folders regardless; the panel says so. While vBot cannot read
  // the period from its settings file, automatic deletion is paused and the
  // panel says that too.
  import { onDestroy, untrack } from 'svelte';

  import Banner from '../ui/Banner.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { archiveRetention } from '$lib/archiveRetention.svelte.js';
  import { t } from '$lib/i18n.js';
  import { createSettingsDraft } from '$lib/settingsSave.js';

  const noop = () => {};

  const RETENTION_DEFAULT_DAYS = 30;
  const RETENTION_MIN_DAYS = 1;
  const RETENTION_MAX_DAYS = 3650;

  function validDays(value) {
    return (
      Number.isInteger(value) &&
      value >= RETENTION_MIN_DAYS &&
      value <= RETENTION_MAX_DAYS
    );
  }

  // The saved period: whole days, or null when nothing is deleted
  // automatically.
  function getRetentionDays(rawSettings) {
    const archive = rawSettings?.archive ?? {};
    if (archive.retention_days === null) return null;
    return validDays(archive.retention_days)
      ? archive.retention_days
      : RETENTION_DEFAULT_DAYS;
  }

  function fromSettings(rawSettings) {
    return { retention_days: getRetentionDays(rawSettings) };
  }

  let { settings = null, onCommit = noop, onError = noop } = $props();

  // Seeded once at mount; saves rebase it onto newer Settings.
  let draft = $state(untrack(() => fromSettings(settings)));
  // The days field's text, kept while it holds no valid period.
  let daysText = $state(
    untrack(() => String(draft.retention_days ?? RETENTION_DEFAULT_DAYS)),
  );
  // The period automatic deletion returns to when it is turned back on.
  let lastDays = $state(
    untrack(() => draft.retention_days ?? RETENTION_DEFAULT_DAYS),
  );
  let saving = $state(false);

  let automatic = $derived(draft.retention_days !== null);
  let daysInvalid = $derived(automatic && !validDays(Number(daysText)));

  const retentionDraft = createSettingsDraft({
    settings: untrack(() => settings),
    fromSettings,
    read: () => draft,
    write: (next) => {
      draft = next;
      if (next.retention_days !== null) {
        lastDays = next.retention_days;
        daysText = String(next.retention_days);
      }
    },
    toPayload: (values) => ({
      archive: { retention_days: values.retention_days },
    }),
  });

  function hasChanges() {
    return draft.retention_days !== getRetentionDays(settings);
  }

  let saveDisabled = $derived(saving || !hasChanges());
  const autosaveContext = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...draft }),
    hasChanges,
    save: saveRetention,
  });
  const unregisterAutosave = autosaveContext.register(autosave.participant);

  $effect(() => {
    if (saveDisabled) return;
    autosave.scheduleRun();
    return () => autosave.cancelPendingTimer();
  });

  onDestroy(() => {
    unregisterAutosave();
    autosave.cancelPendingTimer();
  });

  async function saveRetention() {
    if (!hasChanges()) return true;
    return retentionDraft.save({
      onCommit,
      onError,
      setSaving: (value) => (saving = value),
    });
  }

  function setAutomatic(next) {
    draft = { retention_days: next ? lastDays : null };
    if (next) daysText = String(lastDays);
    onError('');
  }

  function setDaysText(next) {
    daysText = next;
    const days = Number(next);
    if (next.trim() !== '' && validDays(days)) {
      lastDays = days;
      draft = { retention_days: days };
      onError('');
    }
  }
</script>

{#if archiveRetention.unknown}
  <Banner variant="warn">{t('archive.retention.unknown')}</Banner>
{/if}

<div class="s-group">
  <div class="s-row s-row--compact">
    <div class="s-row-info">
      <div class="s-row-label">{t('settings.archive.automatic')}</div>
      <div class="s-row-desc">{t('settings.archive.automaticDescription')}</div>
    </div>
    <div class="s-row-control">
      <Toggle
        checked={automatic}
        ariaLabel={t('settings.archive.automatic')}
        onChange={setAutomatic}
      />
    </div>
  </div>

  <div class="s-row s-row--compact" hidden={!automatic}>
    <div class="s-row-info">
      <div class="s-row-label">{t('settings.archive.days')}</div>
      <div class="s-row-desc">{t('settings.archive.daysDescription')}</div>
    </div>
    <div class="s-row-control s-row-control--number">
      <TextField
        id="settings-archive-retention-days"
        type="number"
        min={RETENTION_MIN_DAYS}
        max={RETENTION_MAX_DAYS}
        step="1"
        value={daysText}
        invalid={daysInvalid}
        ariaLabel={t('settings.archive.days')}
        onInput={setDaysText}
      />
    </div>
  </div>
</div>

<div class="s-footer">
  <SaveStatus
    {saving}
    pending={autosave.participant.hasChanges()}
    onClick={() => autosave.participant.runSave('manual')}
  />
</div>
