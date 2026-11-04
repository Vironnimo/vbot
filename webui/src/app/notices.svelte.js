import { SvelteSet } from 'svelte/reactivity';

import {
  dismissUpdateNotice,
  updateNoticeDismissed,
} from '$lib/applicationUpdate.js';
import { t } from '$lib/i18n.js';

// App-wide states for the toast stack, the only place for them: nothing
// app-wide may appear above the view (user decision 2026-11-04). `context`
// supplies the live state and the actions.
export function createAppNotices(context) {
  // Closed notices that return at the next connection.
  const closed = new SvelteSet();
  // Versions dismissed in this page; storage keeps the newest across reloads.
  let dismissedUpdate = $state('');

  const closable = (id) => () => closed.add(id);

  const restartNotice = () => {
    const { restartBusy: busy, restartFailed: failed } = context;
    // A failure is news, so it shows even after the pending notice was closed.
    const id = failed ? 'desktop-restart-failed' : 'desktop-restart';
    if (closed.has(id)) return null;
    let label = t('app.desktopRestart.restart');
    if (busy) label = t('app.desktopRestart.restarting');
    else if (failed) label = t('common.retry');
    return {
      id,
      variant: 'info',
      message:
        failed && !busy
          ? t('app.desktopRestart.failed')
          : t('app.desktopRestart.pending'),
      actions: [
        { label, loading: busy, variant: 'primary', onClick: context.restart },
      ],
      onDismiss: busy ? undefined : closable(id),
    };
  };

  const reloadNotice = () => {
    const id = 'webui-outdated';
    if (!context.webuiOutdated || closed.has(id)) return null;
    return {
      id,
      variant: 'info',
      message: t('app.webuiOutdated'),
      actions: [
        {
          label: t('app.webuiOutdatedReload'),
          variant: 'primary',
          onClick: () => window.location.reload(),
        },
      ],
      onDismiss: closable(id),
    };
  };

  const updateNotice = () => {
    const update = context.applicationUpdate;
    if (
      !update ||
      update.version_id === dismissedUpdate ||
      updateNoticeDismissed(update.version_id)
    ) {
      return null;
    }
    return {
      id: 'application-update',
      variant: 'info',
      message: t('app.updateAvailable', { version: update.label }),
      onDismiss: () => {
        dismissUpdateNotice(update.version_id);
        dismissedUpdate = update.version_id;
      },
    };
  };

  const list = $derived(
    [
      // A Desktop restart also loads the new WebUI, so it replaces the reload.
      context.restartOffered ? restartNotice() : reloadNotice(),
      updateNotice(),
      context.extensionNotice,
    ].filter(Boolean),
  );

  return {
    get list() {
      return list;
    },
    connectionOpened: () => closed.clear(),
  };
}
