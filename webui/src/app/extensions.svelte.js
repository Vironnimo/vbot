import { listExtensionPages } from '$lib/api.js';
import { onMount } from 'svelte';

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

  let extensionPageInvalidationRevision = $state(0);

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
      labelKey: '',
      labelFallback: page.title,
      section: 'work',
    })),
  ]);

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
          extensionPageInvalidationRevision += 1;
          updated = true;
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
    get extensionPageInvalidationRevision() {
      return extensionPageInvalidationRevision;
    },
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
