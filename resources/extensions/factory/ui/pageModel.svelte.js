import { onMount } from 'svelte';
import { init, t } from '../../../../webui/src/lib/i18n.js';
import { createExtensionPageClient } from '../../../../webui/src/lib/extensionPageClient.js';
import { setApplicationTimeZone } from '../../../../webui/src/lib/dateTimePrefs.svelte.js';
import {
  configureModelFavorites,
  loadModelFavorites,
} from '../../../../webui/src/lib/modelFavorites.svelte.js';
import { failureText } from './presentation.js';

// This owner keeps server snapshots and the bridge lifetime. Editors own their
// drafts, so neither invalidations nor live state changes replace a draft.
export function createFactoryPageModel(bridgeClient) {
  const client = bridgeClient ?? createExtensionPageClient();
  let route = $state('');
  let catalog = $state({});
  let templates = $state([]);
  let library = $state([]);
  let selectedTemplate = $state(null);
  let templateDrafts = $state({});
  let cards = $state([]);
  let setups = $state([]);
  let detail = $state(null);
  let selectedSetup = $state(null);
  let setupEffectiveModels = $state({});
  let setupGeneration = $state(0);
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
  let setupRequest = 0;
  let templateRequest = 0;
  let libraryRequest = 0;
  let refreshPromise = null;
  let refreshAgain = false;
  let fullRefresh = false;
  let refreshSetups = false;
  let pickers = 0;
  let heldCatalog = null;

  const view = $derived(
    route.startsWith('/card/')
      ? 'card'
      : route.startsWith('/setups')
        ? 'setups'
        : route.startsWith('/library')
          ? 'library'
          : 'board',
  );
  const cardId = $derived(
    view === 'card' ? decodePart(route.split('/')[2]) : '',
  );
  const setupId = $derived(
    view === 'setups' ? decodePart(route.split('/')[2]) : '',
  );
  const templateId = $derived(
    view === 'library' ? decodePart(route.split('/')[2]) : '',
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

  async function loadSetups() {
    const result = await call('setup.list');
    if (!disposed) setups = result.setups ?? [];
  }

  async function loadLibrary() {
    const request = ++libraryRequest;
    const entries = [];
    let after = '';
    do {
      const result = await call('library.list', {
        include_archived: true,
        limit: 100,
        ...(after ? { after } : {}),
      });
      entries.push(...(result.templates ?? []));
      after = result.next_cursor ?? '';
    } while (after && !disposed && request === libraryRequest);
    if (!disposed && request === libraryRequest) library = entries;
  }

  async function loadTemplate() {
    const id = templateId;
    if (!id || id === 'new') return;
    const request = ++templateRequest;
    const result = await call('library.get', { id });
    if (!disposed && request === templateRequest && id === templateId)
      selectedTemplate = result.template;
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

  async function loadSetup() {
    const id = setupId;
    if (!id) return;
    const request = ++setupRequest;
    const result = await call('setup.get', { project_id: id });
    if (disposed || request !== setupRequest || id !== setupId) return;
    selectedSetup = result.setup ?? null;
    setupEffectiveModels = result.effective_models ?? {};
  }

  // Coalesce invalidations, including a change received while reads run. A
  // mutation is never replayed by reconnect or refresh recovery.
  function refresh({ full = false, setupsChanged = false } = {}) {
    refreshAgain = true;
    fullRefresh ||= full;
    refreshSetups ||= full || setupsChanged;
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      while (refreshAgain && !disposed) {
        refreshAgain = false;
        const readCatalog = fullRefresh;
        const readSetups = refreshSetups;
        fullRefresh = false;
        refreshSetups = false;
        try {
          await Promise.all([
            loadBoard(),
            loadDetail(),
            ...(readSetups ? [loadSetups(), loadSetup()] : []),
            ...(readCatalog
              ? [loadCatalog(), loadLibrary(), loadTemplate()]
              : []),
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
      /^\/card\/[^/]+$/.test(next) ||
      /^\/setups(?:\/[^/]+)?$/.test(next) ||
      /^\/library(?:\/[^/]+)?$/.test(next);
    if (!valid) {
      await client.replaceRoute('');
      next = '';
    }
    const normalized = next === '/' ? '' : next;
    if (route === normalized && connected) return;
    const previousCard = cardId;
    const previousSetup = setupId;
    const previousTemplate = templateId;
    route = normalized;
    error = '';
    if (cardId !== previousCard) detail = null;
    if (setupId !== previousSetup) {
      selectedSetup = null;
      setupEffectiveModels = {};
    }
    if (templateId !== previousTemplate) selectedTemplate = null;
    // Projects and Agents change outside the page; setups read them fresh.
    if (
      load &&
      (normalized.startsWith('/setups') || normalized.startsWith('/library'))
    )
      void loadCatalog().catch(() => {});
    if (!load || (!cardId && !setupId && !templateId)) return;
    detailLoading = true;
    try {
      if (cardId) await loadDetail();
      if (setupId) await loadSetup();
      if (templateId) await loadTemplate();
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

  function acceptSavedSetup(setup, effectiveModels) {
    if (setupId === setup.project_id) selectedSetup = setup;
    if (setupId === setup.project_id && effectiveModels)
      setupEffectiveModels = effectiveModels;
    setups = [
      ...setups.filter((entry) => entry.project_id !== setup.project_id),
      setup,
    ];
  }

  function acceptSavedTemplate(template) {
    if (templateId === template.id) selectedTemplate = template;
    library = [
      ...library.filter((entry) => entry.id !== template.id),
      template,
    ];
  }

  async function mutate(operation, args) {
    if (pending) return null;
    pending = operation;
    error = '';
    try {
      const result = await call(operation, args);
      if (disposed) return null;
      if (result.card) acceptSavedCard(result.card);
      if (result.setup) acceptSavedSetup(result.setup, result.effective_models);
      if (operation === 'card.delete') await client.replaceRoute('');
      if (operation === 'setup.delete') {
        selectedSetup = null;
        setupEffectiveModels = {};
        setupGeneration += 1;
      }
      await refresh({ setupsChanged: operation.startsWith('setup.') });
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
    for (const [name, value] of Object.entries(next.theme ?? {}))
      document.documentElement.style.setProperty(
        name.startsWith('--') ? name : `--${name}`,
        value,
      );
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
      void refresh({
        full: !change || change?.resource === 'library',
        setupsChanged: change?.resource === 'setups',
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
    acceptSavedSetup,
    acceptSavedTemplate,
    loadLibrary,
    getTemplateDraft: (id) => templateDrafts[id],
    keepTemplateDraft: (id, draft) => {
      templateDrafts[id] = draft;
    },
    clearTemplateDraft: (id) => {
      delete templateDrafts[id];
    },
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
    get setupId() {
      return setupId;
    },
    get templateId() {
      return templateId;
    },
    get library() {
      return library;
    },
    get selectedTemplate() {
      return selectedTemplate;
    },
    get catalog() {
      return catalog;
    },
    get templates() {
      return templates;
    },
    get cards() {
      return cards;
    },
    get setups() {
      return setups;
    },
    get detail() {
      return detail;
    },
    get selectedSetup() {
      return selectedSetup;
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
    get setupEffectiveModels() {
      return setupEffectiveModels;
    },
    get setupGeneration() {
      return setupGeneration;
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
