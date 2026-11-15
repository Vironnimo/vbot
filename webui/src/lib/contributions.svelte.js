import { getContext, setContext } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { contributionContext } from './contributions.js';
import {
  getSettings,
  updateSettings,
  readExtensionContributionSources,
} from './api.js';
import { createContributionSources } from './contributionSources.js';
import { createSettingsDraft, rebaseDraft } from './settingsSave.js';
import { createAutosaveParticipant } from './autosave.js';
import { t } from './i18n.js';

const CONTEXT = Symbol('ui-contributions');
const STORAGE_KEY = 'vbot.ui-layout.v1';

function readLayouts() {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}');
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    return Object.fromEntries(
      Object.entries(value)
        .filter(
          ([, layout]) =>
            layout && typeof layout === 'object' && !Array.isArray(layout),
        )
        .map(([surface, layout]) => [
          surface,
          {
            ...layout,
            hidden: Array.isArray(layout.hidden)
              ? layout.hidden.filter((id) => typeof id === 'string')
              : [],
            order: Array.isArray(layout.order)
              ? layout.order.filter((id) => typeof id === 'string')
              : [],
            replacements:
              layout.replacements &&
              typeof layout.replacements === 'object' &&
              !Array.isArray(layout.replacements)
                ? Object.fromEntries(
                    Object.entries(layout.replacements).filter(
                      ([, id]) => typeof id === 'string',
                    ),
                  )
                : {},
          },
        ]),
    );
  } catch {
    return {};
  }
}

