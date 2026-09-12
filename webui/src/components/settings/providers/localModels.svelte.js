import { listModels, updateSettings } from '$lib/api.js';
import { t } from '$lib/i18n.js';

export function createLocalProviderModels(context) {
  const LOCAL_CONTEXT_DEFAULT_CAP = 32768;

  // Flagged-local models (model.list → local: true), grouped per provider for
  // the "Local model context" editor inside that provider's card.
  let localModels = $state([]);

  // Draft input values for the context editor, keyed by full model id.
  let localContextDrafts = $state({});

  let localContextBusy = $state(false);

  let localContextWindows = $derived(
    context.settings?.local_models?.context_windows ?? {},
  );

  let localModelsByProvider = $derived(groupLocalModelsByProvider(localModels));

  async function loadLocalModels() {
    try {
      const result = await listModels();
      localModels = (result?.models ?? []).filter(
        (model) => model?.local === true,
      );
    } catch {
      localModels = [];
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

  async function saveLocalContextWindow(model, rawValue) {
    const trimmed = String(rawValue ?? '').trim();
    let value = null;
    if (trimmed !== '') {
      const parsed = Number(trimmed);
      if (!Number.isInteger(parsed) || parsed <= 0) {
        context.onToast({
          title: t(
            'settings.providers.localContext.invalidValue',
            'Context window must be a positive whole number',
          ),
          variant: 'error',
        });
        return;
      }
      value = parsed;
    }

    localContextBusy = true;
    localContextDrafts = { ...localContextDrafts, [model.id]: trimmed };
    try {
      await updateSettings({
        local_models: { context_windows: { [model.id]: value } },
      });
      context.onError('');
      await context.onReloadSettings();
      await loadLocalModels();
      localContextDrafts = {};
    } catch (error) {
      context.onToast({
        title: error?.message || String(error),
        variant: 'error',
      });
    } finally {
      localContextBusy = false;
    }
  }
  return {
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
    get saveLocalContextWindow() {
      return saveLocalContextWindow;
    },
  };
}
