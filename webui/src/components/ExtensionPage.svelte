<script>
  import { onDestroy, onMount } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';
  import Banner from './ui/Banner.svelte';
  import {
    invokeExtensionPageOperation,
    openExtensionPageRun,
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
    'route.replace',
    'toast',
    'run.subscribe',
    'run.unsubscribe',
  ]);

  let {
    descriptor,
    route = '',
    theme = {},
    locale = 'en',
    timezone = 'UTC',
    invalidation = null,
    onRouteChange = () => {},
    onToast = () => {},
  } = $props();
  let frame = $state.raw(null);
  let frameContext = $state.raw(null);
  let disposed = false;
  let observedDescriptor = '';
  let observedInvalidation = null;
  const runSubscriptions = new SvelteMap();

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

  function invalidateContext(reason) {
    const previous = frameContext;
    frameContext = null;
    for (const subscription of runSubscriptions.values()) subscription.close();
    runSubscriptions.clear();
    if (previous?.window)
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
        result = await readExtensionPageHistory(
          context.descriptor.owner,
          { id: context.descriptor.page, epoch: context.descriptor.epoch },
          data.params.group_id,
          data.params.participant_id,
          data.params.query ?? {},
        );
        if (frameContext !== context) return;
        context.allowedUrls = projectedFileUrls(result);
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
          const subscription = subscribeRunEvents(url, {
            onEvent: (event) => {
              if (
                frameContext === context &&
                valid(event, MAX_HOST_MESSAGE_BYTES)
              ) {
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
          result = { live: true, subscription_id: data.id };
        } else result = { live: false };
      } else if (
        data.method === 'run.unsubscribe' &&
        typeof data.params.id === 'string'
      ) {
        runSubscriptions.get(data.params.id)?.close();
        runSubscriptions.delete(data.params.id);
        result = {};
      } else if (
        data.method === 'route.replace' &&
        typeof data.params.route === 'string'
      ) {
        onRouteChange(data.params.route);
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

  $effect(() => {
    const context = frameContext;
    if (context?.ready) post(context, contextPayload(context));
  });

  $effect(() => {
    const next = invalidation;
    const context = frameContext;
    if (!next || next === observedInvalidation || !context?.ready) return;
    observedInvalidation = next;
    if (
      next.owner === context.descriptor.owner &&
      next.page === context.descriptor.page
    )
      post(context, {
        type: 'vbot.extension.invalidate',
        version: BRIDGE_VERSION,
        nonce: context.nonce,
        epoch: context.descriptor.epoch,
        descriptor: context.descriptor,
        revision: next.revision ?? null,
      });
  });

  onMount(() => window.addEventListener('message', onMessage));
  onDestroy(() => {
    disposed = true;
    invalidateContext('disposed');
    window.removeEventListener('message', onMessage);
  });
</script>

{#if descriptor?.entry_url}
  <iframe
    bind:this={frame}
    title={descriptor.title}
    sandbox="allow-scripts"
    src={descriptor.entry_url}
    onload={onFrameLoad}
  ></iframe>
{:else}
  <Banner variant="error">This Extension page is unavailable.</Banner>
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
