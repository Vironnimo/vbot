import { listExtensionPages } from '$lib/api.js';
import { onMount, tick } from 'svelte';

export function createAppExtensions(context) {
  const EXTENSION_THEME_TOKENS = Object.freeze({
    background: '--bg',
    surface: '--surface',
    elevatedSurface: '--surface-2',
    border: '--border',
    text: '--text-hi',
    mutedText: '--text-med',
    accent: '--accent',
  });

  let extensionPages = $state([]);

  let extensionPagesLoadInFlight = null;

  let extensionPagesRefreshQueued = false;

  // The open Extension page subscribes here (`ExtensionPage.svelte`).
  const invalidationListeners = [];

  let invalidationRevision = 0;

  let extensionPageRoute = $state('');

  let extensionPageRouteView = $state('');

  let extensionPageTheme = $state({});

  const refreshExtensionPageTheme = () => {
    const styles = getComputedStyle(document.documentElement);
    Object.assign(extensionPageTheme, {
      mode: styles.colorScheme === 'light' ? 'light' : 'dark',
      ...Object.fromEntries(
        Object.entries(EXTENSION_THEME_TOKENS).map(([name, token]) => [
          name,
          styles.getPropertyValue(token).trim(),
        ]),
      ),
    });
  };

  const allNavigationItems = $derived([
    ...context.navigationItems,
    ...extensionPages.map((page) => ({
      id: page.route,
      label: () => page.title,
      section: 'work',
    })),
  ]);

  // `owner` null asks whichever page is open to refresh everything; an owner
  // limits the invalidation to that Extension's page and `change` names the
  // records it changed.
  function invalidatePages(owner, change = null) {
    const invalidation = Object.freeze({
      owner,
      change,
      revision: ++invalidationRevision,
    });
    for (const listener of [...invalidationListeners]) listener(invalidation);
  }

  function subscribePageInvalidations(listener) {
    invalidationListeners.push(listener);
    return () => {
      const index = invalidationListeners.indexOf(listener);
      if (index >= 0) invalidationListeners.splice(index, 1);
    };
  }

  // A scoped Extension change: data behind the owner's page changed, while
  // its descriptors did not, so they are not fetched again.
  function publishPageChange(scope) {
    invalidatePages(scope.owner, {
      resource: scope.resource,
      ids: scope.ids,
      revision: scope.revision,
    });
  }

  // Page descriptors may change after an Extension reload or reconnect. At
  // most one request runs at once, with one follow-up request coalescing any
  // burst. This fetches descriptors only; it never repeats page mutations.
  const loadExtensionPages = async () => {
    if (extensionPagesLoadInFlight) {
      extensionPagesRefreshQueued = true;
      return extensionPagesLoadInFlight;
    }
    const load = async () => {
      let updated = false;
      do {
        extensionPagesRefreshQueued = false;
        try {
          const result = await listExtensionPages();
          extensionPages = Array.isArray(result?.pages) ? result.pages : [];
          updated = true;
          context.onPagesLoaded?.();
          // A replaced descriptor reloads its page first; only a page that
          // stays open is asked to refresh.
          await tick();
          invalidatePages(null);
        } catch {
          // Keep the last valid descriptors while a transient RPC error clears.
        }
      } while (extensionPagesRefreshQueued);
      return updated;
    };
    extensionPagesLoadInFlight = load().finally(() => {
      extensionPagesLoadInFlight = null;
    });
    return extensionPagesLoadInFlight;
  };

  function syncRoute(activeViewId) {
    const nextView = activeViewId.startsWith('extension:') ? activeViewId : '';
    if (nextView !== extensionPageRouteView) {
      extensionPageRoute = '';
    }
    extensionPageRouteView = nextView;
  }

  onMount(() => {
    refreshExtensionPageTheme();
    const themeObserver = new MutationObserver(refreshExtensionPageTheme);
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['class', 'style'],
    });
    void loadExtensionPages();
    const onExtensionPage = (event) => {
      const target = event.detail;
      if (
        target?.kind === 'open_extension_page' &&
        extensionPages.some(
          (page) =>
            page.extension === target.extension &&
            page.page === target.page &&
            page.route === target.route,
        )
      ) {
        context.selectView(target.route);
      }
    };
    window.addEventListener('vbot-extension-page', onExtensionPage);

    return () => {
      themeObserver.disconnect();
      window.removeEventListener('vbot-extension-page', onExtensionPage);
    };
  });
  return {
    syncRoute,
    get extensionPages() {
      return extensionPages;
    },
    subscribePageInvalidations,
    publishPageChange,
    get extensionPageRoute() {
      return extensionPageRoute;
    },
    set extensionPageRoute(value) {
      extensionPageRoute = value;
    },
    get extensionPageTheme() {
      return extensionPageTheme;
    },
    get allNavigationItems() {
      return allNavigationItems;
    },
    get loadExtensionPages() {
      return loadExtensionPages;
    },
  };
}
