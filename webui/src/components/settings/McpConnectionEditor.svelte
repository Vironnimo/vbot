<script>
  import { t } from '$lib/i18n.js';
  import { MCP_DESCRIPTION_MAX_LENGTH } from '$lib/mcpSettings.js';
  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextField from '../ui/TextField.svelte';
  import PathField from '../ui/PathField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import McpQuickFill from './McpQuickFill.svelte';
  const componentId = $props.id();
  let {
    draft = $bindable(),
    original,
    state,
    fillNotes,
    quickFill,
    save,
  } = $props();
  let transportOptions = $derived([
    { value: 'stdio', label: t('mcp.local') },
    { value: 'http', label: t('mcp.http') },
    {
      value: 'sse',
      label: t('mcp.sse'),
      secondaryLabel: t('mcp.sseDeprecated'),
    },
  ]);
  let samplingOptions = $derived([
    { value: 'off', label: t('mcp.samplingOff') },
    { value: 'ask', label: t('mcp.samplingAsk') },
    { value: 'allow', label: t('mcp.samplingAllow') },
  ]);
  let rootsOptions = $derived([
    { value: 'off', label: t('mcp.rootsOff') },
    { value: 'workspace', label: t('mcp.rootsWorkspace') },
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
</script>

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
        {#if !original}
          <McpQuickFill disabled={state.busy} onFill={quickFill} />
        {/if}
        {#each fillNotes as note, noteIndex (noteIndex)}
          <Banner variant="warn">{note}</Banner>
        {/each}
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
                ariaLabelledby={field.labelId}
                value={draft.transport}
                options={transportOptions}
                disabled={state.busy}
                ariaLabel={t('mcp.connectionType')}
                onValueChange={(value) => set('transport', value)}
              />{/snippet}
          </FormField>
        </div>
        {#if draft.transport === 'sse'}
          <Banner variant="warn">{t('mcp.sseHint')}</Banner>
        {/if}
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
            {#snippet children(field)}<PathField
                id={field.controlId}
                mode="file"
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
            <FormField
              controlId={`${componentId}-sampling`}
              label={t('mcp.sampling')}
              help={t('mcp.samplingHelp')}
            >
              {#snippet children(field)}<Dropdown
                  id={field.controlId}
                  ariaLabelledby={field.labelId}
                  value={draft.sampling}
                  options={samplingOptions}
                  disabled={state.busy}
                  ariaLabel={t('mcp.sampling')}
                  onValueChange={(value) => set('sampling', value)}
                />{/snippet}
            </FormField>
            <FormField
              controlId={`${componentId}-roots`}
              label={t('mcp.roots')}
              help={t('mcp.rootsHelp')}
            >
              {#snippet children(field)}<Dropdown
                  id={field.controlId}
                  ariaLabelledby={field.labelId}
                  value={draft.roots}
                  options={rootsOptions}
                  disabled={state.busy}
                  ariaLabel={t('mcp.roots')}
                  onValueChange={(value) => set('roots', value)}
                />{/snippet}
            </FormField>
            {#if draft.transport === 'stdio'}
              <FormField
                controlId={`${componentId}-cwd`}
                label={t('mcp.directory')}
              >
                {#snippet children(field)}<PathField
                    id={field.controlId}
                    mode="directory"
                    projectShortcuts
                    value={draft.cwd}
                    disabled={state.busy}
                    onInput={(value) => set('cwd', value)}
                  />{/snippet}
              </FormField>
            {:else if draft.oauth}
              <FormField
                controlId={`${componentId}-redirect`}
                label={t('mcp.redirect')}
                help={t('mcp.redirectHelp')}
              >
                {#snippet children(field)}<TextField
                    id={field.controlId}
                    aria-describedby={field.describedBy}
                    type="url"
                    value={draft.oauth_redirect_uri}
                    disabled={state.busy}
                    onInput={(value) => set('oauth_redirect_uri', value)}
                  />{/snippet}
              </FormField>
              <FormField
                controlId={`${componentId}-client-id`}
                label={t('mcp.oauthClientId')}
                help={t('mcp.oauthClientIdHelp')}
              >
                {#snippet children(field)}<TextField
                    id={field.controlId}
                    aria-describedby={field.describedBy}
                    value={draft.oauth_client_id}
                    disabled={state.busy}
                    onInput={(value) => set('oauth_client_id', value)}
                  />{/snippet}
              </FormField>
              {#if draft.oauth_client_id.trim()}
                <FormField
                  controlId={`${componentId}-client-secret`}
                  label={t('mcp.oauthClientSecret')}
                  help={t('mcp.oauthClientSecretHelp')}
                >
                  {#snippet children(field)}<TextField
                      id={field.controlId}
                      aria-describedby={field.describedBy}
                      value={draft.oauth_client_secret}
                      disabled={state.busy}
                      pattern="[A-Za-z_][A-Za-z0-9_]*"
                      onInput={(value) => set('oauth_client_secret', value)}
                    />{/snippet}
                </FormField>
              {/if}
              <FormField
                controlId={`${componentId}-scopes`}
                label={t('mcp.oauthScopes')}
                help={t('mcp.oauthScopesHelp')}
              >
                {#snippet children(field)}<TextField
                    id={field.controlId}
                    aria-describedby={field.describedBy}
                    value={draft.oauth_scopes}
                    disabled={state.busy}
                    onInput={(value) => set('oauth_scopes', value)}
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
