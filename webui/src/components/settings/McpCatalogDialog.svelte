<script>
  // Connects a hosted MCP service from the MCP Extension's catalog in one
  // dialog: choose a service and Connect, sign in to it in the browser when
  // it needs an account, then choose the Agents that may use it. Opened for
  // an existing connection, it starts at that connection's waiting sign-in.
  import { onMount, untrack } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';
  import Dropdown from '../Dropdown.svelte';
  import RequestedUrl from '../RequestedUrl.svelte';
  import Badge from '../ui/Badge.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextField from '../ui/TextField.svelte';
  import {
    extensionOperation,
    listAgents,
    listTools,
    updateAgent,
  } from '$lib/api.js';
  import { inputResponse } from '$lib/extensionInputs.js';
  import { t } from '$lib/i18n.js';
  import {
    MCP_CATALOG_CATEGORIES,
    mcpAgentAccess,
    mcpCatalogMark,
    mcpCatalogMatches,
    mcpGrantedToolAccess,
    mcpSignInPhase,
  } from '$lib/mcpCatalog.js';
  import { mcpProblemText } from '$lib/mcpSettings.js';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/timeText.js';
  import { tooltip } from '$lib/tooltip.js';

  const noop = () => {};
  // The MCP Extension's changes that alter a connection's sign-in: its
  // status, and its pending inputs, which hold the sign-in request.
  const SIGN_IN_RESOURCES = new Set(['connections', 'pending_inputs']);

  let {
    subscribeInvalidations = null,
    // The id of a saved connection whose waiting sign-in the dialog opens
    // at, instead of the catalog.
    signInConnection = null,
    onClose = noop,
    // Receives the id of a connection the dialog finished setting up.
    onConnected = noop,
  } = $props();

  const componentId = $props.id();
  let view = $state('browse');
  let entries = $state(null);
  let loadError = $state('');
  let query = $state('');
  let category = $state('');
  const readOnly = new SvelteSet();
  let busy = $state(false);
  let error = $state('');
  // The connection being set up: its id, the service name and whether it
  // signs in with OAuth.
  let target = $state(null);
  let status = $state(null);
  // The last sign-in request seen, which tells a timed-out sign-in from
  // another failure after the request is gone.
  let lastRequest = $state(null);
  let now = $state(Date.now());
  let pasted = $state('');
  let access = $state(null);
  let tools = $state([]);
  const chosen = new SvelteSet();
  let stopped = false;
  // One status read runs at a time; changes arriving meanwhile read once
  // more after it.
  let reading = false;
  let readQueued = false;

  let categoryOptions = $derived([
    { value: '', label: t('mcp.catalogAllCategories') },
    ...MCP_CATALOG_CATEGORIES.map((value) => ({
      value,
      label: {
        knowledge: t('mcp.catalogCategory.knowledge'),
        productivity: t('mcp.catalogCategory.productivity'),
        design: t('mcp.catalogCategory.design'),
        development: t('mcp.catalogCategory.development'),
        analytics: t('mcp.catalogCategory.analytics'),
        business: t('mcp.catalogCategory.business'),
      }[value],
    })),
  ]);
  let shown = $derived(
    entries ? mcpCatalogMatches(entries, { query, category }) : [],
  );
  let signIn = $derived(mcpSignInPhase(status, lastRequest, now));
  let title = $derived(
    view === 'signin'
      ? t('mcp.signInFor', { name: target.name })
      : view === 'grant'
        ? t('mcp.catalogGrantTitle', { name: target.name })
        : t('mcp.catalogTitle'),
  );
  let grantable = $derived(
    (access ?? []).filter((item) => chosen.has(item.agent.id)),
  );

  onMount(() => {
    const id = untrack(() => signInConnection);
    if (id) {
      target = { id, name: id, oauth: true };
      view = 'signin';
      void readStatus();
    } else void loadCatalog();
    return () => {
      stopped = true;
    };
  });
  $effect(() => subscribeInvalidations?.(onInvalidation));
  // A waiting sign-in turns into a timed-out one at its expiry, even when no
  // change arrives then.
  $effect(() => {
    const expires = Date.parse(signIn.request?.expires_at ?? '');
    if (signIn.phase !== 'waiting' || Number.isNaN(expires)) return;
    const timer = setTimeout(
      () => {
        now = Date.now();
      },
      Math.max(0, expires - Date.now()),
    );
    return () => clearTimeout(timer);
  });

  function onInvalidation({ owner, change }) {
    if (view !== 'signin') return;
    if (
      owner == null ||
      (owner === 'mcp' && (!change || SIGN_IN_RESOURCES.has(change.resource)))
    )
      void readStatus();
  }

  async function loadCatalog() {
    loadError = '';
    try {
      const result = await extensionOperation('mcp', 'catalog');
      if (!stopped) entries = result.entries;
    } catch (failure) {
      if (!stopped) loadError = failure.message;
    }
  }

  async function connect(entry) {
    busy = true;
    error = '';
    try {
      const result = await extensionOperation('mcp', 'add_from_catalog', {
        entry: entry.id,
        ...(readOnly.has(entry.id) ? { read_only: true } : {}),
      });
      if (stopped) return;
      target = {
        id: result.id,
        name: entry.name,
        oauth: entry.auth === 'oauth',
      };
      if (!target.oauth) {
        await openGrant();
        return;
      }
      view = 'signin';
      showStatus(result);
    } catch (failure) {
      if (!stopped) error = failure.message;
    } finally {
      busy = false;
    }
  }

  async function readStatus() {
    readQueued = true;
    if (reading) return;
    reading = true;
    try {
      while (readQueued && !stopped && view === 'signin') {
        readQueued = false;
        try {
          showStatus(
            await extensionOperation('mcp', 'status', { id: target.id }),
          );
        } catch (failure) {
          if (!stopped) error = failure.message;
        }
      }
    } finally {
      reading = false;
    }
  }

  function showStatus(next) {
    if (stopped || view !== 'signin') return;
    status = next;
    now = Date.now();
    const phase = mcpSignInPhase(next, lastRequest, now);
    if (phase.request) lastRequest = phase.request;
    if (phase.phase === 'connected') void openGrant();
  }

  // The browser that signed in could not reach vBot: the address it shows
  // completes the sign-in.
  async function completeSignIn(event) {
    event.preventDefault();
    const request = signIn.request;
    if (!request || !pasted.trim()) return;
    busy = true;
    error = '';
    try {
      await extensionOperation('mcp', 'respond', {
        request_id: request.id,
        response: inputResponse(request, { redirect_url: pasted.trim() }),
      });
      pasted = '';
    } catch (failure) {
      if (!stopped) error = failure.message;
    } finally {
      busy = false;
    }
  }

  // A new sign-in: a stored one is replaced, otherwise the connection
  // starts again and asks for one.
  async function retry() {
    busy = true;
    error = '';
    lastRequest = null;
    try {
      const operation = status?.oauth?.signed_in ? 'reauthorize' : 'reconnect';
      showStatus(await extensionOperation('mcp', operation, { id: target.id }));
    } catch (failure) {
      if (!stopped) error = failure.message;
    } finally {
      busy = false;
    }
  }

  async function openGrant() {
    view = 'grant';
    error = '';
    access = null;
    try {
      const [agentsResult, toolsResult] = await Promise.all([
        listAgents(),
        listTools(),
      ]);
      if (stopped) return;
      tools = toolsResult.tools;
      access = mcpAgentAccess(agentsResult.agents, tools, `mcp_${target.id}`);
    } catch (failure) {
      if (!stopped) error = failure.message;
    }
  }

  async function grant() {
    busy = true;
    error = '';
    try {
      for (const { agent } of grantable) {
        await updateAgent({
          id: agent.id,
          tool_access: mcpGrantedToolAccess(agent, tools, `mcp_${target.id}`),
        });
        chosen.delete(agent.id);
        access = access.map((item) =>
          item.agent.id === agent.id ? { ...item, granted: true } : item,
        );
      }
      if (!stopped) onConnected(target.id);
    } catch (failure) {
      if (!stopped) error = failure.message;
    } finally {
      busy = false;
    }
  }

  function finish() {
    onConnected(target.id);
  }

  function failureText() {
    return (
      mcpProblemText(status?.problem) ||
      status?.error ||
      t('mcp.catalogSignInFailed')
    );
  }
