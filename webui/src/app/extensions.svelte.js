import { listExtensionPages } from '$lib/api.js';
import { onMount, tick } from 'svelte';
import { t } from '$lib/i18n.js';

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
  let extensionInvalidationQueued = false;

  // Whether descriptors were requested before. The first load finds every
  // subscriber holding what it read when it mounted; each later load follows
  // a reconnect or an Extension layer change.
  let extensionPagesRequested = false;

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
      icon: page.icon,
      section: 'main',
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
    if (scope.resource === 'page_badges') {
      void loadExtensionPages({ invalidate: false });
      return;
    }
    invalidate(scope.owner, {
      resource: scope.resource,
      ids: scope.ids,
      revision: scope.revision,
    });
  }

  // Page descriptors load on every `connection_ready`, the first included,
  // and after an Extension reload. At most one request runs at once, with one
  // follow-up request coalescing any burst. This fetches descriptors only; it
  // never repeats page mutations.
  const loadExtensionPages = async ({
    invalidate: shouldInvalidate = true,
  } = {}) => {
    extensionInvalidationQueued ||= shouldInvalidate;
    if (extensionPagesLoadInFlight) {
      extensionPagesRefreshQueued = true;
      return extensionPagesLoadInFlight;
    }
    const load = async () => {
      let updated = false;
      do {
        extensionPagesRefreshQueued = false;
        const invalidateSubscribers = extensionInvalidationQueued;
        extensionInvalidationQueued = false;
        const reload = extensionPagesRequested;
        extensionPagesRequested = true;
        try {
          const result = await listExtensionPages();
          extensionPages = Array.isArray(result?.pages) ? result.pages : [];
          updated = true;
          context.onPagesLoaded?.();
        } catch {
          // Keep the last valid descriptors while a transient RPC error clears.
        }
        if (reload && invalidateSubscribers) {
          // A replaced descriptor reloads its page first. Independently of
          // catalog success, retained pages and the other Extension surfaces
          // must catch up after a reconnect or an Extension reload.
          await tick();
          invalidate(null);
        }
      } while (extensionPagesRefreshQueued);
      return updated;
    };
    extensionPagesLoadInFlight = load().finally(() => {
      extensionPagesLoadInFlight = null;
    });
    return extensionPagesLoadInFlight;
  };

  const pageIndicators = $derived(
    Object.fromEntries(
      extensionPages
        .filter((page) => Number.isInteger(page.badge) && page.badge > 0)
        .map((page) => [
          page.route,
          {
            count: page.badge,
            tone: 'unread',
            label: t('extensions.page.attentionCount', { count: page.badge }),
          },
        ]),
    ),
  );

  function notify(notification) {
    if (
      typeof notification?.extension !== 'string' ||
      typeof notification.page_id !== 'string' ||
      typeof notification.title !== 'string' ||
      notification.title.length > 200 ||
      typeof notification.body !== 'string' ||
      notification.body.length > 2000 ||
      typeof notification.route !== 'string' ||
      notification.route.length > 1024 ||
      (notification.key != null &&
        (typeof notification.key !== 'string' || notification.key.length > 128))
    )
      return;
    const route = notification.route;
    if (
      route &&
      (!route.startsWith('/') ||
        /[\\?#%\p{C}]/u.test(route) ||
        route
          .slice(1)
          .split('/')
          .some((part) => !part || part === '.' || part === '..'))
    )
      return;
    let toastId;
    toastId = context.showToast({
      title: notification.title,
      message: notification.body,
      variant: 'info',
      key:
        notification.key == null
          ? undefined
          : JSON.stringify([
              'extension',
              notification.extension,
              notification.key,
            ]),
      actions: [
        {
          label: t('extensions.page.open'),
          onClick: async () => {
            let page = extensionPages.find(
              (page) =>
                page.extension === notification.extension &&
                page.page === notification.page_id,
            );
            if (!page) {
              await loadExtensionPages({ invalidate: false });
              page = extensionPages.find(
                (page) =>
                  page.extension === notification.extension &&
                  page.page === notification.page_id,
              );
            }
            if (page) {
              context.openPage(
                page.route,
                route ? route.slice(1).split('/') : [],
              );
              context.dismissToast(toastId);
            }
          },
        },
      ],
    });
  }

  onMount(() => {
    refreshExtensionPageTheme();
    const themeObserver = new MutationObserver(refreshExtensionPageTheme);
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['class', 'style'],
    });
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
    notify,
    get pageIndicators() {
      return pageIndicators;
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
