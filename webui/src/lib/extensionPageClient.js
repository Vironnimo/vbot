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
      return;
    }
    if (!matchesContext(data)) return;
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
      for (const listener of invalidationListeners) listener(data);
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
      : request.reject(
          new Error(
            typeof data.error === 'string'
              ? data.error
              : 'Extension request failed',
          ),
        );
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
    replaceRoute: (route) => call('route.replace', { route }),
    toast: (message, variant = 'info') => call('toast', { message, variant }),
    subscribeRun: (groupId, runId, afterSequence = 0) =>
      call('run.subscribe', {
        group_id: groupId,
        run_id: runId,
        after_sequence: afterSequence,
      }),
    unsubscribeRun: (id) => call('run.unsubscribe', { id }),
    onContext(listener) {
      contextListeners.add(listener);
      return () => contextListeners.delete(listener);
    },
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
      rejectPending('Extension page was disposed');
      invalidationListeners.clear();
      contextListeners.clear();
      runEventListeners.clear();
      context = null;
    },
  };
}
