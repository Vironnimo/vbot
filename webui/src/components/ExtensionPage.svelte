<script>
  import { onDestroy, onMount } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';
  import { t } from '$lib/i18n.js';
  import { createExtensionRunStream } from '$lib/extensionRunStream.js';
  import { noteExtensionPageInvalidation } from '$lib/clientMetrics.js';
  import Banner from './ui/Banner.svelte';
  import { useAutosaveContext } from '$lib/autosave.js';
  import {
    createStandaloneNavigation,
    useNavigation,
  } from '$lib/navigation.svelte.js';
  import {
    invokeExtensionPageOperation,
    openExtensionPageRun,
    cancelExtensionPageToolCall,
    readExtensionPageHistory,
    subscribeRunEvents,
  } from '$lib/api.js';

  const BRIDGE_VERSION = 1;
  const MAX_MESSAGE_BYTES = 64 * 1024;
  const MAX_HOST_MESSAGE_BYTES = 8 * 1024 * 1024;
  const MAX_REQUEST_ID_LENGTH = 128;
  const METHODS = new Set([
    'operation',
    'history.read',
    'link.open',
    'media.open',
    'route.push',
    'route.replace',
    'toast',
    'run.subscribe',
    'run.unsubscribe',
    'run.cancel_tool',
  ]);

  let {
    descriptor,
    navigation = createStandaloneNavigation(),
    theme = {},
    locale = 'en',
    timezone = 'UTC',
    subscribeInvalidations = null,
    onToast = () => {},
  } = $props();
  // The page's route spells its navigation place (`/swarms/<id>`), or is ''
  // at the page's start. Back/Forward and the main navigation change the
  // place; the page changes it with `route.push` (a step) or `route.replace`
  // (a correction of the current entry).
  const route = $derived(
    navigation.place.length ? `/${navigation.place.join('/')}` : '',
  );
  const placeFromRoute = (value) => value.split('/').filter(Boolean);
  // The App navigator: its Back/Forward first close registered layers, and
  // the page's frame forwards the Back/Forward input it handles.
  const shell = useNavigation();
  let frame = $state.raw(null);
  let frameContext = $state.raw(null);
  let disposed = false;
  let observedDescriptor = '';
  const runSubscriptions = new SvelteMap();
  const unregisterAutosave = useAutosaveContext().register({
    hasPending: () => frameContext?.autosavePending === true,
    flush: flushAutosave,
    release: releaseAutosave,
  });

  // Asks the current frame to save its editor; transitions share the flush
  // that runs. Every flush sent stays answerable until its result, a reload
  // or its deadline.
  function flushAutosave() {
    const context = frameContext;
    if (!context?.autosavePending) return Promise.resolve(true);
    if (context.autosaveFlush) return context.autosaveFlush.promise;
    const id = newNonce();
    let finish;
    const promise = new Promise((resolve) => {
      const timer = setTimeout(() => finish(false), 35_000);
      finish = (saved) => {
        clearTimeout(timer);
        context.autosaveFlushes.delete(id);
        if (context.autosaveFlush?.id === id) context.autosaveFlush = null;
        resolve(saved === true && frameContext === context);
      };
    });
    context.autosaveFlush = { id, promise, finish };
    context.autosaveFlushes.set(id, finish);
    post(context, {
      ...contextPayload(context),
      type: 'vbot.extension.autosave.flush',
      id,
    });
    return promise;
  }

  // The user left while the frame saves: later transitions no longer share
  // the running flush but ask the frame again, and the frame's editor stops
  // holding them for its running write unless the draft changed after that
  // write started.
  function releaseAutosave() {
    const context = frameContext;
    if (!context) return;
    context.autosaveFlush = null;
    if (!context.autosavePending) return;
    post(context, {
      ...contextPayload(context),
      type: 'vbot.extension.autosave.release',
    });
  }

  function descriptorIdentity(value) {
    if (!value || typeof value !== 'object') return '';
    return [value.extension, value.page, value.epoch, value.entry_url].join(
      '\u0000',
    );
  }

  function newNonce() {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (value) =>
      value.toString(16).padStart(2, '0'),
    ).join('');
  }

  function isPlainObject(value) {
    if (!value || typeof value !== 'object') return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function valid(value, maxBytes = MAX_MESSAGE_BYTES) {
    try {
      return (
        isPlainObject(value) &&
        new TextEncoder().encode(JSON.stringify(value)).byteLength <= maxBytes
      );
    } catch {
      return false;
    }
  }

  function captureDescriptor(value) {
    if (
      !value ||
      typeof value.extension !== 'string' ||
      typeof value.page !== 'string' ||
      typeof value.epoch !== 'string'
    )
      return null;
    return { owner: value.extension, page: value.page, epoch: value.epoch };
  }

  function matchesContext(data, context) {
    return (
      data?.version === BRIDGE_VERSION &&
      data.nonce === context.nonce &&
      data.epoch === context.descriptor.epoch &&
      data.descriptor?.owner === context.descriptor.owner &&
      data.descriptor?.page === context.descriptor.page
    );
  }

  function post(context, data) {
    if (disposed || frameContext !== context || !context.window) return;
    // Replies include catalogs and canonical history, not just small commands.
    // Never drop a reply silently: onMessage returns this failure to its caller.
    if (!valid(data, MAX_HOST_MESSAGE_BYTES))
      throw new Error('Extension response is too large');
    context.window.postMessage(JSON.parse(JSON.stringify(data)), '*');
  }

  function projectedFileUrls(result) {
    if (!Array.isArray(result?.file_urls)) return [];
    return result.file_urls.filter(
      (url) =>
        typeof url === 'string' &&
        /^\/api\/files\/[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(url),
    );
  }

  function openProjectedUrl(context, value) {
    if (typeof value !== 'string' || !value) throw new Error('Invalid link');
    let url;
    try {
      url = new URL(value, window.location.origin);
    } catch {
      throw new Error('Invalid link');
    }
    if (url.protocol === 'mailto:') {
      window.open(url.href, '_blank', 'noopener,noreferrer');
      return;
    }
    if (url.protocol !== 'http:' && url.protocol !== 'https:')
      throw new Error('Invalid link');
    if (url.origin === window.location.origin) {
      const localUrl = `${url.pathname}${url.search}`;
      if (!context.allowedUrls.includes(localUrl))
        throw new Error('This local file is unavailable');
    }
    window.open(url.href, '_blank', 'noopener,noreferrer');
  }

  function contextPayload(context) {
    return {
      type: 'vbot.extension.context',
      version: BRIDGE_VERSION,
      nonce: context.nonce,
      epoch: context.descriptor.epoch,
      descriptor: context.descriptor,
      route,
      theme: isPlainObject(theme) ? theme : {},
      locale: typeof locale === 'string' && locale ? locale : 'en',
      timezone: typeof timezone === 'string' && timezone ? timezone : 'UTC',
    };
  }

  // While the page reports an open dialog or menu, it holds one layer of the
  // App navigation; Back/Forward then ask the page to close its topmost one.
  function showPageLayers(context, open) {
    if (open === Boolean(context.releaseLayer)) return;
    if (!open) {
      context.releaseLayer();
      context.releaseLayer = null;
      return;
    }
    context.releaseLayer =
      shell?.registerLayer({
        close: () =>
          post(context, {
            type: 'vbot.extension.layers.close',
            version: BRIDGE_VERSION,
            nonce: context.nonce,
            epoch: context.descriptor.epoch,
            descriptor: context.descriptor,
          }),
      }) ?? (() => {});
  }

  function invalidateContext(reason) {
    const previous = frameContext;
    frameContext = null;
    if (previous) showPageLayers(previous, false);
    for (const finish of previous?.autosaveFlushes.values() ?? []) {
      finish(false);
    }
    for (const subscription of runSubscriptions.values()) subscription.close();
    runSubscriptions.clear();
    if (previous?.window) {
      noteExtensionPageInvalidation(reason);
      previous.window.postMessage(
        {
          type: 'vbot.extension.invalidate',
          version: BRIDGE_VERSION,
          nonce: previous.nonce,
          epoch: previous.descriptor.epoch,
          descriptor: previous.descriptor,
          reason,
        },
        '*',
      );
    }
  }

  function onFrameLoad() {
    invalidateContext('reload');
    const captured = captureDescriptor(descriptor);
    const child = frame?.contentWindow;
    if (disposed || !captured || !child) return;
    const context = {
      window: child,
      nonce: newNonce(),
      descriptor: captured,
      ready: false,
      allowedUrls: [],
      autosaveFlushes: new Map(),
      // Where the App moves on the Back/Forward keys and mouse buttons (the
      // Desktop app), the page forwards the ones pressed inside its frame,
      // which never reach the App window.
      forwardsHistoryInput: shell?.handlesInput === true,
    };
    frameContext = context;
    post(context, {
      type: 'vbot.extension.init',
      version: BRIDGE_VERSION,
      nonce: context.nonce,
      epoch: captured.epoch,
      descriptor: captured,
      route,
      theme: isPlainObject(theme) ? theme : {},
      locale: typeof locale === 'string' && locale ? locale : 'en',
      timezone: typeof timezone === 'string' && timezone ? timezone : 'UTC',
      forwardHistoryInput: context.forwardsHistoryInput,
    });
  }

  function validCall(data) {
    return (
      data.type === 'vbot.extension.call' &&
      METHODS.has(data.method) &&
      typeof data.id === 'string' &&
      data.id.length > 0 &&
      data.id.length <= MAX_REQUEST_ID_LENGTH &&
      isPlainObject(data.params)
    );
  }

  function validRunSubscription(params) {
    return (
      typeof params.group_id === 'string' &&
      params.group_id.length > 0 &&
      typeof params.run_id === 'string' &&
      params.run_id.length > 0 &&
      (params.after_sequence === undefined ||
        (Number.isInteger(params.after_sequence) && params.after_sequence >= 0))
    );
  }

  async function onMessage(event) {
    const context = frameContext;
    const data = event.data;
    if (
      event.origin !== 'null' ||
      !context ||
      event.source !== context.window ||
      !valid(data) ||
      !matchesContext(data, context)
    )
      return;
    if (data.type === 'vbot.extension.ready') {
      context.ready = true;
      post(context, contextPayload(context));
      return;
    }
    if (
      context.ready &&
      data.type === 'vbot.extension.autosave.state' &&
      typeof data.pending === 'boolean'
    ) {
      context.autosavePending = data.pending;
      return;
    }
    if (
      context.ready &&
      data.type === 'vbot.extension.layers.state' &&
      typeof data.open === 'boolean'
    ) {
      showPageLayers(context, data.open);
      return;
    }
    if (
      context.ready &&
      context.forwardsHistoryInput &&
      data.type === 'vbot.extension.history.move' &&
      (data.direction === 'back' || data.direction === 'forward')
    ) {
      if (data.direction === 'back') shell.back();
      else shell.forward();
      return;
    }
    if (
      context.ready &&
      data.type === 'vbot.extension.autosave.result' &&
      context.autosaveFlushes.has(data.id) &&
      typeof data.saved === 'boolean'
    ) {
      context.autosaveFlushes.get(data.id)(data.saved);
      return;
    }
    if (!context.ready || !validCall(data)) return;
    try {
      let result;
      if (
        data.method === 'operation' &&
        typeof data.params.operation === 'string' &&
        data.params.operation &&
        isPlainObject(data.params.arguments)
      )
        result = await invokeExtensionPageOperation(
          context.descriptor.owner,
          data.params.operation,
          data.params.arguments,
          { id: context.descriptor.page, epoch: context.descriptor.epoch },
        );
      else if (
        data.method === 'history.read' &&
        typeof data.params.group_id === 'string' &&
        data.params.group_id &&
        typeof data.params.participant_id === 'string' &&
        data.params.participant_id &&
        (data.params.query === undefined || isPlainObject(data.params.query))
      ) {
        const historyScope = JSON.stringify([
          data.params.group_id,
          data.params.participant_id,
        ]);
        if (context.historyScope !== historyScope) {
          context.historyScope = historyScope;
          context.allowedUrls = [];
        }
        result = await readExtensionPageHistory(
          context.descriptor.owner,
          { id: context.descriptor.page, epoch: context.descriptor.epoch },
          data.params.group_id,
          data.params.participant_id,
          data.params.query ?? {},
        );
        if (frameContext !== context) return;
        if (context.historyScope === historyScope)
          context.allowedUrls = [
            ...new Set([...context.allowedUrls, ...projectedFileUrls(result)]),
          ];
      } else if (
        (data.method === 'link.open' || data.method === 'media.open') &&
        typeof data.params.url === 'string'
      ) {
        openProjectedUrl(context, data.params.url);
        result = {};
      } else if (
        data.method === 'run.subscribe' &&
        validRunSubscription(data.params)
      ) {
        const opened = await openExtensionPageRun(
          context.descriptor.owner,
          { id: context.descriptor.page, epoch: context.descriptor.epoch },
          data.params.group_id,
          data.params.run_id,
          data.params.after_sequence ?? 0,
        );
        if (frameContext !== context) return;
        const url = opened?.stream?.url;
        if (typeof url === 'string' && url.startsWith('/api/extension-runs/')) {
          const runScope =
            typeof opened.participant_id === 'string'
              ? JSON.stringify([data.params.group_id, opened.participant_id])
              : null;
          const subscription = createExtensionRunStream({
            opened,
            afterSequence: data.params.after_sequence ?? 0,
            subscribeRunEvents,
            openRun: (afterSequence) =>
              openExtensionPageRun(
                context.descriptor.owner,
                {
                  id: context.descriptor.page,
                  epoch: context.descriptor.epoch,
                },
                data.params.group_id,
                data.params.run_id,
                afterSequence,
              ),
            onResync: () => {
              noteExtensionPageInvalidation('run_stream_recovered');
              post(context, {
                ...contextPayload(context),
                type: 'vbot.extension.invalidate',
                reason: 'run_stream_recovered',
              });
            },
            onEvent: ({ type, data: payload }) => {
              const event = { ...payload, type };
              if (
                frameContext === context &&
                valid(event, MAX_HOST_MESSAGE_BYTES)
              ) {
                if (
                  runScope !== null &&
                  (context.historyScope == null ||
                    context.historyScope === runScope)
                )
                  context.allowedUrls = [
                    ...new Set([
                      ...context.allowedUrls,
                      ...projectedFileUrls(event),
                    ]),
                  ];
                post(context, {
                  type: 'vbot.extension.stream',
                  version: BRIDGE_VERSION,
                  nonce: context.nonce,
                  epoch: context.descriptor.epoch,
                  descriptor: context.descriptor,
                  id: data.id,
                  event,
                });
              }
            },
          });
          runSubscriptions.get(data.id)?.close();
          runSubscriptions.set(data.id, subscription);
          result = {
            live: true,
            subscription_id: data.id,
            replay_through_sequence: opened.replay_through_sequence,
          };
        } else result = { live: false };
      } else if (
        data.method === 'run.cancel_tool' &&
        validRunSubscription(data.params) &&
        typeof data.params.tool_call_id === 'string' &&
        data.params.tool_call_id.length > 0
      ) {
        result = await cancelExtensionPageToolCall(
          context.descriptor.owner,
          { id: context.descriptor.page, epoch: context.descriptor.epoch },
          data.params.group_id,
          data.params.run_id,
          data.params.tool_call_id,
        );
      } else if (
        data.method === 'run.unsubscribe' &&
        typeof data.params.id === 'string'
      ) {
        runSubscriptions.get(data.params.id)?.close();
        runSubscriptions.delete(data.params.id);
        result = {};
      } else if (
        (data.method === 'route.push' || data.method === 'route.replace') &&
        typeof data.params.route === 'string'
      ) {
        // The new route reaches the page with the next context update.
        const place = placeFromRoute(data.params.route);
        if (data.method === 'route.push') navigation.navigate(place);
        else navigation.replace(place);
        result = {};
      } else if (
        data.method === 'toast' &&
        typeof data.params.message === 'string'
      ) {
        onToast(data.params.message, data.params.variant);
        result = {};
      } else throw new Error('Extension capability is unavailable');
      post(context, {
        type: 'vbot.extension.result',
        version: BRIDGE_VERSION,
        nonce: context.nonce,
        epoch: context.descriptor.epoch,
        descriptor: context.descriptor,
        id: data.id,
        result,
      });
    } catch (error) {
      post(context, {
        type: 'vbot.extension.error',
        version: BRIDGE_VERSION,
        nonce: context.nonce,
        epoch: context.descriptor.epoch,
        descriptor: context.descriptor,
        id: data.id,
        error: error.message,
      });
    }
  }

  $effect.pre(() => {
    const identity = descriptorIdentity(descriptor);
    if (observedDescriptor && observedDescriptor !== identity)
      invalidateContext('descriptor_changed');
    observedDescriptor = identity;
  });

  // Sends route and display changes to the ready frame. The payload is read
  // before the readiness check: `ready` is a plain field, so an effect that
  // first ran before the frame's Ready would otherwise never rerun.
  $effect(() => {
    const context = frameContext;
    if (!context) return;
    const payload = contextPayload(context);
    if (context.ready) post(context, payload);
  });

  function validChange(change) {
    return (
      isPlainObject(change) &&
      typeof change.resource === 'string' &&
      change.resource.length > 0 &&
      Array.isArray(change.ids) &&
      change.ids.every((id) => typeof id === 'string' && id.length > 0) &&
      Number.isInteger(change.revision) &&
      change.revision >= 0
    );
  }

  // An invalidation without `owner` refreshes whichever page is open. One
  // with `owner` reaches only that Extension's page, carrying the records it
  // changed as `change`; an unusable change degrades to a full refresh.
  function forwardInvalidation(next) {
    const context = frameContext;
    if (!context?.ready) return;
    if (next.owner != null && next.owner !== context.descriptor.owner) return;
    noteExtensionPageInvalidation('change');
    const message = {
      type: 'vbot.extension.invalidate',
      version: BRIDGE_VERSION,
      nonce: context.nonce,
      epoch: context.descriptor.epoch,
      descriptor: context.descriptor,
      revision: next.revision ?? null,
    };
    const change = validChange(next.change)
      ? {
          resource: next.change.resource,
          ids: next.change.ids,
          revision: next.change.revision,
        }
      : null;
    post(
      context,
      change && valid({ ...message, change }, MAX_HOST_MESSAGE_BYTES)
        ? { ...message, change }
        : message,
    );
  }

  $effect(() => subscribeInvalidations?.(forwardInvalidation));

  onMount(() => window.addEventListener('message', onMessage));
  onDestroy(() => {
    disposed = true;
    unregisterAutosave();
    invalidateContext('disposed');
    window.removeEventListener('message', onMessage);
  });
</script>

{#if descriptor?.entry_url}
  <iframe
    bind:this={frame}
    title={descriptor.title}
    sandbox="allow-scripts"
    allow="clipboard-write *"
    src={descriptor.entry_url}
    onload={onFrameLoad}
  ></iframe>
{:else}
  <Banner variant="error">{t('extensions.pageUnavailable')}</Banner>
{/if}

<style>
  iframe {
    width: 100%;
    height: 100%;
    min-height: 0;
    border: 0;
    background: var(--surface);
  }
</style>
