<script>
  import McpConnectionEditor from './McpConnectionEditor.svelte';
  import McpInspector from './McpInspector.svelte';
  import './mcp.css';
  import { onMount, onDestroy, tick } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';
  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import FormField from '../ui/FormField.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Modal from '../ui/Modal.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';

  import Toggle from '../ui/Toggle.svelte';
  import McpCatalogDialog from './McpCatalogDialog.svelte';
  import McpConnectionDiagnostics from './McpConnectionDiagnostics.svelte';
  import McpImportDialog from './McpImportDialog.svelte';

  import { t } from '$lib/i18n.js';
  import {
    createMcpSettings,
    mcpDraft,
    mcpCredentialNames,
    mcpProblemText,
    mcpSetupNeeded,
  } from '$lib/mcpSettings.js';
  import { tooltip } from '$lib/tooltip.js';

  const componentId = $props.id();
  let {
    // The App's Extension invalidations; the connections refresh on their
    // changes instead of on a timer.
    subscribeInvalidations = null,
    // Bumped when the Agents change; the catalog dialog lists Agents.
    agentsRefreshToken = 0,
  } = $props();
  let state = $state({
    connections: [],
    loading: true,
    busy: false,
    error: '',
    notice: '',
    job: null,
    inspector: null,
  });
  let draft = $state(null);
  let original = $state(null);
  let removal = $state(null);
  let secretConnection = $state(null);
  let secretKey = $state('');
  let secretValue = $state('');
  // The import dialog's starting text while it is open, otherwise null.
  let importSource = $state(null);
  let fillNotes = $state([]);
  // The catalog dialog while it is open, otherwise null: `signIn` names the
  // connection whose waiting sign-in it opens at instead of the catalog.
  let catalog = $state(null);
  // Each connection is one collapsed row; its endpoint, catalog counts and
  // actions open in its details. The details stay in the DOM so settings
  // search still matches them.
  const expanded = new SvelteSet();
  const controller = createMcpSettings({
    onChange: (next) => {
      state = next;
    },
  });
  let blocked = $derived(state.busy || Boolean(state.job));
  let dialogOpen = $derived(
    Boolean(draft || secretConnection || catalog) || importSource !== null,
  );
  onMount(() => {
    void controller.refresh();
  });
  $effect(() => subscribeInvalidations?.(controller.handleInvalidation));
  onDestroy(() => controller.dispose());

  function edit(connection = null) {
    original = connection
      ? JSON.parse(JSON.stringify(connection.configuration))
      : null;
    draft = mcpDraft(original);
    fillNotes = [];
  }
  // Fills the new connection from a command line or URL; setup text with
  // several servers or a secret to store goes to the import dialog.
  async function quickFill(text) {
    const { servers } = await controller.previewImport(text);
    const [server] = servers;
    if (
      servers.length !== 1 ||
      server.error ||
      server.credentials.some((credential) => credential.state === 'provided')
    ) {
      draft = null;
      importSource = text;
      return;
    }
    // What the user already typed into the form stays.
    draft = {
      ...mcpDraft({ ...server.connection, enabled: server.enabled }),
      id: String(draft.id ?? '').trim() || server.id,
      description: draft.description || server.connection.description || '',
    };
    fillNotes = [
      ...server.warnings.map((warning) => warning.message),
      ...server.credentials
        .filter((credential) => credential.state === 'missing')
        .map((credential) =>
          t('mcp.quickFillCredential', {
            name: credential.name,
            target: credential.target,
          }),
        ),
    ];
  }
  function imported(ids) {
    importSource = null;
    for (const id of ids) expanded.add(id);
  }
  // The catalog dialog set up the connection `id`: its row opens and takes
  // focus, since the control that opened the dialog may be gone with it.
  async function connected(id) {
    catalog = null;
    expanded.add(id);
    await tick();
    const index = state.connections.findIndex(
      (connection) => connection.id === id,
    );
    if (index >= 0)
      document
        .querySelector(`[aria-controls="${componentId}-details-${index}"]`)
        ?.focus();
  }
  // A connection that waits for the user's browser sign-in.
  function waitsForSignIn(connection) {
    return (connection.pending_requests ?? []).some(
      (request) => request.kind === 'oauth',
    );
  }
  async function save(event) {
    event.preventDefault();
    const createdId = original ? '' : String(draft.id ?? '').trim();
    if (await controller.save(draft, original)) {
      draft = null;
      // A new connection opens its details, where it can be tested.
      if (createdId) expanded.add(createdId);
    }
  }
  function toggleDetails(id) {
    if (expanded.has(id)) expanded.delete(id);
    else expanded.add(id);
  }
  // The row head toggles its details like a Provider row; the chevron button
  // stays the keyboard and screen-reader control.
  function handleHeadClick(event, id) {
    if (
      event.target.closest('button, a, input, select, textarea') ||
      window.getSelection()?.toString()
    )
      return;
    toggleDetails(id);
  }
  function openCredentials(connection) {
    secretConnection = connection;
    secretKey = mcpCredentialNames(connection.configuration)[0] ?? '';
    secretValue = '';
  }
  function closeCredentials() {
    secretConnection = null;
    secretValue = '';
  }
  async function saveCredential(value) {
    const saved = await controller.credential(
      secretConnection.id,
      secretKey,
      value,
    );
    if (saved) closeCredentials();
  }
  async function remove() {
    const id = removal.id;
    removal = null;
    await controller.mutate('remove', id);
  }
  // The run status of an enabled connection; a disabled one needs no chip
  // because its switch already says so.
  function status(connection) {
    if (!connection.configuration.enabled) return null;
    const states = {
      connected: { label: t('mcp.connected'), variant: 'success' },
      connecting: { label: t('mcp.connecting'), variant: 'warn' },
      failed: { label: t('mcp.failed'), variant: 'error' },
      disconnected: {
        label: t('mcp.disconnected'),
        variant: 'neutral',
      },
    };
    return (
      states[connection.state] ?? {
        label: connection.state,
        variant: 'neutral',
      }
    );
  }
