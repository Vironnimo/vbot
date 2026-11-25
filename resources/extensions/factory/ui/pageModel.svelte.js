import { onMount } from 'svelte';
import { init } from '@vbot/extension-sdk/lib/i18n.js';
import { createExtensionPageClient } from '@vbot/extension-sdk/client';
import { applyExtensionTheme } from '@vbot/extension-sdk/lib/extensionTheme.js';
import { setApplicationTimeZone } from '@vbot/extension-sdk/lib/dateTimePrefs.svelte.js';
import {
  configureModelFavorites,
  loadModelFavorites,
} from '@vbot/extension-sdk/lib/modelFavorites.svelte.js';
import { failureText } from './presentation.js';

export function createFactoryPageModel(bridgeClient) {
  const client = bridgeClient ?? createExtensionPageClient();
  let route = $state('');
  let catalog = $state({});
  let factories = $state([]);
  let factory = $state(null);
  let members = $state([]);
  let jobs = $state([]);
  let needsAttention = $state([]);
  let running = $state([]);
  let upcoming = $state([]);
  let recentRuns = $state([]);
  let selectedRun = $state(null);
  let loading = $state(true);
  let error = $state('');
  let refreshError = $state('');
  let pending = $state('');
  let connected = false;
  let disposed = false;
  let generation = 0;
  let refreshPromise = null;
  let refreshAgain = false;
  let fullRefresh = false;
  let pickers = 0;
  let heldCatalog = null;

  const parts = $derived(route.split('/').filter(Boolean).map(decodePart));
  const view = $derived(
    parts[0] === 'job-run'
      ? 'job-run'
      : parts[0] === 'factory'
        ? parts[2] || 'overview'
        : 'overview',
  );
  const runId = $derived(parts[0] === 'job-run' ? parts[1] || '' : '');
  const projectId = $derived(
    parts[0] === 'project'
      ? parts[1] || ''
      : factory?.project_id || catalog.projects?.[0]?.id || '',
  );
  const factoryId = $derived(
    parts[0] === 'factory'
      ? parts[1] || ''
      : selectedRun?.factory_id || factory?.id || '',
  );

  const call = (operation, args = {}) => client.operation(operation, args);

  function decodePart(part) {
    try {
      return decodeURIComponent(part ?? '');
    } catch {
      return '';
    }
  }

  function pickerOpen(open) {
    pickers = Math.max(0, pickers + (open ? 1 : -1));
    if (!pickers && heldCatalog) {
      catalog = heldCatalog;
      heldCatalog = null;
    }
  }

  async function loadCatalog() {
    const result = await call('catalog');
    if (disposed) return;
    const nextCatalog = {
      ...(result.catalog ?? result ?? {}),
      ...(result.triggers_available !== undefined
        ? { triggers_available: result.triggers_available }
        : {}),
    };
    if (pickers) heldCatalog = nextCatalog;
    else catalog = nextCatalog;
  }

  function refresh({ full = false } = {}) {
    refreshAgain = true;
    fullRefresh ||= full;
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      while (refreshAgain && !disposed) {
        refreshAgain = false;
        const readCatalog = fullRefresh;
        fullRefresh = false;
        const token = ++generation;
        const currentRoute = route;
        try {
          if (readCatalog || !catalog.projects) await loadCatalog();
          const listResult = await call('factory.list', { limit: 100 });
          const nextFactories = listResult.factories ?? [];

          let nextRun = null;
          if (parts[0] === 'job-run' && parts[1]) {
            const runResult = await call('job_run.get', { run_id: parts[1] });
            nextRun = runResult.run ?? null;
          }

          const targetFactoryId =
            parts[0] === 'factory'
              ? parts[1]
              : nextRun?.factory_id ||
                nextFactories.find(
                  (item) =>
                    item.state !== 'archived' &&
                    (parts[0] !== 'project' || item.project_id === parts[1]),
                )?.id ||
                '';

          let dashboard = null;
          if (targetFactoryId) {
            dashboard = await call('dashboard.get', {
              factory_id: targetFactoryId,
              recent_limit: 20,
              upcoming_limit: 10,
            });
          }

          if (disposed || currentRoute !== route || token !== generation) {
            refreshAgain = !disposed;
            continue;
          }

          factories = nextFactories;
          selectedRun = nextRun;
          if (dashboard) {
            factory = dashboard.factory ?? null;
            members = dashboard.members ?? [];
            jobs = dashboard.jobs ?? [];
            needsAttention = dashboard.needs_attention ?? [];
            running = dashboard.running ?? [];
            upcoming = dashboard.upcoming ?? [];
            recentRuns = dashboard.recent_runs ?? [];
          } else {
            factory = null;
            members = [];
            jobs = [];
            needsAttention = [];
            running = [];
            upcoming = [];
            recentRuns = [];
          }
          refreshError = '';
        } catch (cause) {
          if (!disposed && currentRoute === route) {
            if (
              cause?.code === 'not_found' ||
              cause?.code === 'factory_not_found'
            ) {
              await client.replaceRoute('');
            } else {
              refreshError = failureText(cause);
            }
          }
        } finally {
          if (!disposed && currentRoute === route) loading = false;
        }
      }
    })().finally(() => {
      refreshPromise = null;
    });
    return refreshPromise;
  }

  async function applyRoute(next, { initial = false } = {}) {
    const valid =
      next === '' ||
      next === '/' ||
      /^\/project\/[^/]+$/.test(next) ||
      /^\/factory\/[^/]+(?:\/(?:jobs|team|settings))?$/.test(next) ||
      /^\/job-run\/[^/]+$/.test(next);
    if (!valid) {
      await client.replaceRoute('');
      next = '';
    }
    const normalized = next === '/' ? '' : next;
    if (!initial && route === normalized) return;
    route = normalized;
    error = '';
    loading = true;
    await refresh();
  }

  async function navigate(next) {
    if (next === route) return;
    try {
      await client.pushRoute(next);
    } catch (cause) {
      if (!disposed) error = failureText(cause);
    }
  }

  async function mutate(operation, args = {}) {
    if (pending) return null;
    pending = operation;
    error = '';
    try {
      const result = await call(operation, args);
      if (disposed) return null;
      await refresh({
        full: ['factory.create', 'member.create', 'member.add'].includes(
          operation,
        ),
      });
      return result;
    } catch (cause) {
      if (!disposed) error = failureText(cause);
      return null;
    } finally {
      if (!disposed) pending = '';
    }
  }

  function acceptFactory(record) {
    if (record && factory?.id === record.id) {
      factory = { ...factory, ...record };
    }
  }

  function acceptMember(record) {
    if (record) {
      members = members.map((item) =>
        item.id === record.id ? { ...item, ...record } : item,
      );
    }
  }

  async function openAgentProfile(agentId, incarnation, targetFactoryId) {
    if (!agentId || !incarnation) return;
    try {
      await client.openAgent(agentId, {
        expectedIncarnation: incarnation,
        ...(targetFactoryId ? { relatedScope: targetFactoryId } : {}),
      });
      await refresh({ full: true });
    } catch (cause) {
      if (!disposed) error = failureText(cause);
    }
  }

  onMount(() => {
    configureModelFavorites({
      read: () => client.readModelFavorites(),
      write: (modelName, favorite) =>
        client.setModelFavorite(modelName, favorite),
    });
    const offContext = client.onContext((context) => {
      applyExtensionTheme(context.theme);
      setApplicationTimeZone(context.timezone ?? null);
      void init({ locale: context.locale });
      const initialLoad = !connected;
      connected = true;
      void applyRoute(context.route ?? '', { initial: initialLoad });
      if (initialLoad) void loadModelFavorites().catch(() => {});
    });
    const offInvalidation = client.onInvalidation(({ change } = {}) => {
      if (
        !change ||
        [
          'factories',
          'members',
          'jobs',
          'job_runs',
          'topics',
          'agents',
          'permissions_approvals',
        ].includes(change.resource)
      ) {
        void refresh({ full: !change || change.resource === 'agents' });
      }
    });
    return () => {
      disposed = true;
      generation++;
      offContext();
      offInvalidation();
      if (!bridgeClient) client.dispose();
    };
  });

  return {
    client,
    call,
    mutate,
    refresh,
    navigate,
    pickerOpen,
    openAgentProfile,
    acceptFactory,
    acceptMember,
    get route() {
      return route;
    },
    get view() {
      return view;
    },
    get runId() {
      return runId;
    },
    get projectId() {
      return projectId;
    },
    get factoryId() {
      return factoryId;
    },
    get catalog() {
      return catalog;
    },
    get factories() {
      return factories;
    },
    get factory() {
      return factory;
    },
    get members() {
      return members;
    },
    get jobs() {
      return jobs;
    },
    get needsAttention() {
      return needsAttention;
    },
    get running() {
      return running;
    },
    get upcoming() {
      return upcoming;
    },
    get recentRuns() {
      return recentRuns;
    },
    get selectedRun() {
      return selectedRun;
    },
    get loading() {
      return loading;
    },
    get error() {
      return error;
    },
    get refreshError() {
      return refreshError;
    },
    get pending() {
      return pending;
    },
  };
}
