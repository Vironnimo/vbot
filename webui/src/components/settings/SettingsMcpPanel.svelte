<script>
  import './mcp.css';
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
    MCP_DESCRIPTION_MAX_LENGTH,
    mcpDraft,
    mcpCredentialNames,
  } from '$lib/mcpSettings.js';

  const componentId = $props.id();
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
  let capabilityQuery = $state('');
  const controller = createMcpSettings({
    onChange: (next) => {
      state = next;
    },
  });
  let blocked = $derived(state.busy || Boolean(state.job));
  let transportOptions = $derived([
    { value: 'stdio', label: t('mcp.local') },
    { value: 'http', label: t('mcp.http') },
    { value: 'sse', label: t('mcp.sse') },
  ]);
  let mappingFields = $derived([
    {
      key: 'environment',
      label: t('mcp.environment'),
      value: t('mcp.value'),
    },
    {
      key: 'credential_environment',
      label: t('mcp.credentialEnvironment'),
      value: t('mcp.credentialName'),
    },
    {
      key: 'credential_headers',
      label: t('mcp.credentialHeaders'),
      value: t('mcp.credentialName'),
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
  }
  function set(field, value) {
    draft = { ...draft, [field]: value };
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
      return { label: t('mcp.disabled'), variant: 'neutral' };
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
    <div>
      <h4 class="s-subhead__title">{t('mcp.title')}</h4>
      <p class="s-subhead__desc">
        {t('mcp.host')}
      </p>
    </div>
    <Button
      variant="secondary"
      disabled={blocked || state.loading}
      onClick={() => edit()}>{t('mcp.add')}</Button
    >
  </div>
  {#if state.error && !draft && !secretConnection}
    <Banner variant="error" role="alert">
      {state.error}
      <Button
        variant="secondary"
        disabled={state.busy}
        onClick={controller.refresh}>{t('common.retry')}</Button
      >
    </Banner>
  {/if}
  {#if state.notice && !draft && !secretConnection}<Banner
      variant="success"
      role="status">{state.notice}</Banner
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
    />
  {:else}
    <div class="s-group mcp-connections">
      {#each state.connections as connection (connection.id)}
        {@const appearance = status(connection)}
        <article class="mcp-connection s-entity" aria-label={connection.id}>
          <div class="s-entity__head mcp-connection__head">
            <div class="s-row-info">
              <div class="mcp-identity">
                <strong>{connection.id}</strong><StatusChip
                  variant={appearance.variant}>{appearance.label}</StatusChip
                >
              </div>
              <p class="mcp-endpoint">
                {connection.configuration.transport === 'stdio'
                  ? connection.configuration.command
                  : connection.configuration.url}
              </p>
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
            </div>
            <div class="s-entity__end">
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
            </div>
          </div>
          <div class="mcp-connection__body">
            {#if connection.error}<Banner variant="error"
                >{connection.error}</Banner
              >{/if}
            <div class="mcp-actions">
              <Button
                variant="secondary"
                onClick={() => {
                  capabilityQuery = '';
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
                disabled={blocked ||
                  !mcpCredentialNames(connection.configuration).length}
                onClick={() => openCredentials(connection)}
                >{t('mcp.credentials')}</Button
              >
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
  <Modal
    title={t('mcp.inspectTitle', {
      name: state.inspector.id,
    })}
    onClose={controller.closeInspector}
    class="mcp-modal"
  >
    {#snippet body()}
      <div class="modal-body mcp-inspector">
        <form
          class="mcp-capability-search"
          onsubmit={(event) => {
            event.preventDefault();
            void controller.inspect(state.inspector.id, {
              query: capabilityQuery,
            });
          }}
        >
          <TextField
            ariaLabel={t('mcp.searchTools')}
            placeholder={t('mcp.searchTools')}
            value={capabilityQuery}
            onInput={(value) => {
              capabilityQuery = value;
            }}
          />
          <Button type="submit" variant="secondary">{t('common.search')}</Button
          >
          <Button
            variant="secondary"
            onClick={() => {
              capabilityQuery = '';
              void controller.inspect(state.inspector.id);
            }}>{t('mcp.showAll')}</Button
          >
        </form>
        {#if state.inspector.loading}
          <Banner variant="neutral" role="status">{t('common.loading')}</Banner>
        {:else if state.inspector.error}
          <Banner variant="error" role="alert">
            {state.inspector.error}
            <Button
              variant="secondary"
              onClick={() =>
                controller.inspect(state.inspector.id, {
                  query: state.inspector.query,
                  offset: state.inspector.offset,
                })}>{t('common.retry')}</Button
            >
          </Banner>
        {:else if state.inspector.data}
          {@const catalog = state.inspector.data}
          <div class="mcp-heading">
            <p>
              {t('mcp.accessHelp')}
            </p>
            <StatusChip variant={status(catalog).variant}
              >{status(catalog).label}</StatusChip
            >
          </div>
          {#if !catalog.catalog_available}
            <EmptyState
              density="compact"
              title={t('mcp.catalogMissing')}
              description={t('mcp.catalogMissingHint')}
            />
          {:else}
            {#if catalog.state !== 'connected'}
              <Banner variant="warn">{t('mcp.catalogStale')}</Banner>
            {/if}
            <h4>
              {t('mcp.availableTools', {
                count: catalog.total,
              })}
            </h4>
            {#if catalog.tools.length}
              <ul class="mcp-capability-list">
                {#each catalog.tools as tool (tool.target)}
                  <li>
                    <strong>{tool.name}</strong>
                    <p>
                      {tool.description.slice(0, 160)}{tool.description.length >
                      160
                        ? '…'
                        : ''}
                    </p>
                    {#if tool.description.length > 160}
                      <details>
                        <summary>{t('mcp.fullDescription')}</summary>
                        <p class="mcp-guidance">{tool.description}</p>
                      </details>
                    {/if}
                  </li>
                {/each}
              </ul>
              <div class="mcp-actions">
                <Button
                  variant="secondary"
                  disabled={catalog.previous_offset == null}
                  onClick={() =>
                    controller.inspect(catalog.id, {
                      query: state.inspector.query,
                      offset: catalog.previous_offset,
                    })}>{t('common.previous')}</Button
                >
                <Button
                  variant="secondary"
                  disabled={catalog.next_offset == null}
                  onClick={() =>
                    controller.inspect(catalog.id, {
                      query: state.inspector.query,
                      offset: catalog.next_offset,
                    })}>{t('common.next')}</Button
                >
              </div>
            {:else}
              <EmptyState
                density="compact"
                title={t('mcp.noToolsFound')}
                description={t('mcp.noToolsFoundHint')}
              />
            {/if}
            {#if catalog.instructions}
              <details>
                <summary>{t('mcp.serverGuidance')}</summary>
                <p class="mcp-guidance">{catalog.instructions}</p>
              </details>
            {/if}
            {#if catalog.prompts.length}
              <details>
                <summary>{t('mcp.serverPrompts')}</summary>
                <ul class="mcp-capability-list">
                  {#each catalog.prompts as prompt (prompt.name)}<li>
                      <strong>{prompt.name}</strong>
                      <p>{prompt.description}</p>
                    </li>{/each}
                </ul>
              </details>
            {/if}
          {/if}
        {/if}
      </div>
    {/snippet}
  </Modal>
{/if}

{#if draft}
  <Modal
    title={original ? t('mcp.edit') : t('mcp.add')}
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
            label={t('mcp.name')}
            required
            help={t('mcp.nameHelp')}
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
            label={t('mcp.connectionType')}
          >
            {#snippet children(field)}<Dropdown
                id={field.controlId}
                value={draft.transport}
                options={transportOptions}
                disabled={state.busy}
                ariaLabel={t('mcp.connectionType')}
                onValueChange={(value) => set('transport', value)}
              />{/snippet}
          </FormField>
        </div>
        <FormField
          controlId={`${componentId}-description`}
          label={t('mcp.description')}
          help={t('mcp.descriptionHelp')}
        >
          {#snippet children(field)}<TextField
              id={field.controlId}
              aria-describedby={field.describedBy}
              value={draft.description}
              disabled={state.busy}
              maxlength={MCP_DESCRIPTION_MAX_LENGTH}
              onInput={(value) => set('description', value)}
            />{/snippet}
        </FormField>
        {#if draft.transport === 'stdio'}
          <FormField
            controlId={`${componentId}-command`}
            label={t('mcp.program')}
            required
            help={t('mcp.programHelp')}
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
            <h4>{t('mcp.arguments')}</h4>
            {#each draft.args as argument, index (index)}
              <div class="mcp-entry">
                <FormField
                  controlId={`${componentId}-arg-${index}`}
                  label={t('mcp.argument', {
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
                  ariaLabel={t('mcp.removeArgument', { number: index + 1 })}
                  onClick={() =>
                    set(
                      'args',
                      draft.args.filter((_, position) => position !== index),
                    )}>{t('common.remove')}</Button
                >
              </div>
            {/each}
            <Button
              variant="secondary"
              disabled={state.busy}
              onClick={() => set('args', [...draft.args, ''])}
              >{t('mcp.addArgument')}</Button
            >
          </div>
        {:else}
          <FormField
            controlId={`${componentId}-url`}
            label={t('mcp.url')}
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
          <FormField controlId={`${componentId}-oauth`} label={t('mcp.oauth')}>
            {#snippet children(field)}<Toggle
                id={field.controlId}
                checked={draft.oauth}
                disabled={state.busy}
                ariaLabel={t('mcp.oauth')}
                onChange={(value) => set('oauth', value)}
              />{/snippet}
          </FormField>
        {/if}
        <p>
          {t('mcp.accessHelp')}
        </p>
        <details class="mcp-advanced">
          <summary>{t('mcp.advanced')}</summary>
          <div class="mcp-editor">
            <div class="mcp-grid">
              <FormField
                controlId={`${componentId}-timeout`}
                label={t('mcp.timeout')}
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
                label={t('mcp.enabled')}
              >
                {#snippet children(field)}<Toggle
                    id={field.controlId}
                    checked={draft.enabled}
                    disabled={state.busy}
                    ariaLabel={t('mcp.enabled')}
                    onChange={(value) => set('enabled', value)}
                  />{/snippet}
              </FormField>
            </div>
            {#if draft.transport === 'stdio'}
              <FormField
                controlId={`${componentId}-cwd`}
                label={t('mcp.directory')}
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
                label={t('mcp.redirect')}
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
                      label={t('mcp.entryName')}
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
                      ariaLabel={t('mcp.removeEntry', {
                        group: mapping.label,
                        number: index + 1,
                      })}
                      onClick={() =>
                        set(
                          mapping.key,
                          draft[mapping.key].filter(
                            (_, position) => position !== index,
                          ),
                        )}>{t('common.remove')}</Button
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
                  >{t('mcp.addEntry', {
                    group: mapping.label,
                  })}</Button
                >
              </div>
            {/each}
            <p>
              {t('mcp.secretsHelp')}
            </p>
          </div>
        </details>
        <p>
          {t('mcp.saveHelp')}
        </p>
      </form>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={state.busy}
        onClick={() => {
          draft = null;
        }}>{t('common.cancel')}</Button
      >
      <Button
        variant="primary"
        type="submit"
        form={`${componentId}-form`}
        disabled={state.busy}
        loading={state.busy}>{t('mcp.save')}</Button
      >
    {/snippet}
  </Modal>
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
