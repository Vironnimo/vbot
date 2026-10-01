// The editing owner behind every Task Model binding control in Settings: the
// scoped binding draft and its autosave, the target lists, the selected
// targets' option schemas and the option edits. `SettingsSpecializedModelsPanel`
// and the Conversation search panel present it differently; both create one
// during component initialization, because it registers its autosave
// participant and its lifecycle with the creating component.

import { onDestroy, onMount, untrack } from 'svelte';

import {
  getTaskModelOptions,
  listTaskModelTargets,
  updateTaskModelSettings,
} from '$lib/api.js';
import { createDebouncedAutosave, useAutosaveContext } from '$lib/autosave.js';
import { t, tOr } from '$lib/i18n.js';
import { shouldApplyReloadNow } from '$lib/resourceInvalidation.js';
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

const EMPTY_BINDING = Object.freeze({ target: '', options: {} });

/**
 * @param {object} params
 * @param {string[] | null} params.taskTypes - The task types this placement
 *   owns; null owns every Task Model row.
 * @param {() => object} params.getSettings - The current Settings prop.
 * @param {() => number} params.getModelsRefreshToken - Bumps when Models or
 *   Providers changed; the targets reload once the editor is idle.
 * @param {(settings: object) => void} params.onCommit - Receives saved Settings.
 * @param {(message: string) => void} params.onError - Sets/clears the error.
 */
