<script>
  // Connects a hosted MCP service from the MCP Extension's catalog in one
  // dialog: choose a service and Connect, sign in to it in the browser when
  // it needs an account, then choose the Agents that may use it. Opened for
  // an existing connection, it starts at that connection's waiting sign-in.
  import { onMount, tick, untrack } from 'svelte';
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
    // Bumped when the Agents change; the grant step lists them.
    agentsRefreshToken = 0,
  } = $props();

  const componentId = $props.id();
  const titleId = `${componentId}-title`;
  let view = $state('browse');
  let entries = $state(null);
  let loadError = $state('');
  let query = $state('');
  let category = $state('');
  const readOnly = new SvelteSet();
  let busy = $state(false);
  let error = $state('');
  // The connection being set up: its id, the service's name (`label`; null
  // while it is looked up, empty when the catalog has no entry for it),
  // whether it signs in with OAuth, and the ids of the saved connections
  // that already reached the same service.
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
  // The newest Agent list read; an older one never replaces its result.
  let agentsRead = 0;
  let lastAgentsRefreshToken = null;

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
  // What the dialog calls the connection: the service's name, else the
  // connection's description, else its id.
  let targetName = $derived(
    !target
      ? ''
      : target.label === ''
        ? status?.configuration?.description || target.id
        : (target.label ?? target.id),
  );
  let title = $derived(
    view === 'signin'
      ? t('mcp.signInFor', { name: targetName })
      : view === 'grant'
        ? t('mcp.catalogGrantTitle', { name: targetName })
        : t('mcp.catalogTitle'),
  );
  let grantable = $derived(
    (access ?? []).filter((item) => chosen.has(item.agent.id)),
  );

  onMount(() => {
    const id = untrack(() => signInConnection);
    if (id) {
      target = { id, label: null, oauth: true, siblings: [] };
      view = 'signin';
      void readStatus();
      void nameConnection(id);
    } else void loadCatalog();
    return () => {
      stopped = true;
    };
  });
  $effect(() => subscribeInvalidations?.(onInvalidation));
  // The listed Agents follow their changes, such as a new name; the Agents
  // chosen for access stay chosen.
  $effect(() => {
    const token = agentsRefreshToken;
    if (lastAgentsRefreshToken === null) {
      lastAgentsRefreshToken = token;
      return;
    }
    if (token === lastAgentsRefreshToken) return;
    lastAgentsRefreshToken = token;
    if (untrack(() => access) !== null) void rereadAgents();
  });
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

  // Looks up the service the connection `id` reaches: the catalog entry
  // whose saved connections include it (they match by URL).
  async function nameConnection(id) {
    let entry = null;
    try {
      const { entries: listed } = await extensionOperation('mcp', 'catalog');
      entry = listed.find((item) => item.connections.includes(id)) ?? null;
    } catch {
      // Without the catalog, the connection's own description names it.
    }
    if (stopped || target?.id !== id) return;
    target = entry
      ? {
          ...target,
          label: serviceLabel(entry, id),
          siblings: entry.connections.filter((other) => other !== id),
        }
      : { ...target, label: '' };
  }

  // The service's name, with the connection id when it differs from the
  // entry's id (a second connection to the same service).
  function serviceLabel(entry, id) {
    return entry.id === id
      ? entry.name
      : t('mcp.catalogServiceAs', { name: entry.name, id });
  }

  // Shows another step and moves focus to its title, which names the step
  // for screen readers; the control that had focus is gone with the step.
  async function showStep(next) {
    view = next;
    await tick();
    const heading = document.getElementById(titleId);
    if (!heading || stopped) return;
    heading.setAttribute('tabindex', '-1');
    heading.focus();
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
        label: serviceLabel(entry, result.id),
        oauth: entry.auth === 'oauth',
        siblings: entry.connections,
      };
      if (!target.oauth) {
        await openGrant();
        return;
      }
      void showStep('signin');
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
    void showStep('grant');
    error = '';
    access = null;
    const current = ++agentsRead;
    try {
      const [agentsResult, toolsResult] = await Promise.all([
        listAgents(),
        listTools(),
      ]);
      if (stopped || current !== agentsRead) return;
      tools = toolsResult.tools;
      access = mcpAgentAccess(
        agentsResult.agents,
        tools,
        target.id,
        target.siblings,
      );
    } catch (failure) {
      if (!stopped && current === agentsRead) error = failure.message;
    }
  }

  async function rereadAgents() {
    const current = ++agentsRead;
    try {
      const result = await listAgents();
      if (stopped || current !== agentsRead) return;
      access = mcpAgentAccess(result.agents, tools, target.id, target.siblings);
    } catch {
      // The listed Agents stay until their next change.
    }
  }

  async function grant() {
    busy = true;
    error = '';
    try {
      for (const { agent } of grantable) {
        await updateAgent({
          id: agent.id,
          tool_access: mcpGrantedToolAccess(agent, tools, target.id),
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
  labelledById={titleId}
  closeDisabled={busy}
  onClose={busy ? noop : onClose}
  class={view === 'browse'
    ? 'mcp-modal mcp-catalog-modal mcp-catalog-modal--browse'
    : 'mcp-modal mcp-catalog-modal'}
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
                    style:background={mark.background}
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
                    <Badge variant="success">{t('mcp.catalogConnected')}</Badge>
                  {/if}
                </div>
                <p class="mcp-catalog__description">{entry.description}</p>
                {#if entry.connections.length}
                  <p class="mcp-catalog__note">
                    {t('mcp.catalogConnectedAs', {
                      ids: entry.connections.join(', '),
                    })}
                  </p>
                {/if}
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
        <p>{t('mcp.catalogSignInHelp', { name: targetName })}</p>
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
            ? t('mcp.catalogSignedIn', { name: targetName })
            : t('mcp.catalogAdded', { name: targetName })}</Banner
        >
        <p>{t('mcp.catalogGrantHelp', { name: targetName })}</p>
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
            {#each access as item, index (item.agent.id)}
              {@const usesId = `${componentId}-uses-${index}`}
              {@const usesOther = !item.granted && item.alsoUses.length > 0}
              <li>
                <Checkbox
                  checked={item.granted || chosen.has(item.agent.id)}
                  disabled={busy || item.granted}
                  aria-describedby={usesOther ? usesId : undefined}
                  onChange={(checked) => {
                    if (checked) chosen.add(item.agent.id);
                    else chosen.delete(item.agent.id);
                  }}>{item.agent.name || item.agent.id}</Checkbox
                >
                {#if item.granted}
                  <Badge variant="neutral">{t('mcp.catalogHasAccess')}</Badge>
                {:else if usesOther}
                  <span id={usesId} class="mcp-catalog__note"
                    >{t('mcp.catalogUsesOther', {
                      ids: item.alsoUses.join(', '),
                    })}</span
                  >
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
