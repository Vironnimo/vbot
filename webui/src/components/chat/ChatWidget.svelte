<script module>
  // The frame may grow to this height; taller content scrolls inside it.
  const MIN_HEIGHT_PX = 48;
  const MAX_HEIGHT_PX = 720;
  const DEFAULT_HEIGHT_PX = 120;
  const MAX_MESSAGE_BYTES = 16 * 1024;
  // The server refuses larger states, so they are never kept.
  const MAX_STATE_BYTES = 8 * 1024;
  const MAX_REMEMBERED_HEIGHTS = 200;
  const HELLO = 'vbot-widget-hello';
  const INIT = 'vbot-widget-init';
  const STATE = 'vbot-widget-state';
  const SIZE = 'vbot-widget-size';
  // The last height each widget reported, so a frame mounted again (a row
  // scrolled back into view, a Session shown again) starts at its size and
  // the timeline does not jump. Bookkeeping, never rendered.
  // eslint-disable-next-line svelte/prefer-svelte-reactivity -- bookkeeping, never rendered
  const lastHeights = new Map();

  function rememberHeight(widgetId, height) {
    lastHeights.delete(widgetId);
    lastHeights.set(widgetId, height);
    if (lastHeights.size > MAX_REMEMBERED_HEIGHTS) {
      lastHeights.delete(lastHeights.keys().next().value);
    }
  }

  function serializedBytes(value) {
    try {
      return new TextEncoder().encode(JSON.stringify(value)).byteLength;
    } catch {
      return Infinity;
    }
  }

  function newNonce() {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (value) =>
      value.toString(16).padStart(2, '0'),
    ).join('');
  }
</script>

<script>
  // An Agent widget (`show_widget`) inline in the timeline: its document runs
  // in an opaque-origin frame, and a small bridge the server injects reports
  // the widget's state and height. This side answers each document's hello
  // with a fresh nonce and the state to restore, then accepts only messages
  // from that frame's window carrying that nonce. Reported states go to the
  // widget state store under the displayed Session, whose composer offers
  // changed ones for the next send.
  import { untrack } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { isPlainObject } from '$lib/values.js';
  import {
    reportWidgetState,
    storedWidgetState,
  } from '$lib/widgetStates.svelte.js';
  import { timelineViewState } from './timelineViewState.svelte.js';

  // `{ id, title, url }`; `url` is '' when no document is delivered.
  let { widget } = $props();

  // Outside a Chat timeline (the Swarm page) there is no Session to keep the
  // widget's state, and its frame could not load there.
  const viewState = timelineViewState();
  let sessionKey = $derived(viewState.sessionKey);
  let title = $derived(widget.title || t('chat.widget.untitled'));
  let frame = $state(null);
  // The document the frame shows. A newer URL for the same widget (a server
  // restart mints new capabilities) replaces it only while the frame has not
  // reached the bridge, so a working widget never reloads needlessly.
  let src = $state(untrack(() => widget.url));
  let height = $state(
    untrack(() => lastHeights.get(widget.id) ?? DEFAULT_HEIGHT_PX),
  );
  // The current document's bridge: its window, nonce and whether a message
  // with that nonce arrived. Set by the document's hello.
  let bridge = null;
  let connected = false;

  $effect(() => {
    const url = widget.url;
    if (url !== untrack(() => src) && !connected) src = url;
  });

  // Each frame element is a new window; nothing of the previous one counts.
  $effect(() => {
    src;
    bridge = null;
    connected = false;
  });

  // The bridge repeats its hello until an init reaches it and adopts only
  // the first init, so a repeated hello gets the same nonce again. A hello
  // after the document used its nonce comes from a new document.
  function connect(child) {
    if (!bridge || bridge.window !== child || bridge.confirmed) {
      bridge = { window: child, nonce: newNonce(), confirmed: false };
    }
    connected = true;
    const restore = storedWidgetState(sessionKey, widget.id);
    child.postMessage(
      {
        type: INIT,
        nonce: bridge.nonce,
        state: restore ? JSON.parse(JSON.stringify(restore)) : null,
      },
      '*',
    );
  }

  function receiveState(data) {
    if (
      !isPlainObject(data.state) ||
      serializedBytes(data.state) > MAX_STATE_BYTES
    ) {
      return;
    }
    reportWidgetState(sessionKey, widget.id, {
      title: widget.title,
      state: JSON.parse(JSON.stringify(data.state)),
      initial: data.initial === true,
    });
  }

  function receiveSize(data) {
    if (typeof data.height !== 'number' || !Number.isFinite(data.height)) {
      return;
    }
    height = Math.min(
      MAX_HEIGHT_PX,
      Math.max(MIN_HEIGHT_PX, Math.ceil(data.height)),
    );
    rememberHeight(widget.id, height);
  }

  function handleMessage(event) {
    const child = frame?.contentWindow;
    const data = event.data;
    if (
      !child ||
      event.source !== child ||
      event.origin !== 'null' ||
      !isPlainObject(data) ||
      ![HELLO, STATE, SIZE].includes(data.type) ||
      serializedBytes(data) > MAX_MESSAGE_BYTES
    ) {
      return;
    }
    if (data.type === HELLO) {
      connect(child);
      return;
    }
    if (!bridge || bridge.window !== child || data.nonce !== bridge.nonce) {
      return;
    }
    bridge.confirmed = true;
    if (data.type === STATE) receiveState(data);
    else receiveSize(data);
  }

  $effect(() => {
    if (!sessionKey || !src) return undefined;
    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  });
</script>

<section class="chat-widget" aria-label={title}>
  <div class="chat-widget__header">
    <svg
      class="chat-widget__icon"
      viewBox="0 0 16 16"
      width="14"
      height="14"
      aria-hidden="true"
    >
      <rect x="2" y="2.5" width="12" height="11" rx="1.5" />
      <path d="M2 5.5h12M5 8.5h6M5 11h3.5" />
    </svg>
    <span class="chat-widget__title">{title}</span>
  </div>
  {#if !src}
    <p class="chat-widget__notice">{t('chat.widget.unavailable')}</p>
  {:else if !sessionKey}
    <p class="chat-widget__notice">{t('chat.widget.chatOnly')}</p>
  {:else}
    {#key src}
      <iframe
        bind:this={frame}
        class="chat-widget__frame"
        style:height={`${height}px`}
        {src}
        {title}
        sandbox="allow-scripts"
        referrerpolicy="no-referrer"
        allow="camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'"
      ></iframe>
    {/key}
  {/if}
</section>

<style>
  .chat-widget {
    display: flex;
    align-self: stretch;
    min-width: 0;
    flex-direction: column;
    margin-top: var(--space-sm);
    overflow: hidden;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface);
  }

  .chat-widget__header {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    border-bottom: 1px solid var(--border);
    color: var(--text-med);
  }

  .chat-widget__icon {
    flex-shrink: 0;
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-width: 1.3;
  }

  .chat-widget__title {
    overflow: hidden;
    color: var(--text-hi);
    font-size: var(--fs-body-sm);
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chat-widget__frame {
    display: block;
    width: 100%;
    border: 0;
    background: transparent;
    color-scheme: dark;
  }

  .chat-widget__notice {
    margin: 0;
    padding: 10px;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
  }
</style>