export function createTaskModelEditor({
  taskTypes,
  getSettings,
  getModelsRefreshToken,
  onCommit,
  onError,
}) {
  // Each placement owns only its task types; another page's commit must
  // never make an untouched binding here look like a draft to be saved.
  const rows = TASK_MODEL_ROWS.filter(
    (row) => taskTypes === null || taskTypes.includes(row.taskType),
  );

  function scopedBindings(value) {
    const bindings = normalizeTaskModelSettings(value);
    return Object.fromEntries(
      rows.map((row) => [row.taskType, bindings[row.taskType]]),
    );
  }

  // Only this placement's bindings; with `origin`, only those the draft
  // changed, so an untouched binding is never written back.
  function bindingsPayload(bindings, origin) {
    const payload = createTaskModelUpdatePayload(bindings, origin);
    return {
      model_tasks: Object.fromEntries(
        rows
          .filter((row) => Object.hasOwn(payload, row.taskType))
          .map((row) => [row.taskType, payload[row.taskType]]),
      ),
    };
  }

  // Seeded once from the Settings at creation; later commits flow back
  // through the saved-state comparison.
  let bindings = $state(untrack(() => scopedBindings(getSettings())));
  // Counts user edits, so a save that finishes after another edit stays armed.
  let edits = 0;
  const draft = createSettingsDraft({
    settings: untrack(() => getSettings()),
    fromSettings: scopedBindings,
    read: () => bindings,
    write: replaceBindings,
    toPayload: bindingsPayload,
    submit: async ({ model_tasks: modelTasks, base }) => {
      const result = await updateTaskModelSettings(modelTasks, {
        base: base.model_tasks,
      });
      return { ...getSettings(), model_tasks: result.model_tasks ?? {} };
    },
    // A binding is one choice: its options belong to its target, so a
    // binding saved elsewhere replaces the draft's binding as a whole.
    leafDepth: 1,
  });
  let targetsByType = $state({});
  let schemasByType = $state({});
  let loading = $state(false);
  // True once the target lists have loaded.
  let loaded = $state(false);
  let saving = $state(false);
  // Per-field JSON parse errors keyed by `${taskType}::${field.name}`. The
  // binding is never updated with an invalid value; this map only drives the
  // inline error message under the textarea.
  let jsonErrors = $state({});
  let autoSaveArmed = $state(false);
  // A queued target reload waits here while the user is actively editing,
  // since a reload replaces the available option controls.
  let pendingReload = $state(false);
  let lastModelsRefreshToken = null;
  let schemaRequestIds = {};
  let destroyed = false;

  let saveDisabled = $derived(
    saving ||
      loading ||
      taskModelBindingsMatch(bindings, scopedBindings(getSettings())),
  );
  // "Busy" while loading, saving, or holding unsaved edits: a reload during
  // any of those would disturb in-progress work, so it waits until idle.
  let surfaceBusy = $derived(
    loading || saving || (autoSaveArmed && !saveDisabled),
  );
  const autosaveContext = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => bindings,
    hasChanges: () =>
      autoSaveArmed &&
      !taskModelBindingsMatch(bindings, scopedBindings(getSettings())),
    save: saveBindings,
  });
  const unregisterAutosave = autosaveContext.register(autosave.participant);

  onMount(() => {
    void load();
  });

  onDestroy(() => {
    destroyed = true;
    schemaRequestIds = {};
    unregisterAutosave();
    autosave.cancelPendingTimer();
  });

  // Auto-save is armed only after a real user edit. Schema defaults are
  // display fallbacks and remain owned by the backend.
  $effect(() => {
    if (!autoSaveArmed || saveDisabled) {
      return;
    }

    autosave.scheduleRun();

    return () => {
      autosave.cancelPendingTimer();
    };
  });

  // A `resource_changed(models|providers)` signal queues a target reload
  // (first run is a no-op: mount already loaded).
  $effect(() => {
    const token = getModelsRefreshToken();
    if (lastModelsRefreshToken === null) {
      lastModelsRefreshToken = token;
      return;
    }
    if (token !== lastModelsRefreshToken) {
      lastModelsRefreshToken = token;
      pendingReload = true;
    }
  });

  // Run the queued reload once the surface is idle, so it never re-applies
  // option controls during an edit the user is mid-way through.
  $effect(() => {
    if (pendingReload && shouldApplyReloadNow({ savePending: surfaceBusy })) {
      pendingReload = false;
      void load();
    }
  });

  async function load() {
    if (loading) {
      return;
    }

    loading = true;
    onError('');

    try {
      const targetEntries = await Promise.all(
        rows.map(async (row) => {
          const result = await listTaskModelTargets(row.taskType);
          return [row.taskType, normalizeTargets(result)];
        }),
      );
      targetsByType = Object.fromEntries(targetEntries);
      loaded = true;

      for (const row of rows) {
        const target = bindings[row.taskType]?.target ?? '';
        if (target) {
          await loadSchema(row.taskType, target);
        }
      }
    } catch (error) {
      onError(`${t('settings.specializedModels.loadError')} ${error.message}`);
    } finally {
      loading = false;
    }
  }

  async function refreshTargets(taskType) {
    const targets = await listTaskModelTargets(taskType);
    if (!destroyed)
      targetsByType = {
        ...targetsByType,
        [taskType]: normalizeTargets(targets),
      };
  }

  async function loadSchema(taskType, target) {
    const requestId = (schemaRequestIds[taskType] ?? 0) + 1;
    schemaRequestIds = { ...schemaRequestIds, [taskType]: requestId };
    const isCurrentRequest = () =>
      !destroyed &&
      schemaRequestIds[taskType] === requestId &&
      (bindings[taskType]?.target ?? '') === target;

    if (!target) {
      if (!isCurrentRequest()) {
        return false;
      }
      schemasByType = { ...schemasByType, [taskType]: [] };
      clearJsonErrors(taskType);
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
    schemasByType = {
      ...schemasByType,
      [taskType]: normalizeOptionSchema(result),
    };
    clearJsonErrors(taskType);
    return true;
  }

  async function saveBindings() {
    if (
      !autoSaveArmed ||
      taskModelBindingsMatch(bindings, scopedBindings(getSettings()))
    ) {
      return true;
    }

    const editsAtStart = edits;
    const saved = await draft.save({
      onCommit,
      onError,
      setSaving: (value) => (saving = value),
    });
    if (saved && edits === editsAtStart) {
      autoSaveArmed = false;
    }
    return saved;
  }

  // A rebase can replace a binding with one saved elsewhere; a changed target
  // needs its own option controls.
  function replaceBindings(next) {
    const previous = bindings;
    bindings = next;
    for (const row of rows) {
      const target = next[row.taskType]?.target ?? '';
      if (target !== (previous[row.taskType]?.target ?? '')) {
        loadSchema(row.taskType, target).catch((error) => {
          onError(
            `${t('settings.specializedModels.optionsLoadError')} ${error.message}`,
          );
        });
      }
    }
  }

  function markEdit() {
    edits += 1;
    autoSaveArmed = true;
  }

  async function setTarget(taskType, target) {
    onError('');
    markEdit();
    bindings = { ...bindings, [taskType]: { target, options: {} } };

    try {
      await loadSchema(taskType, target);
    } catch (error) {
      onError(
        `${t('settings.specializedModels.optionsLoadError')} ${error.message}`,
      );
    }
  }

  function handleOptionInput(taskType, field, event) {
    if (field.type === JSON_OPTION_TYPE) {
      const { value, error } = parseJsonFieldValue(event.currentTarget.value);
      setJsonError(taskType, field, error);
      if (error === '' && value !== undefined) {
        setOption(taskType, field, value);
      }
      return;
    }
    setOption(taskType, field, valueFromOptionField(field, event));
  }

  function setOption(taskType, field, value) {
    const currentBinding = bindings[taskType] ?? EMPTY_BINDING;
    // A select whose choices depend on this field never keeps a hidden value.
    const options = reconcileDependentOptions(
      schemasByType[taskType] ?? [],
      { ...(currentBinding.options ?? {}), [field.name]: value },
      field.name,
    );
    bindings = { ...bindings, [taskType]: { ...currentBinding, options } };
    onError('');
    markEdit();
  }

  function resetOptions(taskType) {
    bindings = {
      ...bindings,
      [taskType]: { ...bindings[taskType], options: {} },
    };
    clearJsonErrors(taskType);
    onError('');
    markEdit();
  }

  function valueFromOptionField(field, event) {
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

  function binding(taskType) {
    return bindings[taskType] ?? EMPTY_BINDING;
  }

  function targets(taskType) {
    return targetsByType[taskType] ?? [];
  }

  // The searchable picker's choices: "Not configured", every target, and a
  // saved target the server no longer lists.
  function targetOptions(taskType) {
    const current = binding(taskType);
    const options = [
      { value: '', label: t('settings.specializedModels.noTarget') },
      ...targets(taskType).map((target) => ({
        value: target.id,
        label:
          target.kind === 'local' ? `${target.label} (local)` : target.label,
        searchText: `${target.label} ${target.id} ${target.kind === 'local' ? 'local' : ''}`,
      })),
    ];

    if (
      current.target &&
      !targets(taskType).some((target) => target.id === current.target)
    ) {
      options.push({
        value: current.target,
        label: t('settings.specializedModels.customTarget', {
          target: current.target,
        }),
      });
    }

    return options;
  }

  // The selected target's option fields, local engine labels translated;
  // fields another option's value makes irrelevant are left out.
  function visibleFields(taskType) {
    const fields = schemasByType[taskType] ?? [];
    const presented = binding(taskType).target.startsWith('local/')
      ? fields.map(localFieldPresentation)
      : fields;
    return presented.filter(
      (field) => !isOptionFieldHidden(field, fields, binding(taskType).options),
    );
  }

  function localFieldPresentation(field) {
    return {
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
    };
  }

  function canReset(taskType) {
    return Object.keys(binding(taskType).options).length > 0;
  }

  function fieldChoices(taskType, field) {
    return visibleFieldOptions(
      field,
      schemasByType[taskType] ?? [],
      binding(taskType).options,
    );
  }

  function optionValue(taskType, field) {
    const value = binding(taskType).options[field.name];
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

  function jsonError(taskType, field) {
    return jsonErrors[`${taskType}::${field.name}`] ?? '';
  }

  function setJsonError(taskType, field, message) {
    const key = `${taskType}::${field.name}`;
    const nextErrors = { ...jsonErrors };
    if (message) {
      nextErrors[key] = message;
    } else {
      delete nextErrors[key];
    }
    jsonErrors = nextErrors;
  }

  function clearJsonErrors(taskType) {
    const prefix = `${taskType}::`;
    jsonErrors = Object.fromEntries(
      Object.entries(jsonErrors).filter(([key]) => !key.startsWith(prefix)),
    );
  }

  return {
    rows,
    participant: autosave.participant,
    get bindings() {
      return bindings;
    },
    get loading() {
      return loading;
    },
    get loaded() {
      return loaded;
    },
    get saving() {
      return saving;
    },
    get surfaceBusy() {
      return surfaceBusy;
    },
    binding,
    targets,
    targetOptions,
    visibleFields,
    canReset,
    fieldChoices,
    optionValue,
    jsonError,
    setTarget,
    setOption,
    handleOptionInput,
    resetOptions,
    refreshTargets,
  };
}
