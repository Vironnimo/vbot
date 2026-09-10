<script>
  import { onDestroy, onMount, untrack } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  import LocalSpeechSupport from './LocalSpeechSupport.svelte';
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
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
  import { t } from '$lib/i18n.js';
  import {
    JSON_OPTION_TYPE,
    TASK_MODEL_ROWS,
    createTaskModelUpdatePayload,
    normalizeOptionSchema,
    normalizeTargets,
    normalizeTaskModelSettings,
    parseJsonFieldValue,
    stringifyJsonFieldValue,
    taskModelBindingsMatch,
  } from '$lib/taskModelSettings.js';
  import {
    SURFACE_FORM,
    shouldApplyReloadNow,
  } from '$lib/resourceInvalidation.js';

  const noop = () => {};

  let {
    settings = null,
    onCommit = noop,
    onToast = noop,
    onError = noop,
    modelsRefreshToken = 0,
  } = $props();

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let taskModelBindings = $state(
    untrack(() => normalizeTaskModelSettings(settings)),
  );
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
      taskModelBindingsMatch(
        taskModelBindings,
        normalizeTaskModelSettings(settings),
      ),
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
      !taskModelBindingsMatch(
        taskModelBindings,
        normalizeTaskModelSettings(settings),
      ),
    save: saveTaskModelBindings,
  });
  const unregisterTaskModelsAutosave = autosaveContext.register(
    taskModelsAutosave.participant,
  );

  onMount(() => {
    void loadTaskModelPanel();
    void refreshSpeechMemory();
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
      shouldApplyReloadNow(SURFACE_FORM, { savePending: taskSurfaceBusy })
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
        TASK_MODEL_ROWS.map(async (row) => {
          const result = await listTaskModelTargets(row.taskType);
          return [row.taskType, normalizeTargets(result)];
        }),
      );
      taskModelTargetsByType = Object.fromEntries(targetEntries);

      for (const row of TASK_MODEL_ROWS) {
        const target = taskModelBindings[row.taskType]?.target ?? '';
        if (target) {
          await loadTaskModelSchema(row.taskType, target);
        }
      }
    } catch (error) {
      onError(
        `${t('settings.specializedModels.loadError', 'Specialized model targets could not be loaded.')} ${error.message}`,
      );
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

  function handleManualTaskModelSave() {
    if (taskModelSaving) {
      return;
    }

    if (saveDisabled) {
      onToast({
        title: t('common.alreadySaved', 'Already saved'),
        variant: 'success',
      });
      return;
    }

    taskModelsAutosave.cancelPendingTimer();
    void taskModelsAutosave.participant.runSave('manual');
  }

  async function saveTaskModelBindings() {
    if (
      !autoSaveArmed ||
      taskModelBindingsMatch(
        taskModelBindings,
        normalizeTaskModelSettings(settings),
      )
    ) {
      return true;
    }

    taskModelSaving = true;
    onError('');

    try {
      const result = await updateTaskModelSettings(
        createTaskModelUpdatePayload(
          taskModelBindings,
          normalizeTaskModelSettings(settings),
        ),
      );
      const nextSettings = {
        ...settings,
        model_tasks: result.model_tasks ?? {},
      };
      onCommit(nextSettings);
      taskModelBindings = normalizeTaskModelSettings(nextSettings);
      autoSaveArmed = false;
      onToast({
        title: t(
          'settings.specializedModels.saveSuccess',
          'Specialized model bindings updated.',
        ),
        variant: 'success',
      });
      return true;
    } catch (error) {
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
      return false;
    } finally {
      taskModelSaving = false;
    }
  }

  async function handleTaskModelTargetChange(taskType, target) {
    onError('');
    autoSaveArmed = true;
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
        `${t('settings.specializedModels.optionsLoadError', 'Model options could not be loaded.')} ${error.message}`,
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
    taskModelBindings = {
      ...taskModelBindings,
      [taskType]: {
        ...currentBinding,
        options: {
          ...(currentBinding.options ?? {}),
          [field.name]: value,
        },
      },
    };
    onError('');
    autoSaveArmed = true;
  }

  function resetTaskModelOptions(taskType) {
    taskModelBindings = {
      ...taskModelBindings,
      [taskType]: { ...taskModelBindings[taskType], options: {} },
    };
    clearTaskModelJsonErrors(taskType);
    onError('');
    autoSaveArmed = true;
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
        label: t('settings.specializedModels.noTarget', 'Not configured'),
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
        label: t(
          'settings.specializedModels.customTarget',
          'Custom target: {target}',
          { target: binding.target },
        ),
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
      label: t(`settings.localSpeech.options.${field.name}.label`, field.label),
      description: field.description
        ? t(
            `settings.localSpeech.options.${field.name}.help`,
            field.description,
          )
        : '',
      options: field.options.map((option) => ({
        ...option,
        label: t(`settings.localSpeech.choices.${option.value}`, option.label),
      })),
    }));
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
    {t(
      'settings.specializedModels.loading',
      'Loading specialized model targets…',
    )}
  </Banner>
{/if}

