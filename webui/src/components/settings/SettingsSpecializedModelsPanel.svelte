<script>
  import { onDestroy, onMount, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import {
    getTaskModelOptions,
    listTaskModelTargets,
    updateTaskModelSettings,
  } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    TASK_MODEL_ROWS,
    applyOptionDefaults,
    createTaskModelUpdatePayload,
    normalizeOptionSchema,
    normalizeTargets,
    normalizeTaskModelSettings,
    taskModelBindingsMatch,
  } from '$lib/taskModelSettings.js';

  const noop = () => {};
  const AUTO_SAVE_DEBOUNCE_MS = 800;

  let {
    settings = null,
    onCommit = noop,
    onToast = noop,
    onError = noop,
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
  let taskModelError = $state('');
  let autoSaveTimer = null;
  let autoSaveArmed = $state(false);

  let saveDisabled = $derived(
    taskModelSaving ||
      taskModelLoading ||
      taskModelBindingsMatch(
        taskModelBindings,
        normalizeTaskModelSettings(settings),
      ),
  );

  onMount(() => {
    void loadTaskModelPanel();
  });

  onDestroy(() => {
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
      void saveTaskModelBindings();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAutoSaveTimer();
    };
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
    taskModelError = '';

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
      taskModelError = `${t('settings.specializedModels.loadError', 'Specialized model targets could not be loaded.')} ${error.message}`;
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
      return;
    }

    const result = await getTaskModelOptions(taskType, target);
    const fields = normalizeOptionSchema(result);
    taskModelSchemasByType = {
      ...taskModelSchemasByType,
      [taskType]: fields,
    };
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
    void saveTaskModelBindings();
  }

  async function saveTaskModelBindings() {
    if (saveDisabled) {
      return;
    }

    taskModelSaving = true;
    taskModelError = '';
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
    } catch (error) {
      taskModelError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
    } finally {
      taskModelSaving = false;
    }
  }

  async function handleTaskModelTargetChange(taskType, target) {
    taskModelError = '';
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
      taskModelError = `${t('settings.specializedModels.optionsLoadError', 'Model options could not be loaded.')} ${error.message}`;
    }
  }

  function handleTaskModelOptionChange(taskType, field, event) {
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
    taskModelError = '';
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
      return field.default ?? '';
    }
    return value;
  }
</script>

{#if taskModelLoading}
  <div class="s-feedback s-feedback--neutral">
    {t(
      'settings.specializedModels.loading',
      'Loading specialized model targets…',
    )}
  </div>
{/if}

{#if taskModelError}
  <div class="s-feedback s-feedback--error">{taskModelError}</div>
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
          <Dropdown
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
            listClass="settings-view__thinking-list"
            onValueChange={(value) =>
              handleTaskModelTargetChange(row.taskType, value)}
          />
        </div>
      </div>

      {#if binding.target && fields.length > 0}
        <div class="s-task-model-options">
          {#each fields as field (field.name)}
            <svelte:element
              this={field.type === 'select' ? 'div' : 'label'}
              class="s-field"
            >
              <span class="s-field-label">{field.label}</span>
              {#if field.type === 'select'}
                <Dropdown
                  value={taskModelOptionValue(row.taskType, field)}
                  options={field.options}
                  ariaLabel={field.label}
                  disabled={taskModelSaving}
                  triggerClass="settings-view__dropdown"
                  listClass="settings-view__thinking-list"
                  onValueChange={(value) =>
                    setTaskModelOption(row.taskType, field, value)}
                />
              {:else if field.type === 'textarea'}
                <textarea
                  class="s-input s-textarea"
                  rows="3"
                  value={taskModelOptionValue(row.taskType, field)}
                  disabled={taskModelSaving}
                  oninput={(event) =>
                    handleTaskModelOptionChange(row.taskType, field, event)}
                ></textarea>
              {:else if field.type === 'number'}
                <input
                  class="s-input"
                  type="number"
                  min={field.min ?? undefined}
                  max={field.max ?? undefined}
                  step={field.step ?? 'any'}
                  value={taskModelOptionValue(row.taskType, field)}
                  disabled={taskModelSaving}
                  oninput={(event) =>
                    handleTaskModelOptionChange(row.taskType, field, event)}
                />
              {:else if field.type === 'boolean'}
                <input
                  class="s-checkbox"
                  type="checkbox"
                  checked={taskModelOptionValue(row.taskType, field) === true}
                  disabled={taskModelSaving}
                  onchange={(event) =>
                    handleTaskModelOptionChange(row.taskType, field, event)}
                />
              {:else}
                <input
                  class="s-input"
                  type="text"
                  value={taskModelOptionValue(row.taskType, field)}
                  disabled={taskModelSaving}
                  oninput={(event) =>
                    handleTaskModelOptionChange(row.taskType, field, event)}
                />
              {/if}
              {#if field.description}
                <span class="s-field-help">{field.description}</span>
              {/if}
            </svelte:element>
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
    </div>
  {/each}
</div>

<div class="s-footer">
  <button
    class="btn-primary s-save-button s-save-button--inline"
    type="button"
    onclick={handleManualTaskModelSave}
  >
    {taskModelSaving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
  </button>
</div>
