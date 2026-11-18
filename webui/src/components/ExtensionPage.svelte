<script module>
  const loadSessionView = () => import('./SessionView.svelte');
</script>

<script>
  import { onDestroy, onMount, tick } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';
  import { t } from '$lib/i18n.js';
  import { formatAgentAddress } from '$lib/agentAddress.js';
  import {
    createExtensionRunStream,
    RUN_STREAM_TIMEOUT_MS,
  } from '$lib/extensionRunStream.js';
  import { noteExtensionPageInvalidation } from '$lib/clientMetrics.js';
  import {
    isDesktopAccessor,
    openDesktopExternalUrl,
  } from '$lib/desktopBridge.js';
  import Banner from './ui/Banner.svelte';
  import LazyContent from './ui/LazyContent.svelte';
  import { createExtensionSessionEmbeds } from '$lib/extensionSessionEmbeds.svelte.js';
  import {
    sessionPresentations,
    validPageChatContext,
  } from '$lib/extensionChatContext.js';
  import { getAppChats } from '$lib/appChats.svelte.js';
  import { useAutosaveContext } from '$lib/autosave.js';
  import {
    createStandaloneNavigation,
    useNavigation,
  } from '$lib/navigation.svelte.js';
  import {
    invokeExtensionPageOperation,
    invokeExtensionPageService,
    listModelFavorites,
    openExtensionPageRun,
    cancelExtensionPageToolCall,
    openExtensionPageSpecialist,
    readExtensionPageHistory,
    readExtensionPageTimelineChildren,
    readExtensionPageToolCall,
    listServerDirectory,
    setModelFavorite,
    subscribeRunEvents,
  } from '$lib/api.js';

  const BRIDGE_VERSION = 1;
  const MAX_MESSAGE_BYTES = 64 * 1024;
  const MAX_HOST_MESSAGE_BYTES = 8 * 1024 * 1024;
  const MAX_REQUEST_ID_LENGTH = 128;
  const safeId = (value) =>
    typeof value === 'string' &&
    value.length > 0 &&
    value.length <= 256 &&
    !/[\s/\\@?#\p{C}]/u.test(value);
  function validRelatedAgent(item) {
    if (
      !isPlainObject(item) ||
      !safeId(item.id) ||
      typeof item.name !== 'string' ||
      item.name.length > 512
    )
      return false;
    if (item.placement == null) return true;
    const placement = item.placement;
    if (!isPlainObject(placement)) return false;
    if (placement.kind === 'private')
      return (
        safeId(placement.owner_id) && typeof placement.orphaned === 'boolean'
      );
    return placement.kind === 'chat' || placement.kind === 'helper';
  }

  const METHODS = new Set([
    'operation',
    'history.read',
    'history.children',
    'history.tool_call',
    'link.open',
    'session.open',
    'agent.open',
    'session.embed',
    'chat.open_specialist',
    'chat.open_app',
    'media.open',
    'route.push',
    'route.replace',
    'toast',
    'run.subscribe',
    'run.unsubscribe',
    'run.cancel_tool',
    'directory.list',
    'favorite_models.list',
    'favorite_models.set',
    'service.describe',
    'service.query',
    'service.execute',
    'ui.command',
    'external.open',
    'search.index',
  ]);

  let {
    descriptor,
    navigation = createStandaloneNavigation(),
    theme = {},
    locale = 'en',
    timezone = 'UTC',
    subscribeInvalidations = null,
    onToast = () => {},
    onOpenSession = () => {},
    onOpenAgent = () => {},
    uiContext = {},
    apiCapabilities = { version: null, features: [] },
    onUiCommand = null,
    embedded = false,
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
  const appChats = getAppChats();
  let frame = $state.raw(null);
  let frameContext = $state.raw(null);
  let disposed = false;
  let observedDescriptor = '';
  let observedUiScope = '';
  let pageLayerOpen = $state(false);
  let searchEntries = $state([]);
  const runSubscriptions = new SvelteMap();
  const embeds = createExtensionSessionEmbeds((context, slot, href) => {
    post(context, {
      ...contextPayload(context),
      type: 'vbot.extension.session.reference',
      slot,
      href,
    });
  });
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
      throw new Error(t('extensions.page.responseTooLarge'));
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
    if (typeof value !== 'string' || !value)
      throw new Error(t('extensions.page.invalidLink'));
    let url;
    try {
      url = new URL(value, window.location.origin);
    } catch {
      throw new Error(t('extensions.page.invalidLink'));
    }
    if (url.protocol === 'mailto:') {
      window.open(url.href, '_blank', 'noopener,noreferrer');
      return;
    }
    if (url.protocol !== 'http:' && url.protocol !== 'https:')
      throw new Error(t('extensions.page.invalidLink'));
    if (url.origin === window.location.origin) {
      const localUrl = `${url.pathname}${url.search}`;
      if (!context.allowedUrls.includes(localUrl))
        throw new Error(t('extensions.page.fileUnavailable'));
    }
    window.open(url.href, '_blank', 'noopener,noreferrer');
  }

  // The owning page projects the editor's related identities in bounded reads.
  // Only this fixed operation runs, always for the original frame registration.
  async function readProfileTargets(context, scope, after) {
    if (frameContext !== context)
      throw new Error(t('extensions.page.invalidLink'));
    const result = await invokeExtensionPageOperation(
      context.descriptor.owner,
      'agent.profile_targets',
      after ? { scope, after } : { scope },
      { id: context.descriptor.page, epoch: context.descriptor.epoch },
    );
    if (
      frameContext !== context ||
      !Array.isArray(result?.agents) ||
      !result.agents.every(validRelatedAgent) ||
      (result.next_after != null && !safeId(result.next_after))
    )
      throw new Error(t('extensions.page.invalidLink'));
    return result;
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
      ui: isPlainObject(uiContext) ? uiContext : {},
      api: apiCapabilities,
    };
  }

  // While the page reports an open dialog or menu, it holds one layer of the
  // App navigation; Back/Forward then ask the page to close its topmost one.
  function showPageLayers(context, open) {
    pageLayerOpen = open;
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
    embeds.slots.clear();
    previous?.specialist?.close();
    searchEntries = [];
    if (previous) showPageLayers(previous, false);
    for (const request of previous?.runReads ?? []) request.abort();
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
      services: Array.isArray(descriptor.services)
        ? [...descriptor.services]
        : [],
      ready: false,
      allowedUrls: [],
      autosaveFlushes: new Map(),
      runReads: new Set(),
      // What changed while the browser tab was hidden (`{revision, change}`),
      // delivered once it shows again.
      heldInvalidation: null,
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
      ui: isPlainObject(uiContext) ? uiContext : {},
      api: apiCapabilities,
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

  function validListing(params) {
    return (
      (params.path == null || typeof params.path === 'string') &&
      (params.root === undefined || typeof params.root === 'string') &&
      (params.include_files === undefined ||
        typeof params.include_files === 'boolean') &&
      (params.prefix == null || typeof params.prefix === 'string')
    );
  }

  // Domain codes survive every bridge call; listings additionally keep their
  // structured reason for the shared PathField's recovery choices.
  function requestFailure(error, method) {
    const failure = {};
    if (typeof error?.code === 'string' && error.code)
      failure.code = error.code;
    const reason = error?.details?.data?.reason;
    if (method === 'directory.list' && typeof reason === 'string' && reason)
      failure.reason = reason;
    if (method.startsWith('service.') && isPlainObject(error?.details?.data))
      failure.data = error.details.data;
    return failure;
  }

  function validRunSubscription(params) {
    return (
      typeof params.group_id === 'string' &&
      params.group_id.length > 0 &&
      typeof params.run_id === 'string' &&
      params.run_id.length > 0 &&
      (params.timeline === undefined || typeof params.timeline === 'boolean') &&
      (params.after_sequence === undefined ||
        (Number.isInteger(params.after_sequence) &&
          params.after_sequence >= 0)) &&
      (params.child_limit === undefined ||
        (params.timeline === true && validStepCount(params.child_limit)))
    );
  }

  function validStepCount(value) {
    return Number.isInteger(value) && value >= 1 && value <= 200;
  }

  function validChildrenRead(params) {
    return (
      [
        'group_id',
        'participant_id',
        'run_id',
        'before',
        'history_generation',
      ].every(
        (key) => typeof params[key] === 'string' && params[key].length > 0,
      ) &&
      (params.limit === undefined || validStepCount(params.limit))
    );
  }

  function validToolCallRead(params) {
    return [
      'group_id',
      'participant_id',
      'run_id',
      'tool_id',
      'history_generation',
    ].every((key) => typeof params[key] === 'string' && params[key].length > 0);
  }

  // Server-verified file links of a History reply join the grants of the
  // participant it was read for; a reply for another participant clears them.
  function grantHistoryFiles(context, scope, result) {
    if (context.historyScope === scope)
      context.allowedUrls = [
        ...new Set([...context.allowedUrls, ...projectedFileUrls(result)]),
      ];
  }

  function enterHistoryScope(context, groupId, participantId) {
    const scope = JSON.stringify([groupId, participantId]);
    if (context.historyScope !== scope) {
      context.historyScope = scope;
      context.allowedUrls = [];
    }
    return scope;
  }

  function runOptions(params) {
    return {
      timeline: params.timeline === true,
      ...(params.child_limit === undefined
        ? {}
        : { childLimit: params.child_limit }),
    };
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
    if (context.ready && data.type === 'vbot.extension.session.bounds') {
      embeds.position(data.slot, data.bounds);
      return;
    }
    if (context.ready && data.type === 'vbot.extension.session.references') {
      embeds.setReferences(data.slot, data.references);
      return;
    }
    if (context.ready && data.type === 'vbot.extension.session.release') {
      embeds.slots.delete(data.slot);
      return;
    }
    if (context.ready && data.type === 'vbot.extension.chat.context') {
      context.chat = validPageChatContext(data.value) ? data.value : null;
      context.chatRoute = route;
      return;
    }
    if (!context.ready || !validCall(data)) return;
    try {
      let result;
      if (data.method.startsWith('service.')) {
        const kind = data.method.slice('service.'.length);
        if (
          kind !== 'describe' &&
          (typeof data.params.name !== 'string' ||
            !isPlainObject(data.params.params))
        )
          throw new Error(t('extensions.page.requestInvalid'));
        result = await invokeExtensionPageService(
          context.descriptor.owner,
          context.descriptor.page,
          context.descriptor.epoch,
          kind,
          data.params.name ?? '',
          data.params.params ?? {},
        );
      } else if (data.method === 'search.index') {
        const entries = data.params.entries;
        const ids = [];
        if (
          !Array.isArray(entries) ||
          entries.length > 200 ||
          entries.some((entry) => {
            if (
              !isPlainObject(entry) ||
              typeof entry.id !== 'string' ||
              !entry.id ||
              entry.id.length > 128 ||
              ids.includes(entry.id) ||
              typeof entry.label !== 'string' ||
              !entry.label ||
              entry.label.length > 256 ||
              typeof entry.terms !== 'string' ||
              entry.terms.length > 4096
            )
              return true;
            ids.push(entry.id);
            return false;
          })
        )
          throw new Error(t('extensions.page.requestInvalid'));
        searchEntries = entries.map(({ id, label, terms }) => ({
          id,
          label,
          terms,
        }));
        result = {};
      } else if (data.method === 'ui.command') {
        if (
          !onUiCommand ||
          typeof data.params.name !== 'string' ||
          !isPlainObject(data.params.params)
        )
          throw new Error(t('extensions.page.requestInvalid'));
        result = await onUiCommand(data.params.name, data.params.params);
      } else if (data.method === 'external.open') {
        const url = new URL(data.params.url);
        if (
          !['https:', 'http:'].includes(url.protocol) ||
          url.username ||
          url.password
        )
          throw new Error(t('extensions.page.invalidLink'));
        if (isDesktopAccessor()) await openDesktopExternalUrl(url.href);
        else window.open(url.href, '_blank', 'noopener,noreferrer');
        result = {};
      } else if (
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
        const historyScope = enterHistoryScope(
          context,
          data.params.group_id,
          data.params.participant_id,
        );
        result = await readExtensionPageHistory(
          context.descriptor.owner,
          { id: context.descriptor.page, epoch: context.descriptor.epoch },
          data.params.group_id,
          data.params.participant_id,
          data.params.query ?? {},
        );
        if (frameContext !== context) return;
        grantHistoryFiles(context, historyScope, result);
      } else if (
        data.method === 'history.children' &&
        validChildrenRead(data.params)
      ) {
        const historyScope = enterHistoryScope(
          context,
          data.params.group_id,
          data.params.participant_id,
        );
        result = await readExtensionPageTimelineChildren(
          context.descriptor.owner,
          { id: context.descriptor.page, epoch: context.descriptor.epoch },
          data.params.group_id,
          data.params.participant_id,
          {
            runId: data.params.run_id,
            before: data.params.before,
            historyGeneration: data.params.history_generation,
            ...(data.params.limit === undefined
              ? {}
              : { limit: data.params.limit }),
          },
        );
        if (frameContext !== context) return;
        grantHistoryFiles(context, historyScope, result);
      } else if (
        data.method === 'history.tool_call' &&
        validToolCallRead(data.params)
      ) {
        const historyScope = enterHistoryScope(
          context,
          data.params.group_id,
          data.params.participant_id,
        );
        result = await readExtensionPageToolCall(
          context.descriptor.owner,
          { id: context.descriptor.page, epoch: context.descriptor.epoch },
          data.params.group_id,
          data.params.participant_id,
          {
            runId: data.params.run_id,
            toolId: data.params.tool_id,
            historyGeneration: data.params.history_generation,
          },
        );
        if (frameContext !== context) return;
        grantHistoryFiles(context, historyScope, result);
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
        const afterSequence = data.params.after_sequence ?? 0;
        const openRun = (sequence, options) =>
          openExtensionPageRun(
            context.descriptor.owner,
            { id: context.descriptor.page, epoch: context.descriptor.epoch },
            data.params.group_id,
            data.params.run_id,
            sequence,
            { ...runOptions(data.params), ...options },
          );
        const request = new AbortController();
        context.runReads.add(request);
        const timer = setTimeout(() => request.abort(), RUN_STREAM_TIMEOUT_MS);
        request.signal.addEventListener('abort', () => clearTimeout(timer), {
          once: true,
        });
        const opened = await openRun(afterSequence, {
          signal: request.signal,
        }).finally(() => {
          clearTimeout(timer);
          context.runReads.delete(request);
        });
        if (frameContext !== context) return;
        if (request.signal.aborted)
          throw new Error(t('extensions.page.requestTimedOut'));
        const url = opened?.stream?.url;
        if (typeof url === 'string' && url.startsWith('/api/extension-runs/')) {
          const runScope =
            typeof opened.participant_id === 'string'
              ? JSON.stringify([data.params.group_id, opened.participant_id])
              : null;
          const subscription = createExtensionRunStream({
            opened,
            afterSequence,
            subscribeRunEvents,
            openRun,
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
            timeline_version: opened.stream?.timeline_version,
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
        data.method === 'directory.list' &&
        validListing(data.params)
      ) {
        // Path fields of the page browse the server's folders, as in the app.
        result = await listServerDirectory({
          path: data.params.path ?? null,
          root: data.params.root,
          include_files: data.params.include_files === true,
          prefix: data.params.prefix ?? null,
        });
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
        // A guarded navigation can wait for autosave or a user's decision.
        // Deliver its resulting context before the page continues after await.
        const place = placeFromRoute(data.params.route);
        if (data.method === 'route.push') await navigation.navigate(place);
        else await navigation.replace(place);
        await tick();
        result = {};
      } else if (data.method === 'session.embed') {
        await embeds.mount(context, data.params);
        result = {};
      } else if (
        data.method === 'chat.open_specialist' ||
        data.method === 'chat.open_app'
      ) {
        const shared = data.method === 'chat.open_app';
        const { profile, presentation = 'conversation' } = data.params;
        if (
          !appChats ||
          document.activeElement !== frame ||
          !navigator.userActivation?.isActive ||
          !sessionPresentations.includes(presentation) ||
          (shared
            ? Object.keys(data.params).length !== 0
            : typeof profile !== 'string' ||
              !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(profile))
        )
          throw new Error(t('extensions.page.requestInvalid'));
        const { owner, page, epoch } = context.descriptor;
        context.specialist = appChats.openSpecialist({
          profile: shared ? 'app' : `extension:${owner}:${profile}`,
          presentation,
          title: descriptor.title,
          key: JSON.stringify([owner, page, epoch, shared ? null : profile]),
          createSession: shared
            ? undefined
            : (id) => {
                if (frameContext !== context)
                  throw new Error(t('extensions.page.reloaded'));
                return openExtensionPageSpecialist(
                  owner,
                  { id: page, epoch },
                  profile,
                  id,
                );
              },
          getUiContext: () => {
            if (frameContext !== context)
              throw new Error(t('extensions.page.reloaded'));
            return {
              view: `extension:${owner}:${page}`,
              ...(route ? { page: route.slice(0, 256) } : {}),
              ...(context.chatRoute === route ? context.chat : null),
            };
          },
        });
        result = {};
      } else if (data.method === 'agent.open') {
        const {
          agent_id: id,
          expected_incarnation: expected,
          related_scope: scope,
        } = data.params;
        if (
          !safeId(id) ||
          (expected !== undefined && !safeId(expected)) ||
          (scope !== undefined && !safeId(scope))
        )
          throw new Error(t('extensions.page.invalidLink'));
        onOpenAgent(id, {
          expectedIncarnation: expected,
          readRelatedAgents: scope
            ? (after) => readProfileTargets(context, scope, after)
            : undefined,
        });
        result = {};
      } else if (data.method === 'session.open') {
        const address = data.params.address;
        if (
          !isPlainObject(address) ||
          Object.keys(address).some(
            (key) => !['agent_id', 'project_id', 'session_id'].includes(key),
          ) ||
          !safeId(address.agent_id) ||
          !safeId(address.session_id) ||
          (address.project_id != null && !safeId(address.project_id))
        )
          throw new Error(t('extensions.page.invalidLink'));
        onOpenSession({
          agentId: formatAgentAddress(address.agent_id, address.project_id),
          sessionId: address.session_id,
        });
        result = {};
      } else if (
        data.method === 'toast' &&
        typeof data.params.message === 'string'
      ) {
        onToast(data.params.message, data.params.variant);
        result = {};
      } else if (data.method === 'favorite_models.list') {
        result = await listModelFavorites();
      } else if (
        data.method === 'favorite_models.set' &&
        typeof data.params.model === 'string' &&
        typeof data.params.favorite === 'boolean'
      ) {
        result = await setModelFavorite(
          data.params.model,
          data.params.favorite,
        );
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
        ...requestFailure(error, data.method),
      });
    }
  }

  $effect.pre(() => {
    const identity = descriptorIdentity(descriptor);
    if (observedDescriptor && observedDescriptor !== identity)
      invalidateContext('descriptor_changed');
    observedDescriptor = identity;
  });

  // A retained inline view must not apply a queued action to another Session
  // or Tool row. A new scope starts a fresh handshake without reloading assets.
  $effect.pre(() => {
    const next = JSON.stringify([
      uiContext?.context?.agent_id,
      uiContext?.context?.session_id,
      uiContext?.context?.project_id,
      uiContext?.context?.run_id,
      uiContext?.context?.tool_call_id,
      uiContext?.context?.message_id,
      uiContext?.context?.editor_mode,
    ]);
    if (observedUiScope && next !== observedUiScope && frameContext)
      onFrameLoad();
    observedUiScope = next;
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
  //
  // While the browser tab is hidden, the frame's invalidations wait: the page
  // would only refresh what nobody sees. Showing the tab delivers them as one:
  // the records they all named when they name one resource, else a full
  // refresh. A reload drops what waited, as the new frame reads everything.
  function forwardInvalidation(next) {
    const context = frameContext;
    if (!context?.ready) return;
    if (next.serviceResource) {
      if (
        next.serviceResource.epoch !== context.descriptor.epoch ||
        !next.serviceResource.services.some((name) =>
          context.services.includes(name),
        )
      )
        return;
    } else if (next.owner != null && next.owner !== context.descriptor.owner)
      return;
    const invalidation = {
      revision: next.revision ?? null,
      change: validChange(next.change)
        ? {
            resource: next.change.resource,
            ids: next.change.ids,
            revision: next.change.revision,
          }
        : null,
    };
    if (document.hidden) {
      context.heldInvalidation = mergeInvalidations(
        context.heldInvalidation,
        invalidation,
      );
      return;
    }
    postInvalidation(context, invalidation);
  }

  function postInvalidation(context, { revision, change }) {
    noteExtensionPageInvalidation('change');
    const message = {
      type: 'vbot.extension.invalidate',
      version: BRIDGE_VERSION,
      nonce: context.nonce,
      epoch: context.descriptor.epoch,
      descriptor: context.descriptor,
      revision,
    };
    post(
      context,
      change && valid({ ...message, change }, MAX_HOST_MESSAGE_BYTES)
        ? { ...message, change }
        : message,
    );
  }

  // Beyond this many records a held change becomes a full refresh.
  const MAX_HELD_CHANGE_IDS = 1000;

  function mergeInvalidations(held, next) {
    if (!held) return next;
    const sameResource =
      held.change &&
      next.change &&
      held.change.resource === next.change.resource;
    const ids = sameResource
      ? [...new Set([...held.change.ids, ...next.change.ids])]
      : [];
    return {
      revision: next.revision,
      change:
        sameResource && ids.length <= MAX_HELD_CHANGE_IDS
          ? {
              resource: next.change.resource,
              ids,
              revision: Math.max(held.change.revision, next.change.revision),
            }
          : null,
    };
  }

  function onVisibilityChange() {
    const context = frameContext;
    if (document.hidden || !context?.heldInvalidation) return;
    const held = context.heldInvalidation;
    context.heldInvalidation = null;
    postInvalidation(context, held);
  }

  $effect(() => subscribeInvalidations?.(forwardInvalidation));

  onMount(() => {
    window.addEventListener('message', onMessage);
    document.addEventListener('visibilitychange', onVisibilityChange);
  });
  onDestroy(() => {
    disposed = true;
    unregisterAutosave();
    invalidateContext('disposed');
    window.removeEventListener('message', onMessage);
    document.removeEventListener('visibilitychange', onVisibilityChange);
  });
</script>

{#if descriptor?.entry_url}
  {#each searchEntries as entry (entry.id)}
    <button
      type="button"
      hidden
      data-search-label={entry.label}
      data-search-terms={entry.terms}
      data-extension-search
      onclick={() => {
        if (!frameContext?.ready) return;
        post(frameContext, {
          ...contextPayload(frameContext),
          type: 'vbot.extension.search.reveal',
          id: entry.id,
        });
        frame?.focus({ preventScroll: true });
      }}>{entry.label}</button
    >
  {/each}
  <div class="extension-page">
    <iframe
      class:extension-page--layer={embedded && pageLayerOpen}
      bind:this={frame}
      title={descriptor.title}
      sandbox="allow-scripts"
      allow="clipboard-write *"
      src={descriptor.entry_url}
      onload={onFrameLoad}
    ></iframe>
    {#each embeds.slots as [slot, view] (slot)}
      {#if view.address && view.bounds}
        {@const bounds = view.bounds}
        <div
          class="session-embed"
          hidden={!view.visible || pageLayerOpen}
          style:left={`${bounds.x}px`}
          style:top={`${bounds.y}px`}
          style:width={`${bounds.width}px`}
          style:height={`${bounds.height}px`}
          style:clip-path={`inset(${bounds.clip.map((n) => n + 'px').join(' ')})`}
        >
          <LazyContent load={loadSessionView}>
            {#snippet children(View)}
              <View
                address={view.address}
                presentation={view.presentation}
                active={Boolean(view.visible && !pageLayerOpen)}
                readOnly
                monitor={view.monitor}
                references={view.references}
              />
            {/snippet}
          </LazyContent>
        </div>
      {/if}
    {/each}
  </div>
{:else}
  <Banner variant="error">{t('extensions.pageUnavailable')}</Banner>
{/if}

<style>
  .extension-page {
    position: relative;
    width: 100%;
    height: 100%;
    min-height: 0;
    overflow: clip;
  }
  .session-embed {
    position: absolute;
    overflow: hidden;
    contain: strict;
    background: var(--surface);
  }
  iframe {
    display: block;
    width: 100%;
    height: 100%;
    min-height: 0;
    border: 0;
    background: var(--surface);
  }
  .extension-page--layer {
    position: fixed;
    inset: 0;
    z-index: 1200;
    width: 100vw;
    height: 100dvh;
  }
</style>
