const BRIDGE_VERSION = 1;
const MAX_MESSAGE_BYTES = 64 * 1024;
const MAX_HOST_MESSAGE_BYTES = 8 * 1024 * 1024;
const MAX_REQUEST_ID_LENGTH = 128;
const REQUEST_TIMEOUT_MS = 30_000;

export function createExtensionPageClient({ target = window.parent } = {}) {
  let context = null;
  let nextId = 0;
  const pending = new Map();
  const invalidationListeners = new Set();
  const contextListeners = new Set();
  const runEventListeners = new Set();
  let autosaveParticipant = null;
  // The dialogs and menus the page shows over its content, the topmost last.
  const layers = [];
  let forwardsHistoryInput = false;

  function hostMessage(type, values = {}) {
    if (!context) return;
    target.postMessage(
      {
        type,
        version: BRIDGE_VERSION,
        nonce: context.nonce,
        epoch: context.epoch,
        descriptor: context.descriptor,
        ...values,
      },
      '*',
    );
  }

  function notifyAutosave() {
    hostMessage('vbot.extension.autosave.state', {
      pending: autosaveParticipant?.hasPending() === true,
    });
  }

  function notifyLayers() {
    hostMessage('vbot.extension.layers.state', { open: layers.length > 0 });
  }

  // While any layer is open, the app's Back/Forward close the topmost one
  // (the host sends `layers.close`) instead of leaving the page's place.
  function registerLayer(layer) {
    const entry = { close: () => layer?.close?.() };
    layers.push(entry);
    if (layers.length === 1) notifyLayers();
    return () => {
      const index = layers.indexOf(entry);
      if (index < 0) return;
      layers.splice(index, 1);
      if (layers.length === 0) notifyLayers();
    };
  }

  // Where the app moves on the Back/Forward keys and mouse buttons itself
  // (the Desktop app), the host asks the page to forward those pressed inside
  // it, since they never reach the app window. Same rules as the app: plain
  // Alt with the arrows, the browser keys, side buttons 3 and 4.
  function moveHistory(direction) {
    hostMessage('vbot.extension.history.move', { direction });
  }

  function onHistoryKey(event) {
    if (event.defaultPrevented) return;
    const plainAlt =
      event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey;
    const backwards =
      event.key === 'BrowserBack' || (plainAlt && event.key === 'ArrowLeft');
    const forwards =
      event.key === 'BrowserForward' ||
      (plainAlt && event.key === 'ArrowRight');
    if (!backwards && !forwards) return;
    event.preventDefault();
    moveHistory(backwards ? 'back' : 'forward');
  }

  // The WebView's own navigation for the side buttons is cancelled on both
  // press and release, so each click moves exactly once.
  function onHistoryButton(event) {
    if (event.button !== 3 && event.button !== 4) return;
    event.preventDefault();
    if (event.type === 'mouseup')
      moveHistory(event.button === 3 ? 'back' : 'forward');
  }

  function forwardHistoryInput(enabled) {
    if (enabled === forwardsHistoryInput) return;
    forwardsHistoryInput = enabled;
    const listen = enabled ? 'addEventListener' : 'removeEventListener';
    window[listen]('keydown', onHistoryKey);
    window[listen]('mousedown', onHistoryButton);
    window[listen]('mouseup', onHistoryButton);
  }

  function isPlainObject(value) {
    if (!value || typeof value !== 'object') return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function validMessage(value, maxBytes = MAX_MESSAGE_BYTES) {
    try {
      return (
        isPlainObject(value) &&
        new TextEncoder().encode(JSON.stringify(value)).byteLength <= maxBytes
      );
    } catch {
      return false;
    }
  }

  function matchesContext(data) {
    return (
      context &&
      data.version === BRIDGE_VERSION &&
      data.nonce === context.nonce &&
      data.epoch === context.epoch &&
      data.descriptor?.owner === context.descriptor.owner &&
      data.descriptor?.page === context.descriptor.page
    );
  }

  function rejectPending(message) {
    for (const request of pending.values()) request.reject(new Error(message));
    pending.clear();
  }

  // An error reply may carry the API error's `code` and, for a directory
  // listing, its `reason`; the rejected error keeps the shape of the app's
  // API errors (`code`, `details.data.reason`), so shared components such
  // as PathField read the failure the same way in a page.
  function replyError(data) {
    const error = new Error(
      typeof data.error === 'string' ? data.error : 'Extension request failed',
    );
    if (typeof data.code === 'string' && data.code) error.code = data.code;
    if (typeof data.reason === 'string' && data.reason)
      error.details = { data: { reason: data.reason } };
    return error;
  }

  // `change` names the records the page's own Extension changed; without it
  // the page refreshes everything it shows (reload, reconnect, recovery).
  function invalidation(data) {
    const change = data.change;
    const valid =
      isPlainObject(change) &&
      typeof change.resource === 'string' &&
      change.resource.length > 0 &&
      Array.isArray(change.ids) &&
      change.ids.every((id) => typeof id === 'string' && id.length > 0) &&
      Number.isInteger(change.revision) &&
      change.revision >= 0;
    return Object.freeze({
      reason: typeof data.reason === 'string' ? data.reason : null,
      change: valid
        ? Object.freeze({
            resource: change.resource,
            ids: Object.freeze([...change.ids]),
            revision: change.revision,
          })
        : null,
    });
  }

  const onMessage = (event) => {
    if (
      event.source !== target ||
      !validMessage(event.data, MAX_HOST_MESSAGE_BYTES)
    )
      return;
    const data = event.data;
    if (data.type === 'vbot.extension.init') {
      if (
        data.version !== BRIDGE_VERSION ||
        typeof data.nonce !== 'string' ||
        !data.nonce ||
        !isPlainObject(data.descriptor) ||
        typeof data.descriptor.owner !== 'string' ||
        typeof data.descriptor.page !== 'string' ||
        typeof data.epoch !== 'string'
      )
        return;
      rejectPending('Extension page was reloaded');
      context = {
        nonce: data.nonce,
        epoch: data.epoch,
        descriptor: data.descriptor,
        route: typeof data.route === 'string' ? data.route : '',
        theme: isPlainObject(data.theme) ? data.theme : {},
        locale: typeof data.locale === 'string' ? data.locale : 'en',
        timezone: typeof data.timezone === 'string' ? data.timezone : 'UTC',
      };
      forwardHistoryInput(data.forwardHistoryInput === true);
      target.postMessage(
        {
          type: 'vbot.extension.ready',
          version: BRIDGE_VERSION,
          nonce: context.nonce,
          epoch: context.epoch,
          descriptor: context.descriptor,
        },
        '*',
      );
      for (const listener of contextListeners) listener(context);
      if (autosaveParticipant) notifyAutosave();
      if (layers.length > 0) notifyLayers();
      return;
    }
    if (!matchesContext(data)) return;
    if (data.type === 'vbot.extension.layers.close') {
      layers.at(-1)?.close();
      return;
    }
    if (
      data.type === 'vbot.extension.autosave.flush' &&
      typeof data.id === 'string'
    ) {
      const captured = context;
      Promise.resolve()
        .then(() => autosaveParticipant?.flush() ?? true)
        .catch(() => false)
        .then((saved) => {
          if (
            context?.nonce !== captured.nonce ||
            context?.epoch !== captured.epoch
          )
            return;
          notifyAutosave();
          hostMessage('vbot.extension.autosave.result', {
            id: data.id,
            saved: saved === true,
          });
        });
      return;
    }
    // The user left while a flush runs: the editor stops holding the App's
    // transitions for its running write, and reports whether it still has
    // edits the next transition must wait for.
    if (data.type === 'vbot.extension.autosave.release') {
      if (!autosaveParticipant) return;
      autosaveParticipant.release?.();
      notifyAutosave();
      return;
    }
    if (data.type === 'vbot.extension.context') {
      context = {
        ...context,
        route: typeof data.route === 'string' ? data.route : context.route,
        theme: isPlainObject(data.theme) ? data.theme : context.theme,
        locale: typeof data.locale === 'string' ? data.locale : context.locale,
        timezone:
          typeof data.timezone === 'string' ? data.timezone : context.timezone,
      };
      for (const listener of contextListeners) listener(context);
      return;
    }
    if (data.type === 'vbot.extension.invalidate') {
      const value = invalidation(data);
      for (const listener of invalidationListeners) listener(value);
      return;
    }
    if (
      data.type === 'vbot.extension.stream' &&
      typeof data.id === 'string' &&
      isPlainObject(data.event)
    ) {
      for (const listener of runEventListeners) listener(data.id, data.event);
      return;
    }
    if (
      (data.type !== 'vbot.extension.result' &&
        data.type !== 'vbot.extension.error') ||
      typeof data.id !== 'string'
    )
      return;
    const request = pending.get(data.id);
    if (!request) return;
    pending.delete(data.id);
    data.type === 'vbot.extension.result'
      ? request.resolve(data.result)
      : request.reject(replyError(data));
  };
  window.addEventListener('message', onMessage);

  function call(method, params = {}) {
    if (!context)
      return Promise.reject(new Error('Extension host is unavailable'));
    const id = String(++nextId);
    if (id.length > MAX_REQUEST_ID_LENGTH || !isPlainObject(params))
      return Promise.reject(new Error('Extension request is invalid'));
    const message = {
      type: 'vbot.extension.call',
      version: BRIDGE_VERSION,
      nonce: context.nonce,
      epoch: context.epoch,
      descriptor: context.descriptor,
      id,
      method,
      params,
    };
    if (!validMessage(message))
      return Promise.reject(new Error('Extension request is too large'));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error('Extension request timed out. Refresh to try again.'));
      }, REQUEST_TIMEOUT_MS);
      pending.set(id, {
        resolve: (result) => {
          clearTimeout(timer);
          resolve(result);
        },
        reject: (error) => {
          clearTimeout(timer);
          reject(error);
        },
      });
      try {
        target.postMessage(JSON.parse(JSON.stringify(message)), '*');
      } catch (error) {
        pending.get(id).reject(error);
        pending.delete(id);
      }
    });
  }

  return {
    get context() {
      return context;
    },
    operation: (operation, arguments_ = {}) =>
      call('operation', { operation, arguments: arguments_ }),
    readHistory: (groupId, participantId, query = {}) =>
      call('history.read', {
        group_id: groupId,
        participant_id: participantId,
        query,
      }),
    openLink: (url) => call('link.open', { url }),
    openMedia: (url) => call('media.open', { url }),
    // The route becomes part of the app's Back/Forward history: `pushRoute`
    // records a user step to another place of the page, `replaceRoute`
    // corrects the current entry (a default choice, a record that vanished).
    // A changed route comes back in the next context, like one that
    // Back/Forward changed.
    pushRoute: (route) => call('route.push', { route }),
    replaceRoute: (route) => call('route.replace', { route }),
    toast: (message, variant = 'info') => call('toast', { message, variant }),
    registerAutosave(participant) {
      if (autosaveParticipant)
        throw new Error('An autosave participant is already registered');
      autosaveParticipant = participant;
      notifyAutosave();
      return () => {
        if (autosaveParticipant !== participant) return;
        autosaveParticipant = null;
        notifyAutosave();
      };
    },
    notifyAutosave,
    // Registers an open dialog or menu: `{close}` closes it, and the returned
    // function releases it once it is closed. A Svelte page provides it to
    // its components with `provideNavigation({ registerLayer })` in its root,
    // so the shared Modal and ContextMenu register themselves.
    registerLayer,
    subscribeRun: (groupId, runId, afterSequence = 0) =>
      call('run.subscribe', {
        group_id: groupId,
        run_id: runId,
        after_sequence: afterSequence,
      }),
    unsubscribeRun: (id) => call('run.unsubscribe', { id }),
    cancelToolCall: (groupId, runId, toolCallId) =>
      call('run.cancel_tool', {
        group_id: groupId,
        run_id: runId,
        tool_call_id: toolCallId,
      }),
    // One directory of the vBot server's filesystem, with the params and
    // result of the app's `listServerDirectory`, so a page passes it to the
    // shared PathField as `listDirectory`. `path` null lists the places to
    // start from; a failure carries the listing `reason` (see replyError).
    listDirectory: ({
      path = null,
      root,
      include_files: includeFiles,
      prefix,
    } = {}) =>
      call('directory.list', {
        path,
        ...(root !== undefined ? { root } : {}),
        ...(includeFiles ? { include_files: true } : {}),
        ...(prefix ? { prefix } : {}),
      }),
    onContext(listener) {
      contextListeners.add(listener);
      return () => contextListeners.delete(listener);
    },
    // Listeners receive `{reason, change}`; `change` is `{resource, ids,
    // revision}` from the Extension's `publish_change`, or null.
    onInvalidation(listener) {
      invalidationListeners.add(listener);
      return () => invalidationListeners.delete(listener);
    },
    onRunEvent(listener) {
      runEventListeners.add(listener);
      return () => runEventListeners.delete(listener);
    },
    dispose() {
      window.removeEventListener('message', onMessage);
      forwardHistoryInput(false);
      rejectPending('Extension page was disposed');
      invalidationListeners.clear();
      contextListeners.clear();
      runEventListeners.clear();
      autosaveParticipant = null;
      layers.length = 0;
      context = null;
    },
  };
}