</script>

<Modal
  {title}
  closeDisabled={busy}
  onClose={busy ? noop : onClose}
  class="mcp-modal mcp-catalog-modal"
>
  {#snippet body()}
    <div class="modal-body mcp-editor mcp-catalog">
      {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
      {#if view === 'browse'}
        <p>{t('mcp.catalogHelp')}</p>
        <div class="mcp-catalog__filters">
          <TextField
            type="search"
            ariaLabel={t('mcp.catalogSearch')}
            placeholder={t('mcp.catalogSearch')}
            value={query}
            onInput={(value) => {
              query = value;
            }}
          />
          <Dropdown
            value={category}
            options={categoryOptions}
            ariaLabel={t('mcp.catalogCategoryLabel')}
            onValueChange={(value) => {
              category = value;
            }}
          />
        </div>
        {#if loadError}
          <Banner variant="error" role="alert">
            {loadError}
            <Button variant="secondary" onClick={loadCatalog}
              >{t('common.retry')}</Button
            >
          </Banner>
        {:else if !entries}
          <Banner variant="neutral" role="status">{t('common.loading')}</Banner>
        {:else if !shown.length}
          <EmptyState
            density="compact"
            title={t('mcp.catalogEmpty')}
            description={t('mcp.catalogEmptyHint')}
          />
        {:else}
          <ul class="mcp-catalog__tiles">
            {#each shown as entry (entry.id)}
              {@const mark = mcpCatalogMark(entry)}
              <li class="mcp-catalog__tile" aria-label={entry.name}>
                <div class="mcp-catalog__head">
                  <span
                    class="mcp-catalog__mark"
                    style:--mcp-catalog-hue={mark.hue}
                    aria-hidden="true">{mark.initials}</span
                  >
                  <div class="mcp-catalog__title">
                    <h4>{entry.name}</h4>
                    <span class="mcp-catalog__auth"
                      >{entry.auth === 'oauth'
                        ? t('mcp.catalogAuthOauth')
                        : t('mcp.catalogAuthNone')}</span
                    >
                  </div>
                  {#if entry.connections.length}
                    <span
                      class="tooltip-anchor"
                      use:tooltip={t('mcp.catalogConnectedAs', {
                        ids: entry.connections.join(', '),
                      })}
                      ><Badge variant="success"
                        >{t('mcp.catalogConnected')}</Badge
                      ></span
                    >
                  {/if}
                </div>
                <p class="mcp-catalog__description">{entry.description}</p>
                {#each entry.notes ?? [] as note, noteIndex (noteIndex)}
                  <p class="mcp-catalog__note">{note}</p>
                {/each}
                <div class="mcp-catalog__actions">
                  {#if entry.read_only_url}
                    <Checkbox
                      checked={readOnly.has(entry.id)}
                      disabled={busy}
                      onChange={(checked) => {
                        if (checked) readOnly.add(entry.id);
                        else readOnly.delete(entry.id);
                      }}>{t('mcp.catalogReadOnly')}</Checkbox
                    >
                  {/if}
                  {#if entry.docs_url}
                    <a
                      class="mcp-catalog__docs"
                      href={entry.docs_url}
                      target="_blank"
                      rel="noopener noreferrer">{t('mcp.catalogDocs')}</a
                    >
                  {/if}
                  <Button
                    variant="secondary"
                    class="mcp-catalog__connect"
                    disabled={busy}
                    ariaLabel={t('mcp.catalogConnectAria', {
                      name: entry.name,
                    })}
                    onClick={() => connect(entry)}
                    >{t('mcp.catalogConnect')}</Button
                  >
                </div>
              </li>
            {/each}
          </ul>
        {/if}
      {:else if view === 'signin'}
        <p>{t('mcp.catalogSignInHelp', { name: target.name })}</p>
        {#if signIn.phase === 'waiting'}
          <RequestedUrl
            url={signIn.request.payload?.url}
            openLabel={t('extensions.openSignIn')}
            disabled={busy}
            onOpenFailed={() => (error = t('extensions.openFailed'))}
          />
          {#if signIn.request.expires_at}
            <p>
              {t('mcp.catalogSignInExpires', {
                time: formatAbsoluteTime(signIn.request.expires_at),
                distance: formatRelativeTime(signIn.request.expires_at),
              })}
            </p>
          {/if}
          <Banner variant="neutral" role="status"
            >{t('mcp.catalogSignInWaiting')}</Banner
          >
          <details class="mcp-catalog__paste">
            <summary>{t('mcp.catalogOtherDevice')}</summary>
            <form
              id={`${componentId}-paste`}
              class="mcp-editor"
              onsubmit={completeSignIn}
            >
              <p>{t('mcp.catalogPasteHelp')}</p>
              <FormField
                controlId={`${componentId}-redirect`}
                label={t('extensions.redirectUrl')}
                full
              >
                {#snippet children(field)}<TextField
                    id={field.controlId}
                    type="password"
                    autocomplete="off"
                    value={pasted}
                    disabled={busy}
                    onInput={(value) => {
                      pasted = value;
                    }}
                  />{/snippet}
              </FormField>
              <div class="mcp-actions">
                <Button
                  type="submit"
                  variant="secondary"
                  disabled={busy || !pasted.trim()}
                  >{t('mcp.catalogCompleteSignIn')}</Button
                >
              </div>
            </form>
          </details>
        {:else if signIn.phase === 'failed' || signIn.phase === 'timeout'}
          <Banner
            variant={signIn.phase === 'timeout' ? 'warn' : 'error'}
            role="alert"
          >
            {signIn.phase === 'timeout'
              ? t('mcp.catalogSignInTimedOut')
              : failureText()}
            <Button variant="secondary" disabled={busy} onClick={retry}
              >{t('mcp.catalogRetrySignIn')}</Button
            >
          </Banner>
        {:else}
          <Banner variant="neutral" role="status"
            >{t('mcp.catalogPreparing')}</Banner
          >
        {/if}
      {:else}
        <Banner variant="success" role="status"
          >{target.oauth
            ? t('mcp.catalogSignedIn', { name: target.name })
            : t('mcp.catalogAdded', { name: target.name })}</Banner
        >
        <p>{t('mcp.catalogGrantHelp', { name: target.name })}</p>
        {#if access === null}
          {#if !error}
            <Banner variant="neutral" role="status"
              >{t('common.loading')}</Banner
            >
          {/if}
        {:else if !access.length}
          <p>{t('mcp.catalogNoAgents')}</p>
        {:else}
          <ul class="mcp-catalog__agents">
            {#each access as item (item.agent.id)}
              <li>
                <Checkbox
                  checked={item.granted || chosen.has(item.agent.id)}
                  disabled={busy || item.granted}
                  onChange={(checked) => {
                    if (checked) chosen.add(item.agent.id);
                    else chosen.delete(item.agent.id);
                  }}>{item.agent.name || item.agent.id}</Checkbox
                >
                {#if item.granted}
                  <Badge variant="neutral">{t('mcp.catalogHasAccess')}</Badge>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      {/if}
    </div>
  {/snippet}
  {#snippet footer()}
    {#if view === 'grant'}
      <Button variant="secondary" disabled={busy} onClick={finish}
        >{t('mcp.catalogSkip')}</Button
      >
      <Button
        variant="primary"
        disabled={busy || !grantable.length}
        loading={busy}
        onClick={grant}>{t('mcp.catalogGrant')}</Button
      >
    {:else}
      <Button variant="secondary" disabled={busy} onClick={onClose}
        >{t('common.close')}</Button
      >
    {/if}
  {/snippet}
</Modal>
