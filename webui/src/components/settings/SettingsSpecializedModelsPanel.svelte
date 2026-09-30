<script>
  import { onDestroy, onMount, untrack } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  import LocalSpeechSupport from './LocalSpeechSupport.svelte';
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import FormField from '../ui/FormField.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import {
    getLocalSpeechMemory,
    unloadLocalSpeech,
    getTaskModelOptions,
    listTaskModelTargets,
    updateTaskModelSettings,
  } from '$lib/api.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t, tOr } from '$lib/i18n.js';
  import { createSettingsDraft } from '$lib/settingsSave.js';
  import {
    JSON_OPTION_TYPE,
    TASK_MODEL_ROWS,
    createTaskModelUpdatePayload,
    isOptionFieldHidden,
    normalizeOptionSchema,
    normalizeTargets,
    normalizeTaskModelSettings,
    parseJsonFieldValue,
    reconcileDependentOptions,
    stringifyJsonFieldValue,
    taskModelBindingsMatch,
    visibleFieldOptions,
  } from '$lib/taskModelSettings.js';
  import { shouldApplyReloadNow } from '$lib/resourceInvalidation.js';

  const noop = () => {};

  let {
    settings = null,
    onCommit = noop,
    onError = noop,
    modelsRefreshToken = 0,
    taskTypes = null,
    showTaskLabels = true,
  } = $props();

  // Each placement owns only its task types; another page's commit must
  // never make an untouched binding here look like a draft to be saved.
  const taskRows = untrack(() =>
    TASK_MODEL_ROWS.filter(
      (row) => taskTypes === null || taskTypes.includes(row.taskType),
    ),
  );
  const ownsSpeech = taskRows.some((row) =>
    ['speech_to_text', 'text_to_speech'].includes(row.taskType),
  );

  function scopedBindings(value) {
    const bindings = normalizeTaskModelSettings(value);
    return Object.fromEntries(
      taskRows.map((row) => [row.taskType, bindings[row.taskType]]),
    );
  }

  // Only this placement's bindings; with `origin`, only those the draft
  // changed, so an untouched binding is never written back.
  function taskModelPayload(bindings, origin) {
    const payload = createTaskModelUpdatePayload(bindings, origin);
    return {
      model_tasks: Object.fromEntries(
        taskRows
          .filter((row) => Object.hasOwn(payload, row.taskType))
          .map((row) => [row.taskType, payload[row.taskType]]),
      ),
    };
  }

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let taskModelBindings = $state(untrack(() => scopedBindings(settings)));
  // Counts user edits, so a save that finishes after another edit stays armed.
  let taskModelEdits = 0;
  const taskModelDraft = createSettingsDraft({
    settings: untrack(() => settings),
    fromSettings: scopedBindings,
    read: () => taskModelBindings,
    write: replaceTaskModelBindings,
    toPayload: taskModelPayload,
    submit: async ({ model_tasks: modelTasks, base }) => {
      const result = await updateTaskModelSettings(modelTasks, {
        base: base.model_tasks,
      });
      return { ...settings, model_tasks: result.model_tasks ?? {} };
    },
    // A binding is one choice: its options belong to its target, so a
    // binding saved elsewhere replaces the draft's binding as a whole.
    leafDepth: 1,
  });
  let taskModelTargetsByType = $state({});
  let taskModelSchemasByType = $state({});
  let taskModelLoading = $state(false);
  let taskModelSaving = $state(false);
  // Per-field JSON parse errors keyed by `${taskType}::${field.name}`.
  // Empty string means "no error / not yet typed". The binding is never
  // updated with an invalid value; this map only drives the inline error
  // message under the textarea.
  let taskModelJsonErrors = $state({});
  let autoSaveArmed = $state(false);
  // A queued model reload waits here while the user is actively editing, since
  // a target reload replaces the available option controls.
  let pendingTaskModelReload = $state(false);
  let lastModelsRefreshToken = null;
  let taskModelSchemaRequestIds = {};
  let destroyed = false;
  let speechMemory = $state(null);
  let speechMemoryError = $state('');
  const speechUnloading = new SvelteSet();
  let speechUnloadErrors = $state({});
  let speechMemoryTimer;
  let speechMemoryRequest = 0;
  let selectedSpeechTargets = $derived(
    ['speech_to_text', 'text_to_speech']
      .map((task) => taskModelBindings[task]?.target)
      .filter((target) => target?.startsWith('local/')),
  );
  let speechMemoryModels = $derived(
    (speechMemory?.models ?? []).filter(
      (model) =>
        model.loaded ||
        model.busy ||
        selectedSpeechTargets.includes(model.target),
    ),
  );
  let showSpeechMemory = $derived(
    speechMemoryModels.length > 0 || selectedSpeechTargets.length > 0,
  );

  async function refreshSpeechMemory() {
    const request = ++speechMemoryRequest;
    try {
      const result = await getLocalSpeechMemory();
      if (destroyed || request !== speechMemoryRequest) return;
      speechMemory = result;
      speechMemoryError = '';
    } catch {
      if (destroyed || request !== speechMemoryRequest) return;
      speechMemory = null;
      speechMemoryError = t('settings.localSpeech.memoryError');
    } finally {
      if (!destroyed && request === speechMemoryRequest)
        speechMemoryTimer = setTimeout(refreshSpeechMemory, 2000);
    }
  }

  async function unloadSpeechMemory(model) {
    const target = model.target;
    if (speechUnloading.has(target) || !model.loaded || model.busy) return;
    speechUnloading.add(target);
    speechMemoryRequest += 1;
    clearTimeout(speechMemoryTimer);
    speechUnloadErrors = { ...speechUnloadErrors, [target]: '' };
    try {
      const result = await unloadLocalSpeech(target);
      if (!destroyed) {
        const updated = result.models.find((entry) => entry.target === target);
        if (updated)
          speechMemory = {
            models: speechMemory.models.map((entry) =>
              entry.target === target ? updated : entry,
            ),
          };
      }
    } catch {
      if (!destroyed)
        speechUnloadErrors = {
          ...speechUnloadErrors,
          [target]: t('settings.localSpeech.unloadError'),
        };
    } finally {
      speechUnloading.delete(target);
      if (!destroyed && speechUnloading.size === 0)
        speechMemoryTimer = setTimeout(refreshSpeechMemory, 2000);
    }
  }
  async function refreshLocalTargets(taskType) {
    const targets = await listTaskModelTargets(taskType);
    if (!destroyed)
      taskModelTargetsByType = {
        ...taskModelTargetsByType,
        [taskType]: normalizeTargets(targets),
      };
  }

  let saveDisabled = $derived(
    taskModelSaving ||
      taskModelLoading ||
      taskModelBindingsMatch(taskModelBindings, scopedBindings(settings)),
  );
  // "Busy" while loading, saving, or holding unsaved edits — a reload during any
  // of those would disturb in-progress work, so it is deferred until idle.
  let taskSurfaceBusy = $derived(
    taskModelLoading || taskModelSaving || (autoSaveArmed && !saveDisabled),
  );
  const autosaveContext = useAutosaveContext();
  const taskModelsAutosave = createDebouncedAutosave({
    getSnapshot: () => taskModelBindings,
    hasChanges: () =>
      autoSaveArmed &&
      !taskModelBindingsMatch(taskModelBindings, scopedBindings(settings)),
    save: saveTaskModelBindings,
  });
  const unregisterTaskModelsAutosave = autosaveContext.register(
    taskModelsAutosave.participant,
  );

  onMount(() => {
    void loadTaskModelPanel();
    if (ownsSpeech) void refreshSpeechMemory();
  });

  onDestroy(() => {
    destroyed = true;
    speechMemoryRequest += 1;
    clearTimeout(speechMemoryTimer);
    taskModelSchemaRequestIds = {};
    unregisterTaskModelsAutosave();
    taskModelsAutosave.cancelPendingTimer();
  });

  // Auto-save is armed only after a real user edit. Schema defaults are
  // display fallbacks and remain owned by the backend.
  $effect(() => {
    if (!autoSaveArmed || saveDisabled) {
      return;
    }

    taskModelsAutosave.scheduleRun();

    return () => {
      taskModelsAutosave.cancelPendingTimer();
    };
  });

  // A `resource_changed(models|providers)` signal queues a target reload (first
  // run is a no-op: mount already loaded).
  $effect(() => {
    if (lastModelsRefreshToken === null) {
      lastModelsRefreshToken = modelsRefreshToken;
      return;
    }
    if (modelsRefreshToken !== lastModelsRefreshToken) {
      lastModelsRefreshToken = modelsRefreshToken;
      pendingTaskModelReload = true;
    }
  });

  // Run the queued reload once the surface is idle, so it never re-applies
  // option controls during an edit the user is mid-way through.
  $effect(() => {
    if (
      pendingTaskModelReload &&
      shouldApplyReloadNow({ savePending: taskSurfaceBusy })
    ) {
      pendingTaskModelReload = false;
      void loadTaskModelPanel();
    }
  });

  async function loadTaskModelPanel() {
    if (taskModelLoading) {
      return;
    }

    taskModelLoading = true;
    onError('');

    try {
      const targetEntries = await Promise.all(
        taskRows.map(async (row) => {
          const result = await listTaskModelTargets(row.taskType);
          return [row.taskType, normalizeTargets(result)];
        }),
      );
      taskModelTargetsByType = Object.fromEntries(targetEntries);

      for (const row of taskRows) {
        const target = taskModelBindings[row.taskType]?.target ?? '';
        if (target) {
          await loadTaskModelSchema(row.taskType, target);
        }
      }
    } catch (error) {
      onError(`${t('settings.specializedModels.loadError')} ${error.message}`);
    } finally {
      taskModelLoading = false;
    }
  }

  async function loadTaskModelSchema(taskType, target) {
    const requestId = (taskModelSchemaRequestIds[taskType] ?? 0) + 1;
    taskModelSchemaRequestIds = {
      ...taskModelSchemaRequestIds,
      [taskType]: requestId,
    };
    const isCurrentRequest = () =>
      !destroyed &&
      taskModelSchemaRequestIds[taskType] === requestId &&
      (taskModelBindings[taskType]?.target ?? '') === target;

    if (!target) {
      if (!isCurrentRequest()) {
        return false;
      }
      taskModelSchemasByType = {
        ...taskModelSchemasByType,
        [taskType]: [],
      };
      clearTaskModelJsonErrors(taskType);
      return true;
    }

    let result;
    try {
      result = await getTaskModelOptions(taskType, target);
    } catch (error) {
      if (!isCurrentRequest()) {
        return false;
      }
      throw error;
    }
    if (!isCurrentRequest()) {
      return false;
    }
    const fields = normalizeOptionSchema(result);
    taskModelSchemasByType = {
      ...taskModelSchemasByType,
      [taskType]: fields,
    };
    clearTaskModelJsonErrors(taskType);
    return true;
  }

  async function saveTaskModelBindings() {
    if (
      !autoSaveArmed ||
      taskModelBindingsMatch(taskModelBindings, scopedBindings(settings))
    ) {
      return true;
    }

    const edits = taskModelEdits;
    const saved = await taskModelDraft.save({
      onCommit,
      onError,
      setSaving: (value) => (taskModelSaving = value),
    });
    if (saved && taskModelEdits === edits) {
      autoSaveArmed = false;
    }
    return saved;
  }

  // A rebase can replace a binding with one saved elsewhere; a changed target
  // needs its own option controls.
  function replaceTaskModelBindings(next) {
    const previous = taskModelBindings;
    taskModelBindings = next;
    for (const row of taskRows) {
      const target = next[row.taskType]?.target ?? '';
      if (target !== (previous[row.taskType]?.target ?? '')) {
        loadTaskModelSchema(row.taskType, target).catch((error) => {
          onError(
            `${t('settings.specializedModels.optionsLoadError')} ${error.message}`,
          );
        });
      }
    }
  }

  function markTaskModelEdit() {
    taskModelEdits += 1;
    autoSaveArmed = true;
  }

  async function handleTaskModelTargetChange(taskType, target) {
    onError('');
    markTaskModelEdit();
    taskModelBindings = {
      ...taskModelBindings,
      [taskType]: {
        target,
        options: {},
      },
    };

    try {
      await loadTaskModelSchema(taskType, target);
    } catch (error) {
      onError(
        `${t('settings.specializedModels.optionsLoadError')} ${error.message}`,
      );
    }
  }

  function handleTaskModelOptionChange(taskType, field, event) {
    if (field.type === JSON_OPTION_TYPE) {
      const text = event.currentTarget.value;
      const { value, error } = parseJsonFieldValue(text);
      setTaskModelJsonError(taskType, field, error);
      if (error === '' && value !== undefined) {
        setTaskModelOption(taskType, field, value);
      }
      return;
    }
    setTaskModelOption(
      taskType,
      field,
      valueFromTaskModelOptionField(field, event),
    );
  }

  function setTaskModelOption(taskType, field, value) {
    const currentBinding = taskModelBindings[taskType] ?? {
      target: '',
      options: {},
    };
    // A select whose choices depend on this field never keeps a hidden value.
    const options = reconcileDependentOptions(
      taskModelSchemasByType[taskType] ?? [],
      { ...(currentBinding.options ?? {}), [field.name]: value },
      field.name,
    );
    taskModelBindings = {
      ...taskModelBindings,
      [taskType]: { ...currentBinding, options },
    };
    onError('');
    markTaskModelEdit();
  }

  function resetTaskModelOptions(taskType) {
    taskModelBindings = {
      ...taskModelBindings,
      [taskType]: { ...taskModelBindings[taskType], options: {} },
    };
    clearTaskModelJsonErrors(taskType);
    onError('');
    markTaskModelEdit();
  }

  function valueFromTaskModelOptionField(field, event) {
    if (field.type === 'boolean') {
      return event.currentTarget.checked === true;
    }
    if (field.type === 'number') {
      const value = event.currentTarget.value;
      if (value === '') {
        return '';
      }
      const numberValue = Number(value);
      return Number.isFinite(numberValue) ? numberValue : value;
    }
    return event.currentTarget.value;
  }

  function taskModelTargets(taskType) {
    return taskModelTargetsByType[taskType] ?? [];
  }

  function taskModelTargetOptions(taskType, binding) {
    const targets = taskModelTargets(taskType);
    const options = [
      {
        value: '',
        label: t('settings.specializedModels.noTarget'),
      },
      ...targets.map((target) => ({
        value: target.id,
        label:
          target.kind === 'local' ? `${target.label} (local)` : target.label,
        searchText: `${target.label} ${target.id} ${target.kind === 'local' ? 'local' : ''}`,
      })),
    ];

    if (
      binding.target &&
      !targets.some((target) => target.id === binding.target)
    ) {
      options.push({
        value: binding.target,
        label: t('settings.specializedModels.customTarget', {
          target: binding.target,
        }),
      });
    }

    return options;
  }

  function taskModelFields(taskType) {
    const fields = taskModelSchemasByType[taskType] ?? [];
    if (!taskModelBindings[taskType]?.target?.startsWith('local/')) {
      return fields;
    }
    return fields.map((field) => ({
      ...field,
      label: tOr(
        `settings.localSpeech.options.${field.name}.label`,
        field.label,
      ),
      description: field.description
        ? tOr(
            `settings.localSpeech.options.${field.name}.help`,
            field.description,
          )
        : '',
      options: field.options.map((option) => ({
        ...option,
        label: tOr(
          `settings.localSpeech.choices.${option.value}`,
          option.label,
        ),
      })),
    }));
  }

  function taskModelFieldHidden(taskType, field) {
    return isOptionFieldHidden(
      field,
      taskModelSchemasByType[taskType] ?? [],
      taskModelBindings[taskType]?.options ?? {},
    );
  }

  function taskModelFieldChoices(taskType, field) {
    return visibleFieldOptions(
      field,
      taskModelSchemasByType[taskType] ?? [],
      taskModelBindings[taskType]?.options ?? {},
    );
  }

  function taskModelOptionValue(taskType, field) {
    const options = taskModelBindings[taskType]?.options ?? {};
    const value = options[field.name];
    if (value === undefined || value === null) {
      if (field.type === JSON_OPTION_TYPE) {
        return stringifyJsonFieldValue(field.default);
      }
      return field.default ?? '';
    }
    if (field.type === JSON_OPTION_TYPE) {
      return stringifyJsonFieldValue(value);
    }
    return value;
  }

  function taskModelJsonError(taskType, field) {
    return taskModelJsonErrors[`${taskType}::${field.name}`] ?? '';
  }

  function setTaskModelJsonError(taskType, field, message) {
    const key = `${taskType}::${field.name}`;
    const nextErrors = { ...taskModelJsonErrors };
    if (message) {
      nextErrors[key] = message;
    } else {
      delete nextErrors[key];
    }
    taskModelJsonErrors = nextErrors;
  }

  function clearTaskModelJsonErrors(taskType) {
    const prefix = `${taskType}::`;
    const nextErrors = {};
    for (const [key, message] of Object.entries(taskModelJsonErrors)) {
      if (!key.startsWith(prefix)) {
        nextErrors[key] = message;
      }
    }
    taskModelJsonErrors = nextErrors;
  }