{#if showSpeechMemory}
  <div data-local-speech-memory>
    <div class="s-row-label">{t('settings.localSpeech.memoryTitle')}</div>
    <div class="s-row-desc">{t('settings.localSpeech.memoryHelp')}</div>
    {#if speechMemoryError}<div role="alert">{speechMemoryError}</div>{/if}
    {#if !speechMemory && !speechMemoryError}
      <div role="status">{t('settings.localSpeech.memoryChecking')}</div>
    {/if}
    {#each speechMemoryModels as model (model.target)}
      <div class="s-row" data-speech-memory-target={model.target}>
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
            ariaLabel={t('settings.localSpeech.unloadAria', undefined, {
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

<div class="s-task-model-list">
  {#each TASK_MODEL_ROWS as row (row.taskType)}
    {@const binding = taskModelBindings[row.taskType] ?? {
      target: '',
      options: {},
    }}
    {@const fields = taskModelFields(row.taskType)}
    {@const selectedTarget = taskModelTargets(row.taskType).find(
      (target) => target.id === binding.target,
    )}
    <div class="s-row s-row--stacked s-task-model-row">
      <div class="s-task-model-head">
        <div class="s-row-info">
          <div class="s-row-label">
            {t(row.titleKey, row.titleFallback)}
          </div>
          <div class="s-row-desc">
            {t(row.descriptionKey, row.descriptionFallback)}
          </div>
        </div>
        <div class="s-row-control s-row-control--task-model">
          <SearchableDropdown
            id={`settings-specialized-${row.taskType}`}
            value={binding.target}
            options={taskModelTargetOptions(row.taskType, binding)}
            placeholder={t(
              'settings.specializedModels.noTarget',
              'Not configured',
            )}
            ariaLabel={t(row.titleKey, row.titleFallback)}
            disabled={taskModelLoading || taskModelSaving}
            triggerClass="settings-view__dropdown"
            onValueChange={(value) =>
              handleTaskModelTargetChange(row.taskType, value)}
          />
        </div>
      </div>

      {#if binding.target && fields.length > 0}
        <div class="s-task-model-options">
          {#each fields as field (field.name)}
            {@const jsonError =
              field.type === JSON_OPTION_TYPE
                ? taskModelJsonError(row.taskType, field)
                : ''}
            {@const fieldControlId = `task-model-${row.taskType}-${field.name}`}
            <FormField
              controlId={fieldControlId}
              full={field.type === JSON_OPTION_TYPE}
              label={field.label}
              help={field.description ?? ''}
              error={jsonError
                ? t(
                    'settings.specializedModels.jsonInvalid',
                    'Invalid JSON: {error}',
                    { error: jsonError },
                  )
                : ''}
            >
              {#snippet children(formField)}
                {#if field.type === 'select'}
                  <Dropdown
                    id={formField.controlId}
                    value={taskModelOptionValue(row.taskType, field)}
                    options={field.options}
                    ariaLabel={field.label}
                    ariaDescribedby={formField.describedBy}
                    disabled={taskModelSaving}
                    triggerClass="settings-view__dropdown"
                    listClass="settings-view__thinking-list"
                    onValueChange={(value) =>
                      setTaskModelOption(row.taskType, field, value)}
                  />
                {:else if field.type === 'textarea'}
                  <TextArea
                    id={formField.controlId}
                    rows="3"
                    aria-describedby={formField.describedBy}
                    value={taskModelOptionValue(row.taskType, field)}
                    disabled={taskModelSaving}
                    onInput={(_value, event) =>
                      handleTaskModelOptionChange(row.taskType, field, event)}
                  />
                {:else if field.type === JSON_OPTION_TYPE}
                  <TextArea
                    id={formField.controlId}
                    code
                    invalid={formField.invalid}
                    rows="8"
                    spellcheck="false"
                    autocapitalize="off"
                    autocorrect="off"
                    aria-describedby={formField.describedBy}
                    placeholder={t(
                      'settings.specializedModels.jsonPlaceholder',
                      '[ … ] or { … }',
                    )}
                    value={taskModelOptionValue(row.taskType, field)}
                    disabled={taskModelSaving}
                    onInput={(_value, event) =>
                      handleTaskModelOptionChange(row.taskType, field, event)}
                  />
                {:else if field.type === 'number'}
                  <TextField
                    id={formField.controlId}
                    type="number"
                    aria-describedby={formField.describedBy}
                    min={field.min ?? undefined}
                    max={field.max ?? undefined}
                    step={field.step ?? 'any'}
                    value={taskModelOptionValue(row.taskType, field)}
                    disabled={taskModelSaving}
                    onInput={(_next, event) =>
                      handleTaskModelOptionChange(row.taskType, field, event)}
                  />
                {:else if field.type === 'boolean'}
                  <Toggle
                    id={formField.controlId}
                    checked={taskModelOptionValue(row.taskType, field) === true}
                    disabled={taskModelSaving}
                    ariaLabel={field.label}
                    aria-describedby={formField.describedBy}
                    onChange={(next) =>
                      setTaskModelOption(row.taskType, field, next)}
                  />
                {:else}
                  <TextField
                    id={formField.controlId}
                    value={taskModelOptionValue(row.taskType, field)}
                    aria-describedby={formField.describedBy}
                    disabled={taskModelSaving}
                    onInput={(_next, event) =>
                      handleTaskModelOptionChange(row.taskType, field, event)}
                  />
                {/if}
              {/snippet}
            </FormField>
          {/each}
        </div>
      {:else if binding.target}
        <div class="s-row-desc">
          {t(
            'settings.specializedModels.noOptions',
            'This target has no configurable options.',
          )}
        </div>
      {/if}

      {#if binding.target && Object.keys(binding.options).length > 0}
        <Button
          disabled={taskModelSaving || taskModelLoading}
          onClick={() => resetTaskModelOptions(row.taskType)}
          >{t(
            'settings.specializedModels.resetOptions',
            'Reset options',
          )}</Button
        >
      {/if}

      {#if ['speech_to_text', 'text_to_speech'].includes(row.taskType) && selectedTarget?.kind === 'local'}
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
  {/each}
</div>

<div class="s-footer">
  <Button
    variant="primary"
    class="s-save-button s-save-button--inline"
    onClick={handleManualTaskModelSave}
  >
    {taskModelSaving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
  </Button>
</div>
