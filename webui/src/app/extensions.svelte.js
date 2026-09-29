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

  // Every WebUI surface that shows Extension data subscribes here: the open
  // Extension page (`ExtensionPage.svelte`), the pending-input requests
  // (`ExtensionRequests.svelte`) and the Computer Use control
  // (`ComputerUseControl.svelte`). Each listener filters what concerns it.
  const invalidationListeners = [];

  let invalidationRevision = 0;

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

  // `owner` null means anything Extension-backed can have changed, so every
  // subscriber refreshes everything it shows. An owner names the Extension
  // whose data changed, and `change` the records (`{resource, ids,
  // revision}` from `ExtensionHost.publish_change`).
  function invalidate(owner, change = null) {
    const invalidation = Object.freeze({
      owner,
      change,
      revision: ++invalidationRevision,
    });
    for (const listener of [...invalidationListeners]) listener(invalidation);
  }

  function subscribeInvalidations(listener) {
    invalidationListeners.push(listener);
    return () => {
      const index = invalidationListeners.indexOf(listener);
      if (index >= 0) invalidationListeners.splice(index, 1);
    };
  }

  // A scoped Extension change: data the owner shows changed, while its page
  // descriptors did not, so they are not fetched again.
  function publishChange(scope) {
    invalidate(scope.owner, {
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
          // stays open is asked to refresh. Every other subscriber refreshes
          // too: this load follows a reconnect or an Extension layer change.
          await tick();
          invalidate(null);
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
    get extensionPages() {
      return extensionPages;
    },
    subscribeInvalidations,
    publishChange,
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
