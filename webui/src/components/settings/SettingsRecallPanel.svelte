<script>
  // The "Conversation search" section, read top to bottom: the search
  // method; for a method that searches by meaning, the embedding Model
  // (chosen in the Model picker; a local Model that is not installed yet
  // opens its one-time installation) with its options behind a disclosure;
  // and the semantic index status with its rarely needed rebuild action.
  // Two drafts save here: the Recall backend (`settings.update`) and the
  // `text_embedding` binding (the shared Task Model editor); one save state
  // covers both.
  import { onDestroy, onMount, untrack } from 'svelte';

  import LocalModelInstallDialog from './LocalModelInstallDialog.svelte';
  import TaskModelOptions from './TaskModelOptions.svelte';
  import { createTaskModelEditor } from './taskModelEditor.svelte.js';
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import { getRecallIndexStatus, rebuildRecallIndex } from '$lib/api.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import { createSettingsDraft } from '$lib/settingsSave.js';
  import {
    buildEmbeddingModelOptions,
    buildRecallMethodOptions,
    buildRecallSettingsPayload,
    describeEmbeddingInstall,
    describeEmbeddingModel,
    describeRecallIndexStatus,
    describeRecallMethod,
    embeddingTargetNeedsInstall,
    getProviderItems,
    getRecallSettings,
    recallSearchesByMeaning,
    recommendedEmbeddingTarget,
  } from '$lib/settingsView.js';

  const TASK_TEXT_EMBEDDING = 'text_embedding';
  // How often a retry or next-attempt time is re-rendered relative to now.
  const STATUS_CLOCK_MS = 15_000;
  // The Model picker's list is at least this wide, so a Model's facts fit
  // beside its name.
  const MODEL_PANEL_MIN_WIDTH = 440;

  const noop = () => {};

  let {
    settings = null,
    onCommit = noop,
    onError = noop,
    modelsRefreshToken = 0,
    // The latest pushed `recall_index_status` payload.
    recallIndexStatus = null,
  } = $props();

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let recallSettings = $state(untrack(() => getRecallSettings(settings)));
  let saving = $state(false);
  const recallDraft = createSettingsDraft({
    settings: untrack(() => settings),
    fromSettings: getRecallSettings,
    read: () => recallSettings,
    write: (next) => (recallSettings = next),
    toPayload: buildRecallSettingsPayload,
  });

  const editor = createTaskModelEditor({
    taskTypes: [TASK_TEXT_EMBEDDING],
    getSettings: () => settings,
    getModelsRefreshToken: () => modelsRefreshToken,
    onCommit: (next) => {
      onCommit(next);
      void loadIndexStatus();
    },
    onError: (message) => onError(message),
  });
  const embeddingRow = editor.rows[0];

  let destroyed = false;
  let indexStatus = $state(null);
  let indexStatusError = $state('');
  // Bumps whenever a status is applied, so a slower read never replaces a
  // newer pushed or rebuilt status.
  let indexStatusVersion = 0;
  let lastPushedStatus = untrack(() => recallIndexStatus);
  let lastStatusToken = untrack(() => modelsRefreshToken);
  let nowMs = $state(Date.now());
  let rebuildConfirmOpen = $state(false);
  let rebuilding = $state(false);
  // The local Model whose installation dialog is open, or null.
  let installTarget = $state(null);
  // Options the user changed stay in view; untouched defaults stay folded.
  let optionsOpen = $state(untrack(() => editor.canReset(TASK_TEXT_EMBEDDING)));

  let methodOptions = $derived(buildRecallMethodOptions(recallSettings));
  let methodDescription = $derived(
    describeRecallMethod(recallSettings.backend),
  );
  let searchesByMeaning = $derived(
    recallSearchesByMeaning(recallSettings.backend),
  );
  let binding = $derived(editor.binding(TASK_TEXT_EMBEDDING));
  let targets = $derived(editor.targets(TASK_TEXT_EMBEDDING));
  let modelOptions = $derived(
    buildEmbeddingModelOptions(targets, binding.target),
  );
  let selectedTarget = $derived(
    targets.find((target) => target.id === binding.target) ?? null,
  );
  let modelLine = $derived(
    editor.loaded
      ? describeEmbeddingModel(
          selectedTarget,
          binding.target,
          providerName,
          recommendedEmbeddingTarget(targets),
        )
      : { text: t('settings.recall.model.loading'), attention: false },
  );
  let hasModelOptions = $derived(
    Boolean(binding.target) &&
      (editor.visibleFields(TASK_TEXT_EMBEDDING).length > 0 ||
        editor.canReset(TASK_TEXT_EMBEDDING)),
  );
  let statusLine = $derived(describeRecallIndexStatus(indexStatus, nowMs));
  // The index row shows once a Model is chosen; before the first status
  // arrives it says it is checking.
  let statusRowVisible = $derived(
    searchesByMeaning &&
      Boolean(binding.target) &&
      (statusLine !== null || indexStatus === null),
  );
  // Rebuilding only makes sense once something is indexed or skipped.
  let canRebuild = $derived(
    (indexStatus?.indexed ?? 0) > 0 || (indexStatus?.skipped ?? 0) > 0,
  );

  let saveDisabled = $derived(
    saving || recallSettingsMatch(recallSettings, getRecallSettings(settings)),
  );
  const autosaveContext = useAutosaveContext();
  const recallAutosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...recallSettings }),
    hasChanges: () =>
      !recallSettingsMatch(recallSettings, getRecallSettings(settings)),
    save: saveRecallSettings,
  });
  const unregisterRecallAutosave = autosaveContext.register(
    recallAutosave.participant,
  );

  onMount(() => {
    void loadIndexStatus();
  });

  onDestroy(() => {
    destroyed = true;
    unregisterRecallAutosave();
    recallAutosave.cancelPendingTimer();
  });

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    recallAutosave.scheduleRun();

    return () => {
      recallAutosave.cancelPendingTimer();
    };
  });

  // A pushed status replaces the shown one; the push already held at mount
  // is older than the read the mount starts.
  $effect(() => {
    const pushed = recallIndexStatus;
    if (pushed === lastPushedStatus) {
      return;
    }
    lastPushedStatus = pushed;
    if (pushed) {
      applyIndexStatus(pushed);
    }
  });

  // Models, Providers or a reconnect after missed events: read the status
  // again.
  $effect(() => {
    const token = modelsRefreshToken;
    if (token !== lastStatusToken) {
      lastStatusToken = token;
      void loadIndexStatus();
    }
  });

  // A retry or next attempt reads relative to now, so it is re-rendered
  // while one is shown.
  $effect(() => {
    const state = indexStatus?.state;
    if (state !== 'retrying' && state !== 'error') {
      return;
    }
    nowMs = Date.now();
    const timer = setInterval(() => (nowMs = Date.now()), STATUS_CLOCK_MS);
    return () => clearInterval(timer);
  });

  function providerName(providerId) {
    return (
      getProviderItems(settings).find((item) => item.id === providerId)?.name ??
      ''
    );
  }

  function recallSettingsMatch(left, right) {
    return (
      getRecallSettings({ recall: left }).backend ===
      getRecallSettings({ recall: right }).backend
    );
  }

  function setBackend(backend) {
    recallSettings = {
      ...recallSettings,
      backend,
    };
    onError('');
  }

  // A local Model that is not installed yet is chosen once its installation
  // finishes.
  function chooseModel(targetId) {
    const target = targets.find((item) => item.id === targetId);
    if (embeddingTargetNeedsInstall(target)) {
      installTarget = target;
      return;
    }
    void editor.setTarget(TASK_TEXT_EMBEDDING, targetId);
  }

  async function finishInstall() {
    const target = installTarget;
    if (!target) return;
    await editor.refreshTargets(TASK_TEXT_EMBEDDING);
    if (destroyed || installTarget?.id !== target.id) return;
    installTarget = null;
    void editor.setTarget(TASK_TEXT_EMBEDDING, target.id);
  }

  async function saveRecallSettings() {
    if (recallSettingsMatch(recallSettings, getRecallSettings(settings))) {
      return true;
    }

    const saved = await recallDraft.save({
      onCommit,
      onError,
      setSaving: (value) => (saving = value),
    });
    if (saved) {
      void loadIndexStatus();
    }
    return saved;
  }

  function applyIndexStatus(status) {
    indexStatusVersion += 1;
    indexStatus = status;
    indexStatusError = '';
  }

  async function loadIndexStatus() {
    const version = ++indexStatusVersion;
    try {
      const status = await getRecallIndexStatus();
      if (destroyed || version !== indexStatusVersion) return;
      indexStatus = status;
      indexStatusError = '';
    } catch {
      if (destroyed || version !== indexStatusVersion) return;
      indexStatusError = t('settings.recall.status.loadError');
    }
  }

  async function confirmRebuild() {
    rebuildConfirmOpen = false;
    rebuilding = true;
    onError('');
    try {
      const status = await rebuildRecallIndex();
      if (!destroyed) applyIndexStatus(status);
    } catch (error) {
      if (!destroyed)
        onError(`${t('settings.recall.rebuildError')} ${error.message}`);
    } finally {
      if (!destroyed) rebuilding = false;
    }
  }

  function saveNow() {
    void recallAutosave.participant.runSave('manual');
    void editor.participant.runSave('manual');
  }
