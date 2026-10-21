<script module>
  // The Chat's phone layout (`chat/view/layout.svelte.js`): no embedded
  // preview there, the link opens the Portal in its own tab.
  const MOBILE_MEDIA_QUERY = '(max-width: 640px)';

  function mobileQuery() {
    return typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function'
      ? window.matchMedia(MOBILE_MEDIA_QUERY)
      : null;
  }
</script>

<script>
  // The card a `portal` call shows after its row: the Portal's title and live
  // state, an expandable preview of the app, a link that opens it in a new
  // tab, and Close. The state comes from the displayed Session's
  // `portal.list` projection, shared by all its cards
  // (`lib/portals.svelte.js`); the credential in the Portal's address stays
  // in that memory and in the rendered link and frame.
  import { t } from '$lib/i18n.js';
  import { mountHold } from '$lib/mountHold.js';
  import {
    PORTAL_STATUS_CLOSED,
    PORTAL_STATUS_LOADING,
    PORTAL_STATUS_RUNNING,
    PORTAL_STATUS_WAITING,
    portalStatus,
    portalUrl,
    portalsReachable,
  } from '$lib/chatTimelinePresentation.js';
  import {
    closePortal,
    portalSessionAddress,
    sessionPortals,
    watchPortals,
  } from '$lib/portals.svelte.js';
  import Button from '../ui/Button.svelte';
  import { timelineViewState } from './timelineViewState.svelte.js';

  // `{ id, title, port }` from the call's display.
  let { portal } = $props();

  // Outside a Chat timeline (the Swarm page) no Session names the Portal.
  const viewState = timelineViewState();
  // A timeline that unmounts rows scrolled out of view keeps an open
  // preview mounted, so the app inside does not reload.
  const holdMounted = mountHold();

  let root = $state(null);
  let mobile = $state(mobileQuery()?.matches === true);
  let closing = $state(false);
  let closeError = $state('');

  let address = $derived(portalSessionAddress(viewState.sessionKey));
  let projection = $derived(
    address ? sessionPortals(address.agentId, address.sessionId) : null,
  );
  let listed = $derived(projection?.portals?.[portal.id] ?? null);
  let status = $derived(portalStatus(projection, portal.id));
  let isOpen = $derived(
    status === PORTAL_STATUS_RUNNING || status === PORTAL_STATUS_WAITING,
  );
  let reachable = $derived(
    portalsReachable(
      projection?.reverseProxy === true,
      window.location.hostname,
    ),
  );
  let url = $derived(
    isOpen && reachable && listed ? portalUrl(listed, window.location) : '',
  );
  let title = $derived(
    portal.title || listed?.title || t('chat.portal.untitled'),
  );
  let previewKey = $derived(`portal-preview:${portal.id}`);
  let previewShown = $derived(
    Boolean(url) && !mobile && viewState.isOpen(previewKey),
  );

  $effect(() => {
    const target = address;
    if (!target) return undefined;
    return watchPortals(target.agentId, target.sessionId);
  });

  $effect(() => {
    const query = mobileQuery();
    if (!query) return undefined;
    mobile = query.matches;
    const update = (event) => {
      mobile = event.matches;
    };
    query.addEventListener?.('change', update);
    return () => query.removeEventListener?.('change', update);
  });

  $effect(() => {
    if (!previewShown || !root) return undefined;
    return holdMounted(root);
  });

  function togglePreview() {
    viewState.setOpen(previewKey, !previewShown);
  }

  async function close() {
    if (!address || closing) return;
    closing = true;
    closeError = '';
    try {
      await closePortal(address.agentId, address.sessionId, portal.id);
      viewState.setOpen(previewKey, false);
    } catch (error) {
      closeError = t('chat.portal.closeFailed', {
        message: error?.message || String(error),
      });
    } finally {
      closing = false;
    }
  }
</script>

<section
  bind:this={root}
  class="chat-portal"
  aria-label={t('chat.portal.label', { title })}
