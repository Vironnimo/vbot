import { getContext, setContext } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { contributionContext } from './contributions.js';

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
  let layouts = $state(readLayouts());
  let mounted = $state({});
  let storageError = $state(false);
  const registrations = new SvelteMap();

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
    const next = { ...layouts, [surface]: { ...layouts[surface], ...update } };
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      layouts = next;
      storageError = false;
      return true;
    } catch {
      storageError = true;
      return false;
    }
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
    get storageError() {
      return storageError;
    },
    get customized() {
      return Object.values(layouts).some(
        (layout) =>
          layout?.hidden?.length ||
          layout?.order?.length ||
          Object.values(layout?.replacements ?? {}).some(Boolean),
      );
    },
    get commandNames() {
      return Object.keys(source.commands ?? {});
    },
    layout: (surface) => layouts[surface] ?? {},
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
      const changed = (event) => {
        if (event.key === STORAGE_KEY || event.key === null)
          layouts = readLayouts();
      };
      window.addEventListener('storage', changed);
      return () => window.removeEventListener('storage', changed);
    },
    context: contributionContext,
  };
}

export const provideContributions = (value) => setContext(CONTEXT, value);
export const useContributions = () => getContext(CONTEXT) ?? null;