</script>

<div class="s-group">
  <div class="s-row" data-recall-method>
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.recall.method')}
        <InfoHint text={t('settings.recall.methodHelp')} />
      </div>
      <div class="s-row-desc">{methodDescription}</div>
    </div>
    <div class="s-row-control">
      <Dropdown
        id="settings-recall-backend"
        value={recallSettings.backend}
        options={methodOptions}
        ariaLabel={t('settings.recall.method')}
        triggerClass="settings-view__dropdown"
        listClass="settings-view__thinking-list"
        onValueChange={setBackend}
      />
    </div>
  </div>

  <!-- The embedding Model only matters for a method that searches by
       meaning. Hidden rows stay mounted so settings search still finds
       them. -->
  <div class="s-row" hidden={!searchesByMeaning} data-recall-model>
    <div class="s-row-info">
      <div class="s-row-label">
        {embeddingRow.title()}
        <InfoHint
          text={embeddingRow.help()}
          ariaLabel={t('settings.specializedModels.aboutAria', {
            name: embeddingRow.title(),
          })}
        />
      </div>
      <div
        class="s-row-desc"
        class:recall-model__line--attention={modelLine.attention}
        data-recall-model-line={modelLine.attention ? 'attention' : 'chosen'}
      >
        {modelLine.text}
      </div>
    </div>
    <div class="s-row-control s-row-control--task-model">
      <SearchableDropdown
        id="settings-specialized-text_embedding"
        value={binding.target}
        options={modelOptions}
        placeholder={t('settings.recall.model.placeholder')}
        searchPlaceholder={t('settings.recall.model.search')}
        emptyLabel={t('settings.recall.model.none')}
        ariaLabel={embeddingRow.title()}
        disabled={!editor.loaded}
        panelMinWidth={MODEL_PANEL_MIN_WIDTH}
        triggerClass="settings-view__dropdown"
        onValueChange={chooseModel}
      />
    </div>
  </div>

  {#if hasModelOptions}
    <button
      type="button"
      class="s-row s-row--compact s-disclosure-row"
      id="settings-recall-model-options-toggle"
      aria-expanded={optionsOpen}
      aria-controls="settings-recall-model-options"
      hidden={!searchesByMeaning}
      onclick={() => (optionsOpen = !optionsOpen)}
    >
      <span class="s-row-label">
        <span
          class="disclosure-chevron"
          class:disclosure-chevron--open={optionsOpen}
          aria-hidden="true"
        ></span>
        {t('settings.recall.modelOptions')}
      </span>
    </button>
    {#if searchesByMeaning && optionsOpen}
      <div
        class="s-group__block s-group__block--attached s-task-model-details"
        id="settings-recall-model-options"
        data-recall-model-options
      >
        <TaskModelOptions
          {editor}
          taskType={TASK_TEXT_EMBEDDING}
          title={embeddingRow.title()}
        />
      </div>
    {/if}
  {/if}

  <div
    class="s-row s-row--compact"
    hidden={!statusRowVisible}
    data-recall-index={statusLine?.state ?? ''}
  >
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.recall.index')}
        <InfoHint text={t('settings.recall.indexHelp')} />
      </div>
      <div class="s-row-desc" role="status" aria-live="polite">
        {statusLine?.summary ||
          indexStatusError ||
          t('settings.recall.status.loading')}
      </div>
      {#if statusLine?.problem}
        <div
          class="s-row-desc recall-index__problem"
          class:recall-index__problem--error={statusLine.state === 'error'}
          data-recall-index-problem
        >
          {statusLine.problem}
        </div>
      {/if}
    </div>
    {#if canRebuild}
      <div class="s-row-control">
        <Button
          variant="tertiary"
          disabled={rebuilding}
          onClick={() => (rebuildConfirmOpen = true)}
          >{t('settings.recall.rebuild')}</Button
        >
      </div>
    {/if}
  </div>
</div>

<div class="s-footer">
  <SaveStatus
    saving={saving || editor.saving}
    pending={recallAutosave.participant.hasChanges() ||
      editor.participant.hasChanges()}
    onClick={saveNow}
  />
</div>

{#if installTarget}
  {@const install = describeEmbeddingInstall(installTarget)}
  <LocalModelInstallDialog
    target={installTarget.id}
    label={installTarget.label}
    note={install.note}
    download={install.download}
    onInstalled={finishInstall}
    onClose={() => (installTarget = null)}
  />
{/if}

{#if rebuildConfirmOpen}
  <ConfirmDialog
    title={t('settings.recall.rebuildTitle')}
    body={t('settings.recall.rebuildBody')}
    confirmLabel={t('settings.recall.rebuildConfirm')}
    onConfirm={confirmRebuild}
    onCancel={() => (rebuildConfirmOpen = false)}
  />
{/if}

<style>
  /* Search by meaning cannot work until an embedding Model is chosen and
     installed. Qualified with .s-row-desc to outrank the Settings
     description color. */
  .s-row-desc.recall-model__line--attention,
  .s-row-desc.recall-index__problem {
    color: var(--amber);
  }

  .s-row-desc.recall-index__problem--error {
    color: var(--red);
  }
</style>
