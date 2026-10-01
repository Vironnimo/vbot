<script>
  // The "Conversation search" section: the switch that adds search by
  // meaning, the embedding Model it needs (a local one not set up yet offers
  // its installation in place), the semantic index status with
  // its rebuild action, and an Advanced part with the full backend list and
  // the embedding Model's options. Two drafts save here: the Recall backend
  // (`settings.update`) and the `text_embedding` binding (the shared Task
  // Model editor); one save state covers both.
  import { onDestroy, onMount, untrack } from 'svelte';

  import LocalModelInstall from './LocalModelInstall.svelte';
  import TaskModelOptions from './TaskModelOptions.svelte';
  import { createTaskModelEditor } from './taskModelEditor.svelte.js';
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { getRecallIndexStatus, rebuildRecallIndex } from '$lib/api.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import { createSettingsDraft } from '$lib/settingsSave.js';
  import {
    buildEmbeddingModelChoices,
    buildRecallBackendOptions,
    buildRecallSettingsPayload,
    describeEmbeddingPrivacy,
    describeRecallIndexStatus,
    getProviderItems,
    getRecallSettings,
    recallBackendForMeaning,
    recallBackendNeedsAdvanced,
    recallMeaningAvailable,
    recallSearchesByMeaning,
  } from '$lib/settingsView.js';

  const TASK_TEXT_EMBEDDING = 'text_embedding';
  // How often a retry or next-attempt time is re-rendered relative to now.
  const STATUS_CLOCK_MS = 15_000;

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
  // A stored backend the switch does not select opens Advanced.
  let advancedOpen = $state(
    untrack(() => recallBackendNeedsAdvanced(recallSettings.backend)),
  );
  let allModelsOpen = $state(false);

  let searchesByMeaning = $derived(
    recallSearchesByMeaning(recallSettings.backend),
  );
  let meaningAvailable = $derived(recallMeaningAvailable(recallSettings));
  let recallBackendOptions = $derived(
    buildRecallBackendOptions(recallSettings),
  );
  let binding = $derived(editor.binding(TASK_TEXT_EMBEDDING));
  let targets = $derived(editor.targets(TASK_TEXT_EMBEDDING));
  let choices = $derived(buildEmbeddingModelChoices(targets, binding.target));
  let choiceGroups = $derived(
    [
      {
        id: 'local',
        title: t('settings.recall.model.groupLocal'),
        choices: choices.local,
      },
      {
        id: 'cloud',
        title: t('settings.recall.model.groupCloud'),
        choices: choices.cloud,
      },
    ].filter((group) => group.choices.length > 0),
  );
  let selectedTarget = $derived(
    targets.find((target) => target.id === binding.target) ?? null,
  );
  let selectedListed = $derived(
    choiceGroups.some((group) =>
      group.choices.some((choice) => choice.id === binding.target),
    ),
  );
  let privacyText = $derived(
    describeEmbeddingPrivacy(selectedTarget, providerName),
  );
  let hasModelOptions = $derived(
    editor.visibleFields(TASK_TEXT_EMBEDDING).length > 0 ||
      editor.canReset(TASK_TEXT_EMBEDDING),
  );
  let statusLine = $derived(describeRecallIndexStatus(indexStatus, nowMs));
  // Before the first status arrives the row says it is checking; a state
  // without a status line (off, or no Model yet) hides it.
  let statusRowVisible = $derived(
    searchesByMeaning && (statusLine !== null || indexStatus === null),
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

  // A selected Model outside the recommended list is shown in All models.
  $effect(() => {
    if (editor.loaded && binding.target && !selectedListed) {
      untrack(() => (allModelsOpen = true));
    }
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

  function setMeaning(on) {
    setBackend(recallBackendForMeaning(recallSettings.backend, on));
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
  <div class="s-row s-row--compact">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.recall.meaning')}
        <InfoHint text={t('settings.recall.meaningHelp')} />
      </div>
      <div class="s-row-desc">{t('settings.recall.meaningDescription')}</div>
    </div>
    <div class="s-row-control">
      <Toggle
        id="settings-recall-meaning"
        checked={searchesByMeaning}
        disabled={!searchesByMeaning && !meaningAvailable}
        ariaLabel={t('settings.recall.meaning')}
        onChange={setMeaning}
      />
    </div>
  </div>

  <!-- The embedding Model only matters while search by meaning is on. Hidden
       rows stay mounted so settings search still finds them. -->
  <div
    class="s-row s-row--stacked"
    hidden={!searchesByMeaning}
    data-recall-model
  >
    <fieldset class="recall-model">
      <legend class="s-row-label">
        {embeddingRow.title()}
        <InfoHint
          text={embeddingRow.help()}
          ariaLabel={t('settings.specializedModels.aboutAria', {
            name: embeddingRow.title(),
          })}
        />
      </legend>
      <div class="recall-model__groups">
        {#if !editor.loaded}
          {#if editor.loading}
            <p class="s-row-desc">{t('settings.recall.model.loading')}</p>
          {/if}
        {:else if choiceGroups.length === 0}
          <p class="s-row-desc">{t('settings.recall.model.noRecommended')}</p>
        {/if}
        {#each choiceGroups as group (group.id)}
          <div
            class="recall-model__group"
            role="group"
            aria-labelledby={`settings-recall-model-${group.id}`}
            data-recall-model-group={group.id}
          >
            <div
              class="recall-model__group-title"
              id={`settings-recall-model-${group.id}`}
            >
              {group.title}
            </div>
            {#each group.choices as choice (choice.id)}
              <div
                class="recall-model__choice"
                class:recall-model__choice--unavailable={!choice.usable}
                data-embedding-choice={choice.id}
              >
                <!-- The label is a layout-free wrapper, so the radio and its
                     text share the grid with the install row below. -->
                <label class="recall-model__label">
                  <input
                    type="radio"
                    name="settings-recall-embedding-model"
                    value={choice.id}
                    checked={binding.target === choice.id}
                    disabled={!choice.usable || editor.loading}
                    onchange={() =>
                      editor.setTarget(TASK_TEXT_EMBEDDING, choice.id)}
                  />
                  <span class="recall-model__text">
                    <span class="recall-model__head">
                      <span class="recall-model__name">{choice.label}</span>
                      <span class="recall-model__facts">
                        {choice.facts.join(' · ')}
                      </span>
                    </span>
                    <!-- An installable Model's install row says it is not set
                         up yet. -->
                    {#if !choice.usable && !choice.installable}
                      <span class="s-row-desc">
                        {t('settings.recall.model.notInstalled')}
                      </span>
                    {:else if choice.usable && choice.note}
                      <span class="s-row-desc">{choice.note}</span>
                    {/if}
                  </span>
                </label>
                {#if choice.installable}
                  <div class="recall-model__install">
                    <LocalModelInstall
                      target={choice.id}
                      download={choice.download}
                      onReady={() => editor.refreshTargets(TASK_TEXT_EMBEDDING)}
                    />
                  </div>
                {/if}
              </div>
            {/each}
          </div>
        {/each}
      </div>
    </fieldset>
  </div>

  <div
    class="s-group__block s-group__block--attached s-group__note"
    class:recall-note--attention={!binding.target}
    hidden={!searchesByMeaning}
    data-recall-embedding-note={binding.target ? 'in-use' : 'missing'}
  >
    <p>
      {binding.target
        ? `${privacyText} ${t('settings.recall.model.rebuildNote')}`
        : t('settings.recall.model.missing')}
    </p>
  </div>

  <button
    type="button"
    class="s-row s-row--compact s-disclosure-row"
    id="settings-recall-all-models-toggle"
    aria-expanded={allModelsOpen}
    aria-controls="settings-recall-all-models"
    hidden={!searchesByMeaning}
    onclick={() => (allModelsOpen = !allModelsOpen)}
  >
    <span class="s-row-label">
      <span
        class="disclosure-chevron"
        class:disclosure-chevron--open={allModelsOpen}
        aria-hidden="true"
      ></span>
      {t('settings.recall.model.allModels')}
    </span>
  </button>
  <div
    class="s-group__rows"
    id="settings-recall-all-models"
    hidden={!searchesByMeaning || !allModelsOpen}
  >
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-desc">
          {t('settings.recall.model.allModelsDescription')}
        </div>
      </div>
      <div class="s-row-control s-row-control--task-model">
        <SearchableDropdown
          id="settings-specialized-text_embedding"
          value={binding.target}
          options={editor.targetOptions(TASK_TEXT_EMBEDDING)}
          placeholder={t('settings.specializedModels.noTarget')}
          ariaLabel={embeddingRow.title()}
          disabled={editor.loading}
          triggerClass="settings-view__dropdown"
          onValueChange={(value) =>
            editor.setTarget(TASK_TEXT_EMBEDDING, value)}
        />
      </div>
    </div>
  </div>

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
    <div class="s-row-control">
      <Button
        variant="secondary"
        disabled={rebuilding || statusLine === null}
        onClick={() => (rebuildConfirmOpen = true)}
        >{t('settings.recall.rebuild')}</Button
      >
    </div>
  </div>

  <button
    type="button"
    class="s-row s-row--compact s-disclosure-row"
    id="settings-recall-advanced-toggle"
    aria-expanded={advancedOpen}
    aria-controls="settings-recall-advanced"
    onclick={() => (advancedOpen = !advancedOpen)}
  >
    <span class="s-row-label">
      <span
        class="disclosure-chevron"
        class:disclosure-chevron--open={advancedOpen}
        aria-hidden="true"
      ></span>
      {t('settings.recall.advanced')}
    </span>
  </button>
  <div
    class="s-group__rows"
    id="settings-recall-advanced"
    hidden={!advancedOpen}
  >
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.recall.backend')}
          <InfoHint text={t('settings.recall.backendHelp')} />
        </div>
      </div>
      <div class="s-row-control">
        <Dropdown
          id="settings-recall-backend"
          value={recallSettings.backend}
          options={recallBackendOptions}
          ariaLabel={t('settings.recall.backend')}
          triggerClass="settings-view__dropdown"
          listClass="settings-view__thinking-list"
          onValueChange={setBackend}
        />
      </div>
    </div>
    {#if binding.target && hasModelOptions}
      <div
        class="s-row s-row--stacked"
        hidden={!searchesByMeaning}
        data-recall-model-options
      >
        <div class="s-row-label">{t('settings.recall.modelOptions')}</div>
        <div class="s-task-model-details">
          <TaskModelOptions
            {editor}
            taskType={TASK_TEXT_EMBEDDING}
            title={embeddingRow.title()}
          />
        </div>
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
  /* The recommended embedding Models: a plain radio list under the label,
     grouped by where the Model runs. */
  .recall-model {
    min-width: 0;
    margin: 0;
    padding: 0;
    border: 0;
  }

  .recall-model legend {
    padding: 0;
  }

  .recall-model__groups {
    display: grid;
    gap: 14px;
    margin-top: 10px;
  }

  .recall-model__groups > p {
    margin: 0;
  }

  .recall-model__group {
    display: grid;
    gap: 10px;
  }

  .recall-model__group-title {
    color: var(--text-lo);
    font: 600 var(--fs-label-sm) var(--font-ui);
    letter-spacing: 0.02em;
    text-transform: uppercase;
  }

  /* Radio | text, with a local Model's install row under the text. */
  .recall-model__choice {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: start;
    column-gap: 10px;
    row-gap: 6px;
    max-width: 72ch;
  }

  .recall-model__label {
    display: contents;
    cursor: pointer;
  }

  .recall-model__choice input {
    margin: 3px 0 0;
    accent-color: var(--accent);
  }

  .recall-model__choice--unavailable .recall-model__label {
    cursor: default;
  }

  .recall-model__install {
    grid-column: 2;
  }

  .recall-model__choice--unavailable .recall-model__name {
    color: var(--text-med);
  }

  .recall-model__text {
    display: flex;
    min-width: 0;
    flex-direction: column;
  }

  .recall-model__head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    column-gap: 10px;
  }

  .recall-model__name {
    color: var(--text-hi);
    font: 500 var(--fs-body-md) / 1.4 var(--font-ui);
    overflow-wrap: anywhere;
  }

  .recall-model__facts {
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
  }

  /* Search by meaning cannot work until an embedding Model is chosen. */
  .recall-note--attention {
    color: var(--amber);
  }

  .recall-index__problem {
    color: var(--amber);
  }

  .recall-index__problem--error {
    color: var(--red);
  }
</style>
