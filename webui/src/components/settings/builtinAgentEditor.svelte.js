// The editing owner of one built-in Agent in a Settings panel: the Agent as
// loaded, its form draft (`createAgentFormValues`) and the autosave that sends
// the changed fields with `agent.update`. Create it during component
// initialization, because it registers its autosave participant and its
// lifecycle with the creating component. An Agent of the user that holds the
// id is never edited: the loaded Agent must carry `builtin`.

import { onDestroy } from 'svelte';

import { getAgent, updateAgent } from '$lib/api.js';
import {
  AGENT_FORM_MODE_EDIT,
  createAgentFormValues,
  normalizeAgentForm,
} from '$lib/agentForm.js';
import { createDebouncedAutosave, useAutosaveContext } from '$lib/autosave.js';
import { t } from '$lib/i18n.js';
import { isPlainObject } from '$lib/values.js';

/**
 * @param {object} params
 * @param {string} params.agentId - The reserved id of the built-in Agent.
 * @param {string} params.builtin - The `builtin` kind the Agent must carry.
 * @param {(message: string) => void} params.onError - Sets/clears the error.
 */
export function createBuiltinAgentEditor({ agentId, builtin, onError }) {
  // Null while not loaded and when the id names no built-in Agent.
  let agent = $state(null);
  // True once a load finished, with or without the Agent.
  let loaded = $state(false);
  let loadError = $state('');
  let form = $state(createAgentFormValues({}));
  let baseline = $state(createAgentFormValues({}));
  let formErrors = $state({});
  let saving = $state(false);
  let disposed = false;
  let loadVersion = 0;

  let effective = $derived(
    isPlainObject(agent?.effective) ? agent.effective : {},
  );

  const autosaveContext = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => JSON.stringify(form),
    hasChanges,
    save,
  });
  const unregisterAutosave = autosaveContext.register(autosave.participant);

  $effect(() => {
    if (saving || !hasChanges()) return;
    autosave.scheduleRun();
    return () => {
      autosave.cancelPendingTimer();
    };
  });

  onDestroy(() => {
    disposed = true;
    unregisterAutosave();
    autosave.cancelPendingTimer();
  });

  async function load() {
    const current = ++loadVersion;
    try {
      const result = await getAgent(agentId);
      if (disposed || current !== loadVersion) return;
      if (result?.builtin === builtin) apply(result);
      else agent = null;
      loadError = '';
    } catch (error) {
      if (disposed || current !== loadVersion) return;
      loadError = error?.message || String(error);
    } finally {
      if (!disposed && current === loadVersion) loaded = true;
    }
  }

  // The loaded or saved Agent becomes the baseline. It replaces the form
  // unless the form holds edits: edits since the load, or since `draft` was
  // sent to be saved.
  function apply(next, draft = baseline) {
    const replaceForm = formValuesMatch(form, draft);
    agent = next;
    baseline = createAgentFormValues(next);
    if (replaceForm) form = createAgentFormValues(next);
  }

  function formValuesMatch(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  function changes() {
    return normalizeAgentForm(form, {
      mode: AGENT_FORM_MODE_EDIT,
      initialValues: baseline,
    });
  }

  function hasChanges() {
    if (!agent) return false;
    const result = changes();
    if (!result.isValid) return !formValuesMatch(form, baseline);
    return Object.keys(result.payload).some((field) => field !== 'id');
  }

  async function save(reason) {
    if (!agent || saving) return false;
    const result = changes();
    if (reason !== 'auto' || result.isValid) formErrors = result.errors;
    if (!result.isValid) {
      if (reason !== 'auto') onError(t('errors.validation'));
      return false;
    }
    if (!Object.keys(result.payload).some((field) => field !== 'id'))
      return true;
    const draft = JSON.parse(JSON.stringify(form));
    saving = true;
    try {
      const saved = await updateAgent({ ...result.payload, id: agentId });
      if (disposed) return true;
      apply(
        isPlainObject(saved) ? saved : { ...agent, ...result.payload },
        draft,
      );
      onError('');
      return true;
    } catch (error) {
      if (!disposed) onError(error?.message || t('agents.saveError'));
      return false;
    } finally {
      saving = false;
    }
  }

  function inheritSource(fieldName) {
    const field = effective[fieldName];
    return isPlainObject(field) ? (field.source ?? null) : null;
  }

  function inheritDisplayValue(fieldName) {
    const field = effective[fieldName];
    const value = isPlainObject(field) ? field.value : null;
    return value === null || value === undefined ? '' : String(value);
  }

  function fieldError(fieldName) {
    return formErrors[fieldName] ? t('errors.validation') : '';
  }

  return {
    participant: autosave.participant,
    get agent() {
      return agent;
    },
    get loaded() {
      return loaded;
    },
    get loadError() {
      return loadError;
    },
    get form() {
      return form;
    },
    set form(next) {
      form = next;
    },
    get formErrors() {
      return formErrors;
    },
    get saving() {
      return saving;
    },
    load,
    inheritSource,
    inheritDisplayValue,
    fieldError,
  };
}