>
  <div class="chat-portal__header">
    <svg
      class="chat-portal__icon"
      viewBox="0 0 16 16"
      width="14"
      height="14"
      aria-hidden="true"
    >
      <rect x="1.75" y="2.5" width="12.5" height="11" rx="1.5" />
      <path d="M1.75 5.5h12.5M6.5 8.25 9.5 11M9.5 8.25v2.75H6.75" />
    </svg>
    <span class="chat-portal__title">{title}</span>
    {#if portal.port}
      <span class="chat-portal__port"
        >{t('chat.portal.port', { port: portal.port })}</span
      >
    {/if}
    {#if address}
      <span class="chat-portal__status" data-status={status}>
        <span class="chat-portal__dot" aria-hidden="true"></span>
        {#if status === PORTAL_STATUS_RUNNING}
          {t('chat.portal.status.running')}
        {:else if status === PORTAL_STATUS_WAITING}
          {t('chat.portal.status.waiting')}
        {:else if status === PORTAL_STATUS_CLOSED}
          {t('chat.portal.status.closed')}
        {:else if status === PORTAL_STATUS_LOADING}
          {t('chat.portal.status.loading')}
        {:else}
          {t('chat.portal.status.unknown')}
        {/if}
      </span>
    {/if}
    {#if isOpen}
      <span class="chat-portal__actions">
        {#if url && !mobile}
          <Button
            variant="tertiary"
            class="chat-portal__action"
            aria-expanded={previewShown}
            onClick={togglePreview}
          >
            {previewShown
              ? t('chat.portal.hidePreview')
              : t('chat.portal.showPreview')}
          </Button>
        {/if}
        {#if url}
          <a
            class="chat-portal__link"
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            referrerpolicy="no-referrer"
          >
            {t('chat.portal.openInNewTab')}
            <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
              <path d="M6 3.5H3.5v9h9V10M9 3.5h3.5V7M12.5 3.5 7 9" />
            </svg>
          </a>
        {/if}
        <Button
          variant="tertiary"
          class="chat-portal__action"
          loading={closing}
          tooltip={t('chat.portal.closeHint')}
          onClick={close}
        >
          {t('chat.portal.close')}
        </Button>
      </span>
    {/if}
  </div>
  {#if !address}
    <p class="chat-portal__notice">{t('chat.portal.chatOnly')}</p>
  {:else if isOpen && !reachable}
    <p class="chat-portal__notice">{t('chat.portal.unreachable')}</p>
  {/if}
  {#if closeError}
    <p class="chat-portal__notice chat-portal__notice--error" role="alert">
      {closeError}
    </p>
  {/if}
  {#if previewShown}
    <iframe
      class="chat-portal__frame"
      src={url}
      {title}
      referrerpolicy="no-referrer"
    ></iframe>
  {/if}
</section>

<style>
  .chat-portal {
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

  .chat-portal__header {
    display: flex;
    min-width: 0;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 8px;
    padding: 4px 6px 4px 10px;
    color: var(--text-med);
  }

  .chat-portal__icon {
    flex-shrink: 0;
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.3;
  }

  .chat-portal__title {
    min-width: 0;
    overflow: hidden;
    color: var(--text-hi);
    font-size: var(--fs-body-sm);
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chat-portal__port {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-label-sm);
    white-space: nowrap;
  }

  .chat-portal__status {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    white-space: nowrap;
  }

  .chat-portal__dot {
    width: 7px;
    height: 7px;
    flex-shrink: 0;
    border-radius: 50%;
    background: var(--text-faint);
  }

  .chat-portal__status[data-status='running'] .chat-portal__dot {
    background: var(--green);
  }

  .chat-portal__status[data-status='waiting'] .chat-portal__dot {
    background: var(--amber);
  }

  .chat-portal__actions {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    margin-left: auto;
  }

  .chat-portal__actions :global(.chat-portal__action) {
    min-height: 28px;
    padding: 3px 8px;
  }

  .chat-portal__link {
    display: inline-flex;
    min-height: 28px;
    align-items: center;
    gap: 5px;
    padding: 3px 8px;
    border-radius: var(--r-md);
    color: var(--text-med);
    font: 500 var(--fs-body-sm) / 1.3 var(--font-ui);
    text-decoration: none;
    transition:
      background 120ms ease,
      color 120ms ease;
  }

  .chat-portal__link:hover {
    color: var(--text-hi);
    background: var(--surface-3);
  }

  .chat-portal__link:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }

  .chat-portal__link svg {
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.4;
  }

  .chat-portal__notice {
    margin: 0;
    padding: 8px 10px;
    border-top: 1px solid var(--border);
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
  }

  .chat-portal__notice--error {
    color: var(--red);
  }

  .chat-portal__frame {
    display: block;
    width: 100%;
    height: min(560px, 70vh);
    border: 0;
    border-top: 1px solid var(--border);
    /* Like the website preview: a page without its own background reads as
       it would in a tab. */
    background: white;
  }

  @media (prefers-reduced-motion: reduce) {
    .chat-portal__link {
      transition: none;
    }
  }
</style>
