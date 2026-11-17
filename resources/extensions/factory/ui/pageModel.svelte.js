import { onMount } from 'svelte';
import { init, t } from '@vbot/extension-sdk/lib/i18n.js';
import { createExtensionPageClient } from '@vbot/extension-sdk/client';
import { applyExtensionTheme } from '@vbot/extension-sdk/lib/extensionTheme.js';
import { setApplicationTimeZone } from '@vbot/extension-sdk/lib/dateTimePrefs.svelte.js';
import {
  configureModelFavorites,
  loadModelFavorites,
} from '@vbot/extension-sdk/lib/modelFavorites.svelte.js';
import { failureText } from './presentation.js';

// This owner keeps server snapshots and the bridge lifetime. Editors own their
// drafts, so neither invalidations nor live state changes replace a draft.
export function createFactoryPageModel(bridgeClient) {
  const client = bridgeClient ?? createExtensionPageClient();
  let route = $state('');
  let catalog = $state({});
  let templates = $state([]);
  let triggersAvailable = $state(true);
  let cards = $state([]);
  let workflows = $state([]);
  let detail = $state(null);
  let selectedWorkflow = $state(null);
  let workflowEffectiveModels = $state({});
  let newTemplate = $state('');
  let projectFilter = $state('');
  let showDiscarded = $state(false);
  let wip = $state({ running: 0, limit: 0 });
  let loading = $state(true);
  let detailLoading = $state(false);
  let error = $state('');
  let refreshError = $state('');
  let pending = $state('');
  let connected = false;
  let disposed = false;
  let boardRequest = 0;
  let detailRequest = 0;
  let workflowRequest = 0;
  let refreshPromise = null;
  let refreshAgain = false;
  let fullRefresh = false;
  let refreshWorkflows = false;
  let pickers = 0;
  let heldCatalog = null;

  const view = $derived(
    route.startsWith('/card/')
      ? 'card'
      : route.startsWith('/workflows')
        ? 'workflows'
        : route === '/cards'
          ? 'board'
          : 'native',
  );
  const cardId = $derived(
    view === 'card' ? decodePart(route.split('/')[2]) : '',
  );
  // A saved Workflow's id, or 'new' while the user creates one.
  const workflowId = $derived(
    view === 'workflows' ? decodePart(route.split('/')[2]) : '',
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
    templates = result.templates ?? [];
    triggersAvailable = result.triggers_available !== false;
    if (pickers) heldCatalog = result.catalog ?? {};
    else catalog = result.catalog ?? {};
  }

  async function loadBoard() {
    const request = ++boardRequest;
    const filter = projectFilter;
    const result = await call(
      'board.get',
      filter ? { project_id: filter } : {},
    );
    if (disposed || request !== boardRequest || filter !== projectFilter)
      return;
    cards = result.cards ?? [];
    wip = result.wip ?? { running: 0, limit: 0 };
  }

  async function loadWorkflows() {
    const result = await call('workflow.list');
    if (!disposed) workflows = result.workflows ?? [];
  }

  async function loadDetail() {
    const id = cardId;
    if (!id) return;
    const request = ++detailRequest;
    let result;
    try {
      result = await call('card.get', { id });
    } catch (cause) {
      if (
        cause?.code === 'card_not_found' &&
        !disposed &&
        request === detailRequest &&
        id === cardId
      ) {
        detail = null;
        await client.replaceRoute('');
        return;
      }
      throw cause;
    }
    if (disposed || request !== detailRequest || id !== cardId) return;
    if (!detail || (result.card?.revision ?? 0) >= (detail.card?.revision ?? 0))
      detail = result;
  }

  async function loadWorkflow() {
    const id = workflowId;
    if (!id || id === 'new') return;
    const request = ++workflowRequest;
    let result;
    try {
      result = await call('workflow.get', { id });
    } catch (cause) {
      if (
        cause?.code === 'workflow_not_found' &&
        !disposed &&
        request === workflowRequest &&
        id === workflowId
      ) {
        selectedWorkflow = null;
        await client.replaceRoute('/workflows');
        return;
      }
      throw cause;
    }
    if (disposed || request !== workflowRequest || id !== workflowId) return;
    selectedWorkflow = result.workflow ?? null;
    workflowEffectiveModels = result.effective_models ?? {};
  }

  // Coalesce invalidations, including a change received while reads run. A
  // mutation is never replayed by reconnect or refresh recovery.
  function refresh({ full = false, workflowsChanged = false } = {}) {
    refreshAgain = true;
    fullRefresh ||= full;
    refreshWorkflows ||= full || workflowsChanged;
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      while (refreshAgain && !disposed) {
        refreshAgain = false;
        const readCatalog = fullRefresh;
        const readWorkflows = refreshWorkflows;
        fullRefresh = false;
        refreshWorkflows = false;
        try {
          await Promise.all([
            loadBoard(),
            loadDetail(),
            ...(readWorkflows ? [loadWorkflows(), loadWorkflow()] : []),
            ...(readCatalog ? [loadCatalog()] : []),
          ]);
          if (!disposed) refreshError = '';
        } catch (cause) {
          if (!disposed) refreshError = failureText(cause);
        } finally {
          if (!disposed) loading = false;
        }
      }
    })().finally(() => {
      refreshPromise = null;
    });
    return refreshPromise;
  }

  async function applyRoute(next, { load = true } = {}) {
    const valid =
      next === '' ||
      next === '/' ||
      next === '/cards' ||
      /^\/factory\/[^/]+(?:\/(?:team|discussions|settings))?$/.test(next) ||
      /^\/(?:task|topic)\/[^/]+$/.test(next) ||
      /^\/card\/[^/]+$/.test(next) ||
      /^\/workflows(?:\/[^/]+)?$/.test(next);
    if (!valid) {
      await client.replaceRoute('');
      next = '';
    }
    const normalized = next === '/' ? '' : next;
    if (route === normalized && connected) return;
    const previousCard = cardId;
    const previousWorkflow = workflowId;
    route = normalized;
    error = '';
    if (cardId !== previousCard) detail = null;
    if (workflowId !== previousWorkflow) {
      selectedWorkflow = null;
      workflowEffectiveModels = {};
    }
    // Projects and Agents change outside the page; the editor reads them fresh.
    if (load && normalized.startsWith('/workflows'))
      void loadCatalog().catch(() => {});
    if (!load || (!cardId && (!workflowId || workflowId === 'new'))) return;
    detailLoading = true;
    try {
      if (cardId) await loadDetail();
      if (workflowId) await loadWorkflow();
    } catch (cause) {
      if (!disposed && route === normalized) error = failureText(cause);
    } finally {
      if (!disposed && route === normalized) detailLoading = false;
    }
  }

  async function navigate(next) {
    if (next === route) return;
    try {
      await client.pushRoute(next);
    } catch (cause) {
      if (!disposed) error = failureText(cause);
    }
  }

  async function filterProject(id) {
    projectFilter = id;
    try {
      await loadBoard();
    } catch (cause) {
      if (!disposed) error = failureText(cause);
    }
  }

  function acceptSavedCard(card) {
    if (
      detail?.card.id === card.id &&
      (card.revision ?? 0) >= (detail.card.revision ?? 0)
    )
      detail = { ...detail, card: { ...detail.card, ...card } };
    cards = cards.map((entry) => (entry.id === card.id ? card : entry));
  }

  function acceptSavedWorkflow(workflow, effectiveModels) {
    if (workflowId === workflow.id) {
      selectedWorkflow = workflow;
      if (effectiveModels) workflowEffectiveModels = effectiveModels;
    }
    workflows = workflows.some((entry) => entry.id === workflow.id)
      ? workflows.map((entry) => (entry.id === workflow.id ? workflow : entry))
      : [...workflows, workflow];
  }

  async function mutate(operation, args) {
    if (pending) return null;
    pending = operation;
    error = '';
    try {
      const result = await call(operation, args);
      if (disposed) return null;
      if (result.card) acceptSavedCard(result.card);
      if (result.workflow)
        acceptSavedWorkflow(result.workflow, result.effective_models);
      if (operation === 'card.delete') await client.replaceRoute('/cards');
      if (operation === 'workflow.delete') {
        workflows = workflows.filter((entry) => entry.id !== args.id);
        selectedWorkflow = null;
        await client.replaceRoute('/workflows');
      }
      await refresh({ workflowsChanged: operation.startsWith('workflow.') });
      return result;
    } catch (cause) {
      if (!disposed) error = failureText(cause);
      return null;
    } finally {
      if (!disposed) pending = '';
    }
  }

  async function openSession(address) {
    try {
      await client.openSession(address);
    } catch (cause) {
      if (!disposed) error = failureText(cause);
    }
  }

  function context(next) {
    document.documentElement.lang = init(next.locale);
    setApplicationTimeZone(next.timezone);
    applyExtensionTheme(next.theme);
    if (!connected) {
      connected = true;
      refreshError = '';
      void applyRoute(next.route ?? '', { load: false })
        .then(() => refresh({ full: true }))
        .catch((cause) => {
          if (!disposed) error = failureText(cause);
        });
      void loadModelFavorites();
    } else if ((next.route ?? '') !== route) void applyRoute(next.route ?? '');
  }

  onMount(() => {
    configureModelFavorites({
      load: () => client.readModelFavorites(),
      save: (model, favorite) => client.setModelFavorite(model, favorite),
      onError: (message) => {
        error = message;
      },
    });
    const timeout = setTimeout(() => {
      if (!connected) {
        loading = false;
        refreshError = t('factory.hostUnavailable');
      }
    }, 10_000);
    const offContext = client.onContext((next) => {
      clearTimeout(timeout);
      context(next);
    });
    const offInvalidation = client.onInvalidation(({ change } = {}) => {
      if (
        change &&
        ['factories', 'members', 'tasks', 'topics', 'page_badges'].includes(
          change.resource,
        )
      )
        return;
      void refresh({
        full: !change,
        workflowsChanged: change?.resource === 'workflows',
      });
    });
    if (client.context) {
      clearTimeout(timeout);
      context(client.context);
    }
    return () => {
      disposed = true;
      clearTimeout(timeout);
      offContext();
      offInvalidation();
      configureModelFavorites(null);
      if (!bridgeClient) client.dispose();
    };
  });

  return {
    client,
    call,
    navigate,
    refresh,
    mutate,
    filterProject,
    pickerOpen,
    acceptSavedCard,
    acceptSavedWorkflow,
    openSession,
    get route() {
      return route;
    },
    get view() {
      return view;
    },
    get cardId() {
      return cardId;
    },
    get workflowId() {
      return workflowId;
    },
    get catalog() {
      return catalog;
    },
    get templates() {
      return templates;
    },
    get triggersAvailable() {
      return triggersAvailable;
    },
    // The template a Workflow created on /workflows/new starts from.
    get newTemplate() {
      return newTemplate;
    },
    set newTemplate(value) {
      newTemplate = value;
    },
    get cards() {
      return cards;
    },
    get workflows() {
      return workflows;
    },
    get detail() {
      return detail;
    },
    get selectedWorkflow() {
      return selectedWorkflow;
    },
    get projectFilter() {
      return projectFilter;
    },
    get showDiscarded() {
      return showDiscarded;
    },
    set showDiscarded(value) {
      showDiscarded = value;
    },
    get workflowEffectiveModels() {
      return workflowEffectiveModels;
    },
    get wip() {
      return wip;
    },
    get loading() {
      return loading;
    },
    get detailLoading() {
      return detailLoading;
    },
    get error() {
      return error;
    },
    set error(value) {
      error = value;
    },
    get refreshError() {
      return refreshError;
    },
    get pending() {
      return pending;
    },
  };
}