export function createContributions(source) {
  const sourceReads = createContributionSources({
    read: (requests, options) =>
      readExtensionContributionSources(requests, options),
    subscribe: (listener) => source.subscribe?.(listener),
  });
  let legacy = $state(readLayouts());
  let layouts = $state(null);
  let baseline = $state(null);
  let ready = $state(false);
  let saving = $state(false);
  let error = $state('');
  let mounted = $state({});
  const registrations = new SvelteMap();
  let readRevision = 0;
  let refreshQueued = false;
  let refreshPromise = null;
  let connected = false;
  const copy = (value) => JSON.parse(JSON.stringify(value));
  const fromSettings = (settings) => settings?.appearance?.ui_layout ?? null;
  const current = () => layouts ?? legacy;
  const pending = () =>
    ready && JSON.stringify(layouts) !== JSON.stringify(baseline);
  const migrationPending = () =>
    ready &&
    baseline === null &&
    layouts === null &&
    Object.keys(legacy).length > 0;

  function acceptSettings(settings) {
    if (!settings) return;
    if (saving) {
      refreshQueued = true;
      return;
    }
    const saved = copy(fromSettings(settings));
    const { value, conflicts } = rebaseDraft(layouts, baseline, saved);
    layouts = value;
    baseline = saved;
    ready = true;
    if (conflicts.length) error = t('settings.saveConflict');
  }

  async function refresh() {
    refreshQueued = true;
    if (saving) return false;
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      while (refreshQueued && !saving) {
        refreshQueued = false;
        const revision = ++readRevision;
        try {
          const settings = await (source.readSettings ?? getSettings)();
          if (revision !== readRevision) continue;
          error = '';
          acceptSettings(settings);
          source.onSettingsCommit?.(settings);
        } catch (failure) {
          if (revision === readRevision)
            error = `${t('settings.saveError')} ${failure.message}`;
          return false;
        }
      }
      return true;
    })();
    try {
      return await refreshPromise;
    } finally {
      refreshPromise = null;
    }
  }

  const participant = createAutosaveParticipant({
    getSnapshot: () => layouts,
    hasChanges: pending,
    save: async () => {
      ++readRevision;
      const draft = createSettingsDraft({
        settings: { appearance: { ui_layout: copy(baseline) } },
        fromSettings,
        read: () => layouts,
        write: (value) => {
          layouts = value;
        },
        toPayload: (ui_layout) => ({ appearance: { ui_layout } }),
        submit: source.writeSettings ?? updateSettings,
      });
      const success = await draft.save({
        onCommit: (settings) => {
          baseline = copy(fromSettings(settings));
          source.onSettingsCommit?.(settings);
        },
        onError: (message) => {
          error = message;
        },
        setSaving: (value) => {
          saving = value;
        },
      });
      if (success && baseline !== null) {
        // Only retire the browser copy after a confirmed server write. A
        // denied storage cleanup is harmless: the saved server layout wins.
        try {
          localStorage.removeItem(STORAGE_KEY);
        } catch {
          /* retained backup */
        }
        legacy = {};
      }
      if (connected && refreshQueued) void refresh();
      return success;
    },
  });
  acceptSettings(source.settings);

  function publishMounted() {
    const merged = {};
    for (const { surface, entries } of registrations.values()) {
      merged[surface] ??= [];
      for (const entry of entries) {
        if (!merged[surface].some((item) => item.id === entry.id))
          merged[surface].push({
            id: entry.id,
            title: entry.title,
            order: entry.order,
          });
      }
    }
    mounted = merged;
  }

  function save(surface, update) {
    if (!ready || migrationPending()) return Promise.resolve(false);
    layouts = { ...current(), [surface]: { ...current()[surface], ...update } };
    return participant.runSave();
  }

  return {
    get entries() {
      return source.entries ?? [];
    },
    get pages() {
      return source.pages ?? [];
    },
    get apiCapabilities() {
      return source.apiCapabilities ?? { version: null, features: [] };
    },
    get theme() {
      return source.theme ?? {};
    },
    get locale() {
      return source.locale ?? 'en';
    },
    get timezone() {
      return source.timezone ?? 'UTC';
    },
    get mounted() {
      return mounted;
    },
    get ready() {
      return ready;
    },
    get saving() {
      return saving;
    },
    get error() {
      return error;
    },
    get pending() {
      return pending();
    },
    get migrationPending() {
      return migrationPending();
    },
    participant,
    acceptSettings,
    refresh,
    retry: () => (ready ? participant.runSave('manual') : refresh()),
    migrate(useLegacy) {
      if (!migrationPending()) return Promise.resolve(false);
      layouts = useLegacy ? copy(legacy) : {};
      return participant.runSave('manual');
    },
    get customized() {
      return Object.values(current()).some(
        (layout) =>
          layout?.hidden?.length ||
          layout?.order?.length ||
          Object.values(layout?.replacements ?? {}).some(Boolean),
      );
    },
    get commandNames() {
      return Object.keys(source.commands ?? {});
    },
    layout: (surface) => current()[surface] ?? {},
    save,
    reset(surface) {
      return save(surface, { hidden: [], order: [], replacements: {} });
    },
    register(surface, entries) {
      const key = {};
      registrations.set(key, { surface, entries });
      publishMounted();
      return () => {
        registrations.delete(key);
        publishMounted();
      };
    },
    subscribe(listener) {
      return source.subscribe?.(listener) ?? (() => {});
    },
    watchSource: sourceReads.watch,
    observeSource: sourceReads.observe,
    notifyError(error) {
      source.notifyError?.(error);
    },
    toast(message, variant) {
      source.toast?.(message, variant);
    },
    openSession(address) {
      return source.openSession?.(address);
    },
    async command(name, params = {}, local = {}) {
      const handler = Object.hasOwn(local, name)
        ? local[name]
        : Object.hasOwn(source.commands ?? {}, name)
          ? source.commands[name]
          : null;
      if (typeof handler !== 'function')
        throw new Error(`UI action unavailable: ${name}`);
      if (!params || typeof params !== 'object' || Array.isArray(params))
        throw new Error('UI action parameters must be an object');
      return (await handler(JSON.parse(JSON.stringify(params)))) ?? {};
    },
    connect() {
      connected = true;
      if (!ready) void refresh();
      const unsubscribe = source.subscribe?.((change) => {
        if (
          change.owner == null &&
          (!change.change?.resource || change.change.resource === 'settings')
        )
          void refresh();
      });
      const changed = (event) => {
        if (
          (event.key === STORAGE_KEY || event.key === null) &&
          baseline === null &&
          layouts === null
        )
          legacy = readLayouts();
      };
      window.addEventListener('storage', changed);
      return () => {
        connected = false;
        ++readRevision;
        unsubscribe?.();
        window.removeEventListener('storage', changed);
      };
    },
    context: contributionContext,
  };
}

export const provideContributions = (value) => setContext(CONTEXT, value);
export const useContributions = () => getContext(CONTEXT) ?? null;