</script>

<!-- A sub-topic of the Extensions section: the MCP Extension's connections
     under a sub-heading, as one group of connection rows. -->
<section class="mcp-panel" aria-label={t('mcp.title')}>
  <div class="s-subhead mcp-subhead">
    <div class="mcp-subhead__title">
      <h4 class="s-subhead__title">{t('mcp.title')}</h4>
      <InfoHint text={t('mcp.help')} ariaLabel={t('mcp.helpAria')} />
    </div>
    <div class="mcp-subhead__actions">
      <Button
        variant="tertiary"
        disabled={blocked || state.loading}
        onClick={() => {
          catalog = { signIn: null };
        }}>{t('mcp.catalog')}</Button
      >
      <Button
        variant="tertiary"
        disabled={blocked || state.loading}
        onClick={() => {
          importSource = '';
        }}>{t('mcp.import')}</Button
      >
      <Button
        variant="secondary"
        disabled={blocked || state.loading}
        onClick={() => edit()}>{t('mcp.add')}</Button
      >
    </div>
  </div>
  {#if state.error && !dialogOpen}
    <Banner variant="error" role="alert">
      {state.error}
      <Button
        variant="secondary"
        disabled={state.busy}
        onClick={controller.refresh}>{t('common.retry')}</Button
      >
    </Banner>
  {/if}
  {#if state.notice && !dialogOpen}<Banner variant="success" role="status"
      >{state.notice}</Banner
    >{/if}
  {#if state.job}
    <Banner variant="warn" role="status">
      {t('mcp.testing', { name: state.job.connection })}
      <Button
        variant="secondary"
        disabled={state.busy}
        onClick={controller.cancel}>{t('mcp.cancelTest')}</Button
      >
    </Banner>
  {/if}
  {#if state.loading}
    <Banner variant="neutral">{t('common.loading')}</Banner>
  {:else if !state.connections.length && !state.error}
    <EmptyState
      density="compact"
      title={t('mcp.empty')}
      description={t('mcp.emptyHint')}
    >
      {#snippet actions()}
        <Button
          variant="primary"
          disabled={blocked}
          onClick={() => {
            catalog = { signIn: null };
          }}>{t('mcp.catalogBrowse')}</Button
        >
      {/snippet}
    </EmptyState>
  {:else}
    <div class="s-group mcp-connections">
      {#each state.connections as connection, index (connection.id)}
        <!-- Missing credentials need the user first, whether or not the
             connection is enabled; the waiting line names them. -->
        {@const setup = mcpSetupNeeded(connection)}
        {@const appearance = setup
          ? { label: setup.hint, variant: 'warn' }
          : status(connection)}
        {@const open = expanded.has(connection.id)}
        {@const description = connection.configuration.description ?? ''}
        <article class="mcp-connection s-entity" aria-label={connection.id}>
          <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions (pointer shortcut for the row; the details button is the keyboard control) -->
          <div
            class="s-entity__head s-entity__head--toggle"
            onclick={(event) => handleHeadClick(event, connection.id)}
          >
            <div class="s-row-info">
              <div class="s-row-label mcp-connection__name">
                {connection.id}
              </div>
              {#if description}
                <div
                  class="s-row-desc mcp-connection__desc"
                  class:mcp-connection__desc--full={open}
                  use:tooltip={{ text: description, whenTruncated: true }}
                >
                  {description}
                </div>
              {/if}
              {#if connection.error && !(setup && connection.problem?.code === 'credential_missing')}
                <div class="s-row-desc mcp-connection__error">
                  {mcpProblemText(connection.problem) || connection.error}
                </div>
              {/if}
              {#if setup}
                <div class="s-row-desc mcp-connection__waiting">
                  {setup.waitingFor}
                </div>
              {/if}
            </div>
            <div class="s-entity__end">
              {#if waitsForSignIn(connection)}
                <Button
                  variant="secondary"
                  ariaLabel={t('mcp.signInFor', { name: connection.id })}
                  onClick={() => {
                    catalog = { signIn: connection.id };
                  }}>{t('mcp.signIn')}</Button
                >
              {/if}
              {#if appearance}
                <StatusChip variant={appearance.variant}
                  >{appearance.label}</StatusChip
                >
              {/if}
              <Toggle
                checked={connection.configuration.enabled}
                disabled={blocked}
                ariaLabel={t('mcp.enabledFor', {
                  name: connection.id,
                })}
                onChange={(enabled) =>
                  controller.mutate(
                    enabled ? 'enable' : 'disable',
                    connection.id,
                  )}
              />
              <Button
                variant="tertiary"
                icon
                class="s-disclosure-btn"
                aria-controls={`${componentId}-details-${index}`}
                ariaLabel={t('mcp.detailsAria', { name: connection.id })}
                aria-expanded={open}
                onClick={() => toggleDetails(connection.id)}
              >
                <span
                  class="disclosure-chevron"
                  class:disclosure-chevron--open={open}
                  aria-hidden="true"
                ></span>
              </Button>
            </div>
          </div>
          <div
            id={`${componentId}-details-${index}`}
            class="s-disclosure-sub mcp-connection__details"
            hidden={!open}
          >
            <div class="mcp-facts">
              <p class="mcp-endpoint">
                {connection.configuration.transport === 'stdio'
                  ? connection.configuration.command
                  : connection.configuration.url}
              </p>
              {#if connection.oauth}
                <p>
                  {connection.oauth.signed_in
                    ? t('mcp.signedIn')
                    : t('mcp.signedOut')}
                </p>
                <p class="mcp-redirect">
                  {t('mcp.redirectUri', { uri: connection.oauth.redirect_uri })}
                </p>
              {/if}
              {#if connection.counts}
                <p class="mcp-catalog-counts">
                  {t('mcp.catalogCounts', {
                    tools: connection.counts.tools ?? 0,
                    resources:
                      (connection.counts.resources ?? 0) +
                      (connection.counts.resource_templates ?? 0),
                    prompts: connection.counts.prompts ?? 0,
                  })}
                </p>
              {/if}
              <McpConnectionDiagnostics {connection} />
            </div>
            <div class="mcp-actions">
              <Button
                variant="secondary"
                onClick={() => {
                  void controller.inspect(connection.id);
                }}>{t('mcp.capabilities')}</Button
              >
              <Button
                variant="tertiary"
                disabled={blocked}
                onClick={() => edit(connection)}>{t('common.edit')}</Button
              >
              <Button
                variant="tertiary"
                disabled={blocked || !connection.configuration.enabled}
                onClick={() => controller.test(connection.id)}
                >{t('mcp.test')}</Button
              >
              <Button
                variant="tertiary"
                disabled={blocked || !connection.configuration.enabled}
                tooltip={t('mcp.reconnectHelp')}
                onClick={() => controller.mutate('reconnect', connection.id)}
                >{t('mcp.reconnect')}</Button
              >
              {#if mcpCredentialNames(connection.configuration).length}
                <Button
                  variant="tertiary"
                  disabled={blocked}
                  onClick={() => openCredentials(connection)}
                  >{t('mcp.credentials')}</Button
                >
              {/if}
              {#if connection.configuration.oauth}
                <Button
                  variant="tertiary"
                  disabled={blocked}
                  onClick={() =>
                    controller.mutate('reauthorize', connection.id)}
                  >{t('mcp.reauthorize')}</Button
                >
              {/if}
              <Button
                variant="danger"
                class="mcp-actions__remove"
                disabled={blocked}
                onClick={() => {
                  removal = connection;
                }}>{t('common.remove')}</Button
              >
            </div>
          </div>
        </article>
      {/each}
    </div>
  {/if}
</section>

{#if state.inspector}
  <McpInspector {state} {controller} {status} />
{/if}

{#if draft}
  <McpConnectionEditor
    bind:draft
    {original}
    {state}
    {fillNotes}
    {quickFill}
    {save}
  />
{/if}

{#if secretConnection}
  <Modal
    title={t('mcp.credentialsFor', {
      name: secretConnection.id,
    })}
    closeDisabled={state.busy}
    onClose={closeCredentials}
    class="mcp-credentials-modal"
  >
    {#snippet body()}
      <form
        id={`${componentId}-secret`}
        class="modal-body mcp-editor"
        onsubmit={(event) => {
          event.preventDefault();
          void saveCredential(secretValue);
        }}
      >
        {#if state.error}<Banner variant="error" role="alert"
            >{state.error}</Banner
          >{/if}
        <FormField
          controlId={`${componentId}-key`}
          label={t('mcp.credentialName')}
        >
          {#snippet children(field)}<Dropdown
              id={field.controlId}
              ariaLabelledby={field.labelId}
              value={secretKey}
              options={mcpCredentialNames(secretConnection.configuration)}
              disabled={state.busy}
              ariaLabel={t('mcp.credentialName')}
              onValueChange={(value) => {
                secretKey = value;
                secretValue = '';
              }}
            />{/snippet}
        </FormField>
        <FormField
          controlId={`${componentId}-value`}
          label={t('mcp.secretValue')}
          help={t('mcp.secretHelp')}
        >
          {#snippet children(field)}<TextField
              id={field.controlId}
              aria-describedby={field.describedBy}
              type="password"
              autocomplete="off"
              value={secretValue}
              disabled={state.busy}
              onInput={(value) => {
                secretValue = value;
              }}
            />{/snippet}
        </FormField>
      </form>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="danger"
        disabled={state.busy}
        onClick={() => saveCredential('')}>{t('mcp.clearCredential')}</Button
      >
      <Button
        variant="secondary"
        disabled={state.busy}
        onClick={closeCredentials}>{t('common.cancel')}</Button
      >
      <Button
        variant="primary"
        type="submit"
        form={`${componentId}-secret`}
        disabled={state.busy || !secretValue}>{t('mcp.saveCredential')}</Button
      >
    {/snippet}
  </Modal>
{/if}
{#if catalog}
  <McpCatalogDialog
    {subscribeInvalidations}
    {agentsRefreshToken}
    signInConnection={catalog.signIn}
    onClose={() => {
      catalog = null;
    }}
    onConnected={connected}
  />
{/if}
{#if importSource !== null}
  <McpImportDialog
    {controller}
    busy={state.busy}
    error={state.error}
    initialSource={importSource}
    onClose={() => {
      importSource = null;
    }}
    onImported={imported}
  />
{/if}
{#if removal}
  <ConfirmDialog
    title={t('mcp.removeTitle', { name: removal.id })}
    body={t('mcp.removeBody')}
    confirmLabel={t('common.remove')}
    onConfirm={remove}
    onCancel={() => {
      removal = null;
    }}
  />
{/if}