</script>

{#if taskModelLoading}
  <Banner variant="neutral">
    {t('settings.specializedModels.loading')}
  </Banner>
{/if}

{#snippet optionField(taskType, field)}
  {@const jsonError =
    field.type === JSON_OPTION_TYPE ? taskModelJsonError(taskType, field) : ''}
  {@const fieldControlId = `task-model-${taskType}-${field.name}`}
  <!-- The field's explanation from the backend schema sits behind the
       label's "?"; the controls name themselves, so the hint does not become
       part of their accessible name. -->
  <FormField
    controlId={fieldControlId}
    full={field.type === JSON_OPTION_TYPE}
    error={jsonError
      ? t('settings.specializedModels.jsonInvalid', {
          error: jsonError,
        })
      : ''}
  >
    {#snippet labelContent()}
      {field.label}
      {#if field.description}
        <InfoHint
          text={field.description}
          ariaLabel={t('settings.specializedModels.aboutAria', {
            name: field.label,
          })}
        />
      {/if}
    {/snippet}
    {#snippet children(formField)}
      {#if field.type === 'select'}
        <Dropdown
          id={formField.controlId}
          value={taskModelOptionValue(taskType, field)}
          options={taskModelFieldChoices(taskType, field)}
          ariaLabel={field.label}
          ariaDescribedby={formField.describedBy}
          triggerClass="settings-view__dropdown"
          listClass="settings-view__thinking-list"
          onValueChange={(value) => setTaskModelOption(taskType, field, value)}
        />
      {:else if field.type === 'textarea'}
        <TextArea
          id={formField.controlId}
          rows="3"
          ariaLabel={field.label}
          aria-describedby={formField.describedBy}
          value={taskModelOptionValue(taskType, field)}
          onInput={(_value, event) =>
            handleTaskModelOptionChange(taskType, field, event)}
        />
      {:else if field.type === JSON_OPTION_TYPE}
        <TextArea
          id={formField.controlId}
          code
          invalid={formField.invalid}
          rows="4"
          spellcheck="false"
          autocapitalize="off"
          autocorrect="off"
          ariaLabel={field.label}
          aria-describedby={formField.describedBy}
          placeholder={t('settings.specializedModels.jsonPlaceholder')}
          value={taskModelOptionValue(taskType, field)}
          onInput={(_value, event) =>
            handleTaskModelOptionChange(taskType, field, event)}
        />
      {:else if field.type === 'number'}
        <TextField
          id={formField.controlId}
          type="number"
          ariaLabel={field.label}
          aria-describedby={formField.describedBy}
          min={field.min ?? undefined}
          max={field.max ?? undefined}
          step={field.step ?? 'any'}
          value={taskModelOptionValue(taskType, field)}
          onInput={(_next, event) =>
            handleTaskModelOptionChange(taskType, field, event)}
        />
      {:else if field.type === 'boolean'}
        <Toggle
          id={formField.controlId}
          checked={taskModelOptionValue(taskType, field) === true}
          ariaLabel={field.label}
          aria-describedby={formField.describedBy}
          onChange={(next) => setTaskModelOption(taskType, field, next)}
        />
      {:else}
        <TextField
          id={formField.controlId}
          value={taskModelOptionValue(taskType, field)}
          ariaLabel={field.label}
          aria-describedby={formField.describedBy}
          onInput={(_next, event) =>
            handleTaskModelOptionChange(taskType, field, event)}
        />
      {/if}
    {/snippet}
  </FormField>
{/snippet}

<div class="s-group s-task-model-list">
  {#each taskRows as row (row.taskType)}
    {@const binding = taskModelBindings[row.taskType] ?? {
      target: '',
      options: {},
    }}
    {@const visibleFields = taskModelFields(row.taskType).filter(
      (field) => !taskModelFieldHidden(row.taskType, field),
    )}
    {@const plainFields = visibleFields.filter(
      (field) => field.type !== JSON_OPTION_TYPE,
    )}
    {@const jsonFields = visibleFields.filter(
      (field) => field.type === JSON_OPTION_TYPE,
    )}
    {@const canReset = Object.keys(binding.options).length > 0}
    {@const selectedTarget = taskModelTargets(row.taskType).find(
      (target) => target.id === binding.target,
    )}
    {@const localSpeech =
      ['speech_to_text', 'text_to_speech'].includes(row.taskType) &&
      selectedTarget?.kind === 'local'}
    <div class="s-row s-task-model-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {showTaskLabels || !row.label ? row.title() : row.label()}
          <InfoHint
            text={row.help()}
            ariaLabel={t('settings.specializedModels.aboutAria', {
              name: row.title(),
            })}
          />
        </div>
        {#if row.description}
          <div class="s-row-desc">{row.description()}</div>
        {/if}
      </div>
      <div class="s-row-control s-row-control--task-model">
        <SearchableDropdown
          id={`settings-specialized-${row.taskType}`}
          value={binding.target}
          options={taskModelTargetOptions(row.taskType, binding)}
          placeholder={t('settings.specializedModels.noTarget')}
          ariaLabel={row.title()}
          disabled={taskModelLoading}
          triggerClass="settings-view__dropdown"
          onValueChange={(value) =>
            handleTaskModelTargetChange(row.taskType, value)}
        />
      </div>
    </div>

    {#if binding.target && (visibleFields.length > 0 || canReset || localSpeech)}
      <!-- The chosen target's options continue its row: ordinary options
           (fields another option's value makes irrelevant stay hidden),
           then the closed JSON disclosures with the quiet reset action on
           their line, then local speech setup. -->
      <div class="s-group__block s-group__block--attached s-task-model-details">
        {#if plainFields.length > 0}
          <div class="s-task-model-options">
            {#each plainFields as field (field.name)}
              {@render optionField(row.taskType, field)}
            {/each}
          </div>
        {/if}

        {#if jsonFields.length > 0 || canReset}
          <div class="s-task-model-more">
            {#if jsonFields.length > 0}
              <div class="s-task-model-more__json">
                {#each jsonFields as field (field.name)}
                  <details class="s-task-model-advanced">
                    <summary
                      >{field.label}<span aria-hidden="true">JSON</span
                      ></summary
                    >
                    {@render optionField(row.taskType, field)}
                  </details>
                {/each}
              </div>
            {/if}
            {#if canReset}
              <Button
                variant="tertiary"
                class="s-task-model-reset"
                ariaLabel={t('settings.specializedModels.resetOptionsAria', {
                  task: row.title(),
                })}
                disabled={taskModelSaving || taskModelLoading}
                onClick={() => resetTaskModelOptions(row.taskType)}
                >{t('settings.specializedModels.resetOptions')}</Button
              >
            {/if}
          </div>
        {/if}

        {#if localSpeech}
          {#key binding.target}
            <LocalSpeechSupport
              target={binding.target}
              tts={row.taskType === 'text_to_speech'}
              {taskSurfaceBusy}
              onReady={() => refreshLocalTargets(row.taskType)}
            />
          {/key}
        {/if}
      </div>
    {/if}
  {/each}
</div>

{#if showSpeechMemory}
  <div class="s-task-memory" data-local-speech-memory>
    <div class="s-subhead s-task-memory__head">
      <h4 class="s-subhead__title">{t('settings.localSpeech.memoryTitle')}</h4>
      <InfoHint
        text={t('settings.localSpeech.memoryHelp')}
        ariaLabel={t('settings.specializedModels.aboutAria', {
          name: t('settings.localSpeech.memoryTitle'),
        })}
      />
    </div>
    {#if speechMemoryError}<div role="alert">{speechMemoryError}</div>{/if}
    {#if !speechMemory && !speechMemoryError}
      <div class="s-group__note" role="status">
        {t('settings.localSpeech.memoryChecking')}
      </div>
    {/if}
    {#if speechMemoryModels.length}
      <div class="s-group">
        {#each speechMemoryModels as model (model.target)}
          <div
            class="s-row s-row--compact"
            data-speech-memory-target={model.target}
          >
            <div class="s-row-info">
              <div class="s-row-label">{model.label}</div>
              <div class="s-row-desc" role="status" aria-live="polite">
                {#if speechUnloading.has(model.target)}
                  {t('settings.localSpeech.unloading')}
                {:else if model.busy}
                  {t('settings.localSpeech.memoryBusy')}
                {:else if model.loaded}
                  {t('settings.localSpeech.memoryLoaded')}
                {:else}
                  {t('settings.localSpeech.memoryEmpty')}
                {/if}
              </div>
              {#if speechUnloadErrors[model.target]}
                <div role="alert">{speechUnloadErrors[model.target]}</div>
              {/if}
            </div>
            <div class="s-row-control">
              <Button
                disabled={speechUnloading.has(model.target) ||
                  !model.loaded ||
                  model.busy}
                ariaLabel={t('settings.localSpeech.unloadAria', {
                  model: model.label,
                })}
                onClick={() => unloadSpeechMemory(model)}
                >{t('settings.localSpeech.unloadButton')}</Button
              >
            </div>
          </div>
        {/each}
      </div>
    {/if}
  </div>
{/if}

<div class="s-footer">
  <SaveStatus
    saving={taskModelSaving}
    pending={taskModelsAutosave.participant.hasChanges()}
    onClick={() => taskModelsAutosave.participant.runSave('manual')}
  />
</div>

<style>
  /* The JSON disclosures and the reset action share one line below the
     ordinary options; an opened disclosure grows downwards while the reset
     action stays at the line's end. */
  .s-task-model-more {
    display: flex;
    align-items: flex-start;
    gap: 12px;
  }

  .s-task-model-more__json {
    display: grid;
    flex: 1 1 auto;
    min-width: 0;
  }

  .s-task-model-more :global(.s-task-model-reset) {
    flex: 0 0 auto;
    margin-left: auto;
  }

  .s-task-memory__head {
    display: flex;
    align-items: center;
    gap: 6px;
  }
</style>
