import { onDestroy, untrack } from 'svelte';
import { createDebouncedAutosave, useAutosaveContext } from '$lib/autosave.js';
import { listModels, updateSettings } from '$lib/api.js';
import { t } from '$lib/i18n.js';
import { isChatModel } from '$lib/modelSelection.js';

export function createLocalProviderModels(context) {
  const LOCAL_CONTEXT_DEFAULT_CAP = 32768;

  // Flagged-local models (model.list → local: true), grouped per provider for
  // the "Context windows" editor inside that provider's details.
  let localModels = $state([]);

  // Draft input values for the context editor, keyed by full model id.
  let localContextDrafts = $state({});

  let localContextBusy = $state(false);
  let baselines = $state({});
  const autosaveContext = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...localContextDrafts }),
    hasChanges,
    save: saveLocalContextWindows,
  });
  const unregister = autosaveContext.register(autosave.participant);
  $effect(() => {
    if (localContextBusy || !hasChanges()) return;
    autosave.scheduleRun();
    return autosave.cancelPendingTimer;
  });
  onDestroy(() => {
    unregister();
    autosave.cancelPendingTimer();
  });

  let localContextWindows = $derived(
    context.settings?.local_models?.context_windows ?? {},
  );

  let localModelsByProvider = $derived(groupLocalModelsByProvider(localModels));
  $effect(() => {
    void localContextWindows;
    untrack(() => {
      if (localContextBusy) return;
      for (const [id, raw] of Object.entries(localContextDrafts)) {
        if (normalized(raw) === baselines[id]) delete localContextDrafts[id];
      }
    });
  });

  async function loadLocalModels() {
    try {
      const result = await listModels();
      localModels = (result?.models ?? []).filter(
        (model) => model?.local === true && isChatModel(model),
      );
    } catch {
      // Keep the shown catalog and its mounted editors until a later refresh.
    }
  }

  function groupLocalModelsByProvider(models) {
    const grouped = {};
    for (const model of models) {
      if (!model?.provider_id) {
        continue;
      }
      if (!grouped[model.provider_id]) {
        grouped[model.provider_id] = [];
      }
      grouped[model.provider_id].push(model);
    }
    return grouped;
  }

  function localContextPlaceholder(model) {
    const effective =
      model?.effective_context_window ??
      Math.min(
        LOCAL_CONTEXT_DEFAULT_CAP,
        model?.context_window ?? LOCAL_CONTEXT_DEFAULT_CAP,
      );
    return String(effective);
  }

  function localContextDraftValue(model) {
    if (model.id in localContextDrafts) {
      return localContextDrafts[model.id];
    }
    const configured = localContextWindows[model.id];
    return configured === undefined || configured === null
      ? ''
      : String(configured);
  }

  function normalized(raw) {
    const trimmed = String(raw ?? '').trim();
    if (!trimmed) return null;
    const parsed = Number(trimmed);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : trimmed;
  }

  function setLocalContextDraft(model, value) {
    if (!(model.id in localContextDrafts)) {
      baselines[model.id] = localContextWindows[model.id] ?? null;
    }
    localContextDrafts[model.id] = value;
  }

  function hasChanges() {
    return Object.entries(localContextDrafts).some(
      ([id, raw]) => normalized(raw) !== baselines[id],
    );
  }

  async function saveLocalContextWindows() {
    const submitted = Object.fromEntries(
      Object.entries(localContextDrafts).filter(
        ([id, raw]) => normalized(raw) !== baselines[id],
      ),
    );
    const values = Object.fromEntries(
      Object.entries(submitted).map(([id, raw]) => [id, normalized(raw)]),
    );
    if (Object.values(values).some((value) => typeof value === 'string')) {
      context.onToast({
        title: t('settings.providers.localContext.invalidValue'),
        variant: 'error',
      });
      return false;
    }
    if (!Object.keys(values).length) return true;
    localContextBusy = true;
    try {
      await updateSettings({ local_models: { context_windows: values } });
      // Advance only the captured keys. Inputs remain editable while the write
      // and catalog refresh run, and later keystrokes remain pending.
      for (const [id, value] of Object.entries(values)) baselines[id] = value;
      context.onError('');
      await context.onRefreshProviderSettings();
      await loadLocalModels();
      for (const [id, raw] of Object.entries(submitted)) {
        if (localContextDrafts[id] === raw) delete localContextDrafts[id];
      }
      return true;
    } catch (error) {
      context.onToast({
        title: error?.message || String(error),
        variant: 'error',
      });
      return false;
    } finally {
      localContextBusy = false;
    }
  }
  return {
    participant: autosave.participant,
    setLocalContextDraft,
    get localContextBusy() {
      return localContextBusy;
    },
    set localContextBusy(value) {
      localContextBusy = value;
    },
    get localModelsByProvider() {
      return localModelsByProvider;
    },
    set localModelsByProvider(value) {
      localModelsByProvider = value;
    },
    get loadLocalModels() {
      return loadLocalModels;
    },
    get localContextPlaceholder() {
      return localContextPlaceholder;
    },
    get localContextDraftValue() {
      return localContextDraftValue;
    },
  };
}
