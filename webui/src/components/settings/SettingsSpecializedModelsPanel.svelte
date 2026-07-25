<script>
  import { onDestroy, onMount, untrack } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import {
    getTaskModelOptions,
    listTaskModelTargets,
    updateTaskModelSettings,
  } from '$lib/api.js';
  import {
    createAutosaveParticipant,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import {
    JSON_OPTION_TYPE,
    TASK_MODEL_ROWS,
    applyOptionDefaults,
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
  const AUTO_SAVE_DEBOUNCE_MS = 800;

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
  let autoSaveTimer = null;
  let autoSaveArmed = $state(false);
  // Disclosure state: per-target option blocks start collapsed; the sub stays
  // in the DOM so settings search still matches option labels.
  const expandedTaskOptions = new SvelteSet();

  function toggleTaskOptions(taskType) {
    if (expandedTaskOptions.has(taskType)) {
      expandedTaskOptions.delete(taskType);
    } else {
      expandedTaskOptions.add(taskType);
    }
  }
  // A queued model reload waits here while the user is actively editing, since
  // a target reload re-applies option defaults onto the bindings.
  let pendingTaskModelReload = $state(false);
  let lastModelsRefreshToken = null;

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
  const taskModelsAutosave = createAutosaveParticipant({
    cancelPending: clearAutoSaveTimer,
    getSnapshot: () => taskModelBindings,
    hasChanges: () =>
      autoSaveArmed &&
      !taskModelBindingsMatch(
        taskModelBindings,
        normalizeTaskModelSettings(settings),
      ),
    save: saveTaskModelBindings,
  });
  const unregisterTaskModelsAutosave =
    autosaveContext.register(taskModelsAutosave);

  onMount(() => {
    void loadTaskModelPanel();
  });

  onDestroy(() => {
    unregisterTaskModelsAutosave();
    clearAutoSaveTimer();
  });

  // Auto-save is armed only after a real user edit so that applying option
  // defaults during the initial load does not silently persist settings.
  $effect(() => {
    if (!autoSaveArmed || saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void taskModelsAutosave.runSave();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAutoSaveTimer();
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
  // option defaults over an edit the user is mid-way through.
  $effect(() => {
    if (
      pendingTaskModelReload &&
      shouldApplyReloadNow(SURFACE_FORM, { savePending: taskSurfaceBusy })
    ) {
      pendingTaskModelReload = false;
      void loadTaskModelPanel();
    }
  });

  function clearAutoSaveTimer() {
    if (autoSaveTimer !== null) {
      clearTimeout(autoSaveTimer);
      autoSaveTimer = null;
    }
  }

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
    if (!target) {
      taskModelSchemasByType = {
        ...taskModelSchemasByType,
        [taskType]: [],
      };
      clearTaskModelJsonErrors(taskType);
      return;
    }

    const result = await getTaskModelOptions(taskType, target);
    const fields = normalizeOptionSchema(result);
    taskModelSchemasByType = {
      ...taskModelSchemasByType,
      [taskType]: fields,
    };
    clearTaskModelJsonErrors(taskType);
    taskModelBindings = {
      ...taskModelBindings,
      [taskType]: applyOptionDefaults(taskModelBindings[taskType], fields),
    };
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

    clearAutoSaveTimer();
    void taskModelsAutosave.runSave('manual');
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
        createTaskModelUpdatePayload(taskModelBindings),
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
        label: target.label,
        searchText: `${target.label} ${target.id}`,
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
    return taskModelSchemasByType[taskType] ?? [];
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

<div class="s-task-model-list">
  {#each TASK_MODEL_ROWS as row (row.taskType)}
    {@const binding = taskModelBindings[row.taskType] ?? {
      target: '',
      options: {},
    }}
    {@const fields = taskModelFields(row.taskType)}
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
          {#if binding.target && fields.length > 0}
            <Button
              variant="tertiary"
              icon
              class="s-disclosure-btn"
              ariaLabel={t(
                'settings.specializedModels.optionsAria',
                'Options for {task}',
                { task: t(row.titleKey, row.titleFallback) },
              )}
              aria-expanded={expandedTaskOptions.has(row.taskType)}
              onClick={() => toggleTaskOptions(row.taskType)}
            >
              ▸
            </Button>
          {/if}
        </div>
      </div>

      {#if binding.target && fields.length > 0}
        <div
          class="s-disclosure-sub"
          hidden={!expandedTaskOptions.has(row.taskType)}
        >
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
                      checked={taskModelOptionValue(row.taskType, field) ===
                        true}
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
        </div>
      {:else if binding.target}
        <div class="s-row-desc">
          {t(
            'settings.specializedModels.noOptions',
            'This target has no configurable options.',
          )}
        </div>
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
