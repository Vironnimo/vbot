<script>
  import { onMount, onDestroy } from 'svelte';
  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { t } from '$lib/i18n.js';
  import {
    createMcpSettings,
    mcpDraft,
    mcpCredentialNames,
  } from '$lib/mcpSettings.js';

  const componentId = $props.id();
  let state = $state({
    connections: [],
    targets: [],
    loading: true,
    busy: false,
    error: '',
    targetError: '',
    notice: '',
    job: null,
  });
  let draft = $state(null);
  let original = $state(null);
  let removal = $state(null);
  let secretConnection = $state(null);
  let secretKey = $state('');
  let secretValue = $state('');
  const controller = createMcpSettings({
    onChange: (next) => {
      state = next;
    },
  });
  let blocked = $derived(state.busy || Boolean(state.job));
  let targets = $derived([
    ...state.targets,
    ...(draft?.agents ?? [])
      .filter(
        (address) => !state.targets.some((target) => target.value === address),
      )
      .map((address) => ({
        value: address,
        label: address,
        secondaryLabel: t('mcp.savedGrant', 'Saved grant'),
      })),
  ]);
  let transportOptions = $derived([
    { value: 'stdio', label: t('mcp.local', 'Local program') },
    { value: 'http', label: t('mcp.http', 'Server URL (HTTP)') },
    { value: 'sse', label: t('mcp.sse', 'Server URL (legacy SSE)') },
  ]);
  let mappingFields = $derived([
    {
      key: 'environment',
      label: t('mcp.environment', 'Environment variables'),
      value: t('mcp.value', 'Value (non-secret)'),
    },
    {
      key: 'credential_environment',
      label: t(
        'mcp.credentialEnvironment',
        'Credentials for environment variables',
      ),
      value: t('mcp.credentialName', 'Credential name'),
    },
    {
      key: 'credential_headers',
      label: t('mcp.credentialHeaders', 'Credentials for HTTP headers'),
      value: t('mcp.credentialName', 'Credential name'),
    },
  ]);

  onMount(() => {
    void controller.refresh();
  });
  onDestroy(() => controller.dispose());

  function edit(connection = null) {
    original = connection
      ? JSON.parse(JSON.stringify(connection.configuration))
      : null;
    draft = mcpDraft(original);
    void controller.loadTargets();
  }
  function set(field, value) {
    draft = { ...draft, [field]: value };
  }
  function grant(address, enabled) {
    set(
      'agents',
      enabled
        ? [...draft.agents, address]
        : draft.agents.filter((item) => item !== address),
    );
  }
  function setMapping(field, index, part, value) {
    set(
      field,
      draft[field].map((entry, position) =>
        position === index ? { ...entry, [part]: value } : entry,
      ),
    );
  }
  async function save(event) {
    event.preventDefault();
    if (await controller.save(draft, original)) draft = null;
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
  function status(connection) {
    if (!connection.configuration.enabled)
      return { label: t('mcp.disabled', 'Disabled'), variant: 'neutral' };
    const states = {
      connected: { label: t('mcp.connected', 'Connected'), variant: 'success' },
      connecting: { label: t('mcp.connecting', 'Connecting'), variant: 'warn' },
      failed: { label: t('mcp.failed', 'Connection failed'), variant: 'error' },
      disconnected: {
        label: t('mcp.disconnected', 'Disconnected'),
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

<section class="mcp-panel" aria-label={t('mcp.title', 'MCP connections')}>
  <div class="mcp-heading">
    <div>
      <h3>{t('mcp.title', 'MCP connections')}</h3>
      <p>
        {t(
          'mcp.host',
          'Programs and application add-ons run on the machine hosting vBot.',
        )}
      </p>
    </div>
    <Button
      variant="primary"
      disabled={blocked || state.loading}
      onClick={() => edit()}>{t('mcp.add', 'Add MCP connection')}</Button
    >
  </div>
  {#if state.error && !draft && !secretConnection}
    <Banner variant="error" role="alert">
      {state.error}
      <Button
        variant="secondary"
        disabled={state.busy}
        onClick={controller.refresh}>{t('common.retry', 'Retry')}</Button
      >
    </Banner>
  {/if}
  {#if state.notice && !draft && !secretConnection}<Banner
      variant="success"
      role="status">{state.notice}</Banner
    >{/if}
  {#if state.job}
    <Banner variant="warn" role="status">
      {t(
        'mcp.testing',
        'Testing {name}. Complete any sign-in or question shown by vBot.',
        { name: state.job.connection },
      )}
      <Button
        variant="secondary"
        disabled={state.busy}
        onClick={controller.cancel}>{t('mcp.cancelTest', 'Cancel test')}</Button
      >
    </Banner>
  {/if}
  {#if state.loading}
    <Banner variant="neutral">{t('common.loading', 'Loading…')}</Banner>
  {:else if !state.connections.length && !state.error}
    <EmptyState
      density="compact"
      title={t('mcp.empty', 'No MCP connections yet')}
      description={t(
        'mcp.emptyHint',
        'Add a local program or a server URL from your MCP setup instructions.',
      )}
    />
  {:else}
    <div class="mcp-connections">
      {#each state.connections as connection (connection.id)}
        {@const appearance = status(connection)}
        <article class="mcp-connection" aria-label={connection.id}>
          <div class="mcp-heading">
            <div class="mcp-identity">
              <strong>{connection.id}</strong><StatusChip
                variant={appearance.variant}>{appearance.label}</StatusChip
              >
            </div>
            <Toggle
              checked={connection.configuration.enabled}
              disabled={blocked}
              ariaLabel={t('mcp.enabledFor', 'Enable {name}', {
                name: connection.id,
              })}
              onChange={(enabled) =>
                controller.mutate(
                  enabled ? 'enable' : 'disable',
                  connection.id,
                )}
            />
          </div>
          <p class="mcp-endpoint">
            {connection.configuration.transport === 'stdio'
              ? connection.configuration.command
              : connection.configuration.url}
          </p>
          <p>
            {connection.configuration.agents.length
              ? t('mcp.granted', 'Agents: {agents}', {
                  agents: connection.configuration.agents.join(', '),
                })
              : t('mcp.noGrants', 'No Agents have access.')}
          </p>
          {#if connection.error}<Banner variant="error"
              >{connection.error}</Banner
            >{/if}
          <div class="mcp-actions">
            <Button
              variant="secondary"
              disabled={blocked}
              onClick={() => edit(connection)}
              >{t('common.edit', 'Edit')}</Button
            >
            <Button
              variant="secondary"
              disabled={blocked || !connection.configuration.enabled}
              onClick={() => controller.test(connection.id)}
              >{t('mcp.test', 'Test connection')}</Button
            >
            <Button
              variant="secondary"
              disabled={blocked ||
                !mcpCredentialNames(connection.configuration).length}
              onClick={() => openCredentials(connection)}
              >{t('mcp.credentials', 'Credentials')}</Button
            >
            <Button
              variant="danger"
              disabled={blocked}
              onClick={() => {
                removal = connection;
              }}>{t('common.remove', 'Remove')}</Button
            >
          </div>
        </article>
      {/each}
    </div>
  {/if}
</section>

{#if draft}
  <Modal
    title={original
      ? t('mcp.edit', 'Edit MCP connection')
      : t('mcp.add', 'Add MCP connection')}
    closeDisabled={state.busy}
    onClose={() => {
      draft = null;
    }}
    class="mcp-modal"
  >
    {#snippet body()}
      <form
        id={`${componentId}-form`}
        class="modal-body mcp-editor"
        onsubmit={save}
      >
        {#if state.error}<Banner variant="error" role="alert"
            >{state.error}</Banner
          >{/if}
        <div class="mcp-grid">
          <FormField
            controlId={`${componentId}-name`}
            label={t('mcp.name', 'Connection name')}
            required
            help={t(
              'mcp.nameHelp',
              'Lowercase letters, numbers and underscores; start with a letter.',
            )}
          >
            {#snippet children(field)}<TextField
                id={field.controlId}
                aria-describedby={field.describedBy}
                value={draft.id}
                disabled={state.busy || Boolean(original)}
                required
                pattern={'[a-z][a-z0-9_]{0,31}'}
                onInput={(value) => set('id', value)}
              />{/snippet}
          </FormField>
          <FormField
            controlId={`${componentId}-transport`}
            label={t('mcp.connectionType', 'Connection type')}
          >
            {#snippet children(field)}<Dropdown
                id={field.controlId}
                value={draft.transport}
                options={transportOptions}
                disabled={state.busy}
                ariaLabel={t('mcp.connectionType', 'Connection type')}
                onValueChange={(value) => set('transport', value)}
              />{/snippet}
          </FormField>
        </div>
        {#if draft.transport === 'stdio'}
          <FormField
            controlId={`${componentId}-command`}
            label={t('mcp.program', 'Program')}
            required
            help={t(
              'mcp.programHelp',
              'Executable on the vBot host, for example uvx, npx or an absolute path.',
            )}
          >
            {#snippet children(field)}<TextField
                id={field.controlId}
                aria-describedby={field.describedBy}
                value={draft.command}
                disabled={state.busy}
                required
                onInput={(value) => set('command', value)}
              />{/snippet}
          </FormField>
          <div class="mcp-group">
            <h4>{t('mcp.arguments', 'Arguments')}</h4>
            {#each draft.args as argument, index (index)}
              <div class="mcp-entry">
                <FormField
                  controlId={`${componentId}-arg-${index}`}
                  label={t('mcp.argument', 'Argument {number}', {
                    number: index + 1,
                  })}
                >
                  {#snippet children(field)}<TextField
                      id={field.controlId}
                      value={argument}
                      disabled={state.busy}
                      onInput={(value) =>
                        set(
                          'args',
                          draft.args.map((item, position) =>
                            position === index ? value : item,
                          ),
                        )}
                    />{/snippet}
                </FormField>
                <Button
                  variant="tertiary"
                  disabled={state.busy}
                  ariaLabel={t(
                    'mcp.removeArgument',
                    'Remove argument {number}',
                    { number: index + 1 },
                  )}
                  onClick={() =>
                    set(
                      'args',
                      draft.args.filter((_, position) => position !== index),
                    )}>{t('common.remove', 'Remove')}</Button
                >
              </div>
            {/each}
            <Button
              variant="secondary"
              disabled={state.busy}
              onClick={() => set('args', [...draft.args, ''])}
              >{t('mcp.addArgument', 'Add argument')}</Button
            >
          </div>
        {:else}
          <FormField
            controlId={`${componentId}-url`}
            label={t('mcp.url', 'Server URL')}
            required
          >
            {#snippet children(field)}<TextField
                id={field.controlId}
                type="url"
                value={draft.url}
                required
                disabled={state.busy}
                onInput={(value) => set('url', value)}
              />{/snippet}
          </FormField>
          <FormField
            controlId={`${componentId}-oauth`}
            label={t('mcp.oauth', 'Sign in with OAuth')}
          >
            {#snippet children(field)}<Toggle
                id={field.controlId}
                checked={draft.oauth}
                disabled={state.busy}
                ariaLabel={t('mcp.oauth', 'Sign in with OAuth')}
                onChange={(value) => set('oauth', value)}
              />{/snippet}
          </FormField>
        {/if}
        <div class="mcp-group">
          <h4>{t('mcp.agentAccess', 'Agent access')}</h4>
          <p>
            {t(
              'mcp.accessHelp',
              'Select who may use this connection. Existing Tool restrictions and Project limits still apply.',
            )}
          </p>
          {#if state.targetError}<Banner variant="warn"
              >{state.targetError}<Button
                variant="secondary"
                onClick={controller.loadTargets}
                >{t('common.retry', 'Retry')}</Button
              ></Banner
            >{/if}
          <div class="mcp-grants">
            {#each targets as target (target.value)}
              <div class="mcp-grant">
                <span
                  >{target.label}<small
                    >{target.secondaryLabel !== target.label
                      ? target.secondaryLabel
                      : ''}</small
                  ></span
                ><Toggle
                  checked={draft.agents.includes(target.value)}
                  disabled={state.busy}
                  ariaLabel={t('mcp.allowAgent', 'Allow {agent}', {
                    agent: target.value,
                  })}
                  onChange={(enabled) => grant(target.value, enabled)}
                />
              </div>
            {/each}
          </div>
          {#if !targets.length && !state.targetError}<p>
              {t(
                'mcp.noAgents',
                'No Agents available yet. You can grant access later.',
              )}
            </p>{/if}
        </div>
        <details class="mcp-advanced">
          <summary>{t('mcp.advanced', 'Advanced settings')}</summary>
          <div class="mcp-editor">
            <div class="mcp-grid">
              <FormField
                controlId={`${componentId}-timeout`}
                label={t('mcp.timeout', 'Timeout (seconds)')}
              >
                {#snippet children(field)}<TextField
                    id={field.controlId}
                    type="number"
                    min="0.001"
                    max="86400"
                    step="any"
                    required
                    value={draft.timeout}
                    disabled={state.busy}
                    onInput={(value) => set('timeout', value)}
                  />{/snippet}
              </FormField>
              <FormField
                controlId={`${componentId}-enabled`}
                label={t('mcp.enabled', 'Enabled')}
              >
                {#snippet children(field)}<Toggle
                    id={field.controlId}
                    checked={draft.enabled}
                    disabled={state.busy}
                    ariaLabel={t('mcp.enabled', 'Enabled')}
                    onChange={(value) => set('enabled', value)}
                  />{/snippet}
              </FormField>
            </div>
            {#if draft.transport === 'stdio'}
              <FormField
                controlId={`${componentId}-cwd`}
                label={t('mcp.directory', 'Working directory')}
              >
                {#snippet children(field)}<TextField
                    id={field.controlId}
                    value={draft.cwd}
                    disabled={state.busy}
                    onInput={(value) => set('cwd', value)}
                  />{/snippet}
              </FormField>
            {:else if draft.oauth}
              <FormField
                controlId={`${componentId}-redirect`}
                label={t('mcp.redirect', 'OAuth redirect URL (optional)')}
              >
                {#snippet children(field)}<TextField
                    id={field.controlId}
                    type="url"
                    value={draft.oauth_redirect_uri}
                    disabled={state.busy}
                    onInput={(value) => set('oauth_redirect_uri', value)}
                  />{/snippet}
              </FormField>
            {/if}
            {#each mappingFields as mapping (mapping.key)}
              <div class="mcp-group">
                <h4>{mapping.label}</h4>
                {#each draft[mapping.key] as entry, index (index)}
                  <div class="mcp-entry mcp-mapping">
                    <FormField
                      controlId={`${componentId}-${mapping.key}-${index}-name`}
                      label={t('mcp.entryName', 'Name')}
                    >
                      {#snippet children(field)}<TextField
                          id={field.controlId}
                          value={entry.name}
                          required
                          disabled={state.busy}
                          onInput={(value) =>
                            setMapping(mapping.key, index, 'name', value)}
                        />{/snippet}
                    </FormField>
                    <FormField
                      controlId={`${componentId}-${mapping.key}-${index}-value`}
                      label={mapping.value}
                    >
                      {#snippet children(field)}<TextField
                          id={field.controlId}
                          value={entry.value}
                          required={mapping.key !== 'environment'}
                          disabled={state.busy}
                          onInput={(value) =>
                            setMapping(mapping.key, index, 'value', value)}
                        />{/snippet}
                    </FormField>
                    <Button
                      variant="tertiary"
                      disabled={state.busy}
                      ariaLabel={t(
                        'mcp.removeEntry',
                        'Remove {group} entry {number}',
                        { group: mapping.label, number: index + 1 },
                      )}
                      onClick={() =>
                        set(
                          mapping.key,
                          draft[mapping.key].filter(
                            (_, position) => position !== index,
                          ),
                        )}>{t('common.remove', 'Remove')}</Button
                    >
                  </div>
                {/each}
                <Button
                  variant="secondary"
                  disabled={state.busy}
                  onClick={() =>
                    set(mapping.key, [
                      ...draft[mapping.key],
                      { name: '', value: '' },
                    ])}
                  >{t('mcp.addEntry', 'Add {group}', {
                    group: mapping.label,
                  })}</Button
                >
              </div>
            {/each}
            <p>
              {t(
                'mcp.secretsHelp',
                'Use credential names here, not secret values. After saving, open Credentials to set their values.',
              )}
            </p>
          </div>
        </details>
        <p>
          {t(
            'mcp.saveHelp',
            'Saving applies the connection on the vBot host. Editing its settings interrupts the current connection.',
          )}
        </p>
      </form>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={state.busy}
        onClick={() => {
          draft = null;
        }}>{t('common.cancel', 'Cancel')}</Button
      >
      <Button
        variant="primary"
        type="submit"
        form={`${componentId}-form`}
        disabled={state.busy}
        loading={state.busy}>{t('mcp.save', 'Save connection')}</Button
      >
    {/snippet}
  </Modal>
{/if}

{#if secretConnection}
  <Modal
    title={t('mcp.credentialsFor', 'Credentials for {name}', {
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
          label={t('mcp.credentialName', 'Credential name')}
        >
          {#snippet children(field)}<Dropdown
              id={field.controlId}
              value={secretKey}
              options={mcpCredentialNames(secretConnection.configuration)}
              disabled={state.busy}
              ariaLabel={t('mcp.credentialName', 'Credential name')}
              onValueChange={(value) => {
                secretKey = value;
                secretValue = '';
              }}
            />{/snippet}
        </FormField>
        <FormField
          controlId={`${componentId}-value`}
          label={t('mcp.secretValue', 'New secret value')}
          help={t(
            'mcp.secretHelp',
            'Saved values are never displayed. This named credential may also be used by other connections.',
          )}
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
        onClick={() => saveCredential('')}
        >{t('mcp.clearCredential', 'Clear credential')}</Button
      >
      <Button
        variant="secondary"
        disabled={state.busy}
        onClick={closeCredentials}>{t('common.cancel', 'Cancel')}</Button
      >
      <Button
        variant="primary"
        type="submit"
        form={`${componentId}-secret`}
        disabled={state.busy || !secretValue}
        >{t('mcp.saveCredential', 'Save credential')}</Button
      >
    {/snippet}
  </Modal>
{/if}
{#if removal}
  <ConfirmDialog
    title={t('mcp.removeTitle', 'Remove {name}?', { name: removal.id })}
    body={t(
      'mcp.removeBody',
      'Agents will lose access to this connection. The external application and installed software will remain.',
    )}
    confirmLabel={t('common.remove', 'Remove')}
    onConfirm={remove}
    onCancel={() => {
      removal = null;
    }}
  />
{/if}

<style>
  .mcp-panel {
    display: grid;
    gap: 14px;
    padding-top: 20px;
    border-top: 1px solid var(--border);
  }
  .mcp-heading,
  .mcp-identity,
  .mcp-actions,
  .mcp-grant {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .mcp-heading,
  .mcp-grant {
    justify-content: space-between;
  }
  .mcp-heading {
    flex-wrap: wrap;
  }
  h3 {
    margin: 0;
    font-size: var(--fs-heading-sm);
  }
  h4 {
    margin: 0;
    font-size: var(--fs-label-md);
    font-weight: 500;
  }
  p {
    margin: 6px 0;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .mcp-connections,
  .mcp-editor,
  .mcp-group {
    display: grid;
    gap: 14px;
    min-width: 0;
  }
  .mcp-connection {
    padding: 14px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    min-width: 0;
  }
  .mcp-identity strong,
  .mcp-endpoint {
    font-family: var(--font-mono);
    overflow-wrap: anywhere;
  }
  .mcp-identity {
    flex-wrap: wrap;
    min-width: 0;
  }
  .mcp-actions {
    flex-wrap: wrap;
    margin-top: 14px;
  }
  .mcp-grid,
  .mcp-grants {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
  }
  .mcp-entry {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 10px;
    align-items: end;
  }
  .mcp-mapping {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
  }
  .mcp-group {
    border-top: 1px solid var(--border);
    padding-top: 14px;
    justify-items: start;
  }
  .mcp-group > .mcp-entry,
  .mcp-grants {
    width: 100%;
  }
  .mcp-grant {
    padding: 8px 0;
    min-width: 0;
  }
  .mcp-grant span {
    overflow-wrap: anywhere;
  }
  .mcp-grant small {
    display: block;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .mcp-advanced {
    border-top: 1px solid var(--border);
    padding-top: 14px;
  }
  summary {
    cursor: pointer;
    color: var(--text-med);
    font-size: var(--fs-body-md);
    padding: 6px 0 14px;
  }
  :global(.modal.mcp-modal),
  :global(.modal.mcp-credentials-modal) {
    display: flex;
    flex-direction: column;
    max-height: calc(100dvh - 32px);
  }
  :global(.modal.mcp-modal) {
    width: min(720px, calc(100vw - 32px));
  }
  .modal-body {
    overflow-y: auto;
    min-height: 0;
  }
  @media (max-width: 640px) {
    .mcp-grid,
    .mcp-grants,
    .mcp-mapping {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
