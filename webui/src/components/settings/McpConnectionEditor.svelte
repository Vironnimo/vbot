<script>
  // MCP connection editor: a sheet in the Settings section grammar. A new
  // connection starts with the quick fill; then the connection itself, the
  // program or server it reaches, and the rarely changed settings behind
  // one disclosure. The browser's own form validation checks the fields; a
  // problem inside the closed disclosure opens it first.
  import { tick } from 'svelte';

  import { t } from '$lib/i18n.js';
  import { MCP_DESCRIPTION_MAX_LENGTH } from '$lib/mcpSettings.js';
  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextField from '../ui/TextField.svelte';
  import PathField from '../ui/PathField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import McpQuickFill from './McpQuickFill.svelte';

  const componentId = $props.id();
  let {
    draft = $bindable(),
    original,
    state: panelState,
    fillNotes,
    quickFill,
    save,
  } = $props();
  let advancedOpen = $state(false);
  let errorBanner = $state();
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

  // A save failure shows at the end of the body, next to Save.
  $effect(() => {
    if (panelState.error) {
      void tick().then(() =>
        errorBanner?.scrollIntoView?.({ block: 'nearest' }),
      );
    }
  });

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
  function close() {
    draft = null;
  }
  // The browser reports the first invalid field when Save is clicked; one
  // inside the closed Advanced settings opens them first so it can show.
  function revealInvalid(event) {
    const hiddenParent = event.target.closest?.('[hidden]');
    if (hiddenParent?.id === `${componentId}-advanced`) {
      hiddenParent.hidden = false;
      advancedOpen = true;
    }
  }
</script>

{#if draft}
  <Modal
    title={original ? t('mcp.edit') : t('mcp.add')}
    description={t('mcp.accessHelp')}
    size="lg"
    variant="sheet"
    class="mcp-modal"
    closeDisabled={panelState.busy}
    onClose={close}
  >
    {#snippet body()}
      <form
        id={`${componentId}-form`}
        onsubmit={save}
        oninvalidcapture={revealInvalid}
      >
        <div class="modal-body mcp-editor">
          {#if !original}
            <div class="s-group">
              <McpQuickFill disabled={panelState.busy} onFill={quickFill} />
            </div>
          {/if}
          {#each fillNotes as note, noteIndex (noteIndex)}
            <Banner variant="warn">{note}</Banner>
          {/each}

          <section
            class="s-section"
            aria-labelledby={`${componentId}-connection-title`}
          >
            <div class="s-section__head">
              <h4
                class="s-section__title"
                id={`${componentId}-connection-title`}
              >
                {t('mcp.connectionSection')}
              </h4>
            </div>
            <div class="s-group">
              <div class="s-row">
                <div class="s-row-info">
                  <label class="s-row-label" for={`${componentId}-name`}>
                    {t('mcp.name')}
                  </label>
                  <div class="s-row-desc" id={`${componentId}-name-desc`}>
                    {t('mcp.nameHelp')}
                  </div>
                </div>
                <div class="s-row-control">
                  <TextField
                    id={`${componentId}-name`}
                    code
                    aria-describedby={`${componentId}-name-desc`}
                    value={draft.id}
                    disabled={panelState.busy || Boolean(original)}
                    required
                    pattern={'[a-z][a-z0-9_]{0,31}'}
                    onInput={(value) => set('id', value)}
                  />
                </div>
              </div>
              <div class="s-row">
                <div class="s-row-info">
                  <label
                    class="s-row-label"
                    id={`${componentId}-transport-label`}
                    for={`${componentId}-transport`}
                  >
                    {t('mcp.connectionType')}
                  </label>
                </div>
                <div class="s-row-control">
                  <Dropdown
                    id={`${componentId}-transport`}
                    ariaLabelledby={`${componentId}-transport-label`}
                    value={draft.transport}
                    options={transportOptions}
                    disabled={panelState.busy}
                    onValueChange={(value) => set('transport', value)}
                  />
                </div>
              </div>
              {#if draft.transport === 'sse'}
                <div class="s-group__block s-group__block--attached">
                  <Banner variant="warn">{t('mcp.sseHint')}</Banner>
                </div>
              {/if}
              <div class="s-row">
                <div class="s-row-info">
                  <label class="s-row-label" for={`${componentId}-description`}>
                    {t('mcp.description')}
                  </label>
                  <div
                    class="s-row-desc"
                    id={`${componentId}-description-desc`}
                  >
                    {t('mcp.descriptionHelp')}
                  </div>
                </div>
                <div class="s-row-control">
                  <TextField
                    id={`${componentId}-description`}
                    aria-describedby={`${componentId}-description-desc`}
                    value={draft.description}
                    disabled={panelState.busy}
                    maxlength={MCP_DESCRIPTION_MAX_LENGTH}
                    onInput={(value) => set('description', value)}
                  />
                </div>
              </div>
            </div>
          </section>

          <section
            class="s-section"
            aria-labelledby={`${componentId}-target-title`}
          >
            <div class="s-section__head">
              <h4 class="s-section__title" id={`${componentId}-target-title`}>
                {draft.transport === 'stdio'
                  ? t('mcp.programSection')
                  : t('mcp.serverSection')}
              </h4>
            </div>
            <div class="s-group">
              {#if draft.transport === 'stdio'}
                <div class="s-row">
                  <div class="s-row-info">
                    <label class="s-row-label" for={`${componentId}-command`}>
                      {t('mcp.program')}
                    </label>
                    <div class="s-row-desc" id={`${componentId}-command-desc`}>
                      {t('mcp.programHelp')}
                    </div>
                  </div>
                  <div class="s-row-control">
                    <PathField
                      id={`${componentId}-command`}
                      mode="file"
                      aria-describedby={`${componentId}-command-desc`}
                      value={draft.command}
                      disabled={panelState.busy}
                      required
                      onInput={(value) => set('command', value)}
                    />
                  </div>
                </div>
                <div class="s-row s-row--stacked">
                  <div class="s-row-info">
                    <div class="s-row-label" id={`${componentId}-args-label`}>
                      {t('mcp.arguments')}
                    </div>
                    <div class="s-row-desc">{t('mcp.argumentsHelp')}</div>
                  </div>
                  <div
                    class="mcp-list"
                    role="group"
                    aria-labelledby={`${componentId}-args-label`}
                  >
                    {#each draft.args as argument, index (index)}
                      <div class="mcp-list__entry">
                        <TextField
                          id={`${componentId}-arg-${index}`}
                          code
                          ariaLabel={t('mcp.argument', { number: index + 1 })}
                          value={argument}
                          disabled={panelState.busy}
                          onInput={(value) =>
                            set(
                              'args',
                              draft.args.map((item, position) =>
                                position === index ? value : item,
                              ),
                            )}
                        />
                        <Button
                          variant="tertiary"
                          disabled={panelState.busy}
                          ariaLabel={t('mcp.removeArgument', {
                            number: index + 1,
                          })}
                          onClick={() =>
                            set(
                              'args',
                              draft.args.filter(
                                (_, position) => position !== index,
                              ),
                            )}>{t('common.remove')}</Button
                        >
                      </div>
                    {/each}
                    <div class="mcp-list__add">
                      <Button
                        variant="secondary"
                        disabled={panelState.busy}
                        onClick={() => set('args', [...draft.args, ''])}
                        >{t('mcp.addArgument')}</Button
                      >
                    </div>
                  </div>
                </div>
              {:else}
                <div class="s-row">
                  <div class="s-row-info">
                    <label class="s-row-label" for={`${componentId}-url`}>
                      {t('mcp.url')}
                    </label>
                  </div>
                  <div class="s-row-control">
                    <TextField
                      id={`${componentId}-url`}
                      type="url"
                      code
                      value={draft.url}
                      required
                      disabled={panelState.busy}
                      onInput={(value) => set('url', value)}
                    />
                  </div>
                </div>
                <div class="s-row s-row--compact">
                  <div class="s-row-info">
                    <label class="s-row-label" for={`${componentId}-oauth`}>
                      {t('mcp.oauth')}
                    </label>
                  </div>
                  <div class="s-row-control">
                    <Toggle
                      id={`${componentId}-oauth`}
                      checked={draft.oauth}
                      disabled={panelState.busy}
                      ariaLabel={t('mcp.oauth')}
                      onChange={(value) => set('oauth', value)}
                    />
                  </div>
                </div>
              {/if}
            </div>
          </section>

          <section
            class="s-section"
            aria-labelledby={`${componentId}-advanced-title`}
          >
            <div class="s-group">
              <button
                type="button"
                class="s-row s-row--compact s-disclosure-row"
                aria-expanded={advancedOpen}
                aria-controls={`${componentId}-advanced`}
                onclick={() => (advancedOpen = !advancedOpen)}
              >
                <span class="s-row-label" id={`${componentId}-advanced-title`}>
                  <span
                    class="disclosure-chevron"
                    class:disclosure-chevron--open={advancedOpen}
                    aria-hidden="true"
                  ></span>
                  {t('mcp.advanced')}
                </span>
              </button>
              <div
                class="s-group__rows"
                id={`${componentId}-advanced`}
                role="group"
                aria-labelledby={`${componentId}-advanced-title`}
                hidden={!advancedOpen}
              >
                <div class="s-row s-row--compact">
                  <div class="s-row-info">
                    <label class="s-row-label" for={`${componentId}-timeout`}>
                      {t('mcp.timeout')}
                    </label>
                  </div>
                  <div class="s-row-control s-row-control--number">
                    <TextField
                      id={`${componentId}-timeout`}
                      type="number"
                      min="0.001"
                      max="86400"
                      step="any"
                      required
                      value={draft.timeout}
                      disabled={panelState.busy}
                      onInput={(value) => set('timeout', value)}
                    />
                  </div>
                </div>
                <div class="s-row s-row--compact">
                  <div class="s-row-info">
                    <label class="s-row-label" for={`${componentId}-enabled`}>
                      {t('mcp.enabled')}
                    </label>
                  </div>
                  <div class="s-row-control">
                    <Toggle
                      id={`${componentId}-enabled`}
                      checked={draft.enabled}
                      disabled={panelState.busy}
                      ariaLabel={t('mcp.enabled')}
                      onChange={(value) => set('enabled', value)}
                    />
                  </div>
                </div>
                <div class="s-row">
                  <div class="s-row-info">
                    <span class="s-row-label mcp-editor__label">
                      <label
                        id={`${componentId}-sampling-label`}
                        for={`${componentId}-sampling`}
                        >{t('mcp.sampling')}</label
                      >
                      <InfoHint
                        text={t('mcp.samplingHelp')}
                        ariaLabel={t('mcp.samplingHelpAria')}
                      />
                    </span>
                  </div>
                  <div class="s-row-control">
                    <Dropdown
                      id={`${componentId}-sampling`}
                      ariaLabelledby={`${componentId}-sampling-label`}
                      value={draft.sampling}
                      options={samplingOptions}
                      disabled={panelState.busy}
                      onValueChange={(value) => set('sampling', value)}
                    />
                  </div>
                </div>
                <div class="s-row">
                  <div class="s-row-info">
                    <span class="s-row-label mcp-editor__label">
                      <label
                        id={`${componentId}-roots-label`}
                        for={`${componentId}-roots`}>{t('mcp.roots')}</label
                      >
                      <InfoHint
                        text={t('mcp.rootsHelp')}
                        ariaLabel={t('mcp.rootsHelpAria')}
                      />
                    </span>
                  </div>
                  <div class="s-row-control">
                    <Dropdown
                      id={`${componentId}-roots`}
                      ariaLabelledby={`${componentId}-roots-label`}
                      value={draft.roots}
                      options={rootsOptions}
                      disabled={panelState.busy}
                      onValueChange={(value) => set('roots', value)}
                    />
                  </div>
                </div>
                {#if draft.transport === 'stdio'}
                  <div class="s-row">
                    <div class="s-row-info">
                      <label class="s-row-label" for={`${componentId}-cwd`}>
                        {t('mcp.directory')}
                      </label>
                    </div>
                    <div class="s-row-control">
                      <PathField
                        id={`${componentId}-cwd`}
                        mode="directory"
                        projectShortcuts
                        value={draft.cwd}
                        disabled={panelState.busy}
                        onInput={(value) => set('cwd', value)}
                      />
                    </div>
                  </div>
                {:else if draft.oauth}
                  <div class="s-row">
                    <div class="s-row-info">
                      <label
                        class="s-row-label"
                        for={`${componentId}-redirect`}
                      >
                        {t('mcp.redirect')}
                      </label>
                      <div
                        class="s-row-desc"
                        id={`${componentId}-redirect-desc`}
                      >
                        {t('mcp.redirectHelp')}
                      </div>
                    </div>
                    <div class="s-row-control">
                      <TextField
                        id={`${componentId}-redirect`}
                        aria-describedby={`${componentId}-redirect-desc`}
                        type="url"
                        code
                        value={draft.oauth_redirect_uri}
                        disabled={panelState.busy}
                        onInput={(value) => set('oauth_redirect_uri', value)}
                      />
                    </div>
                  </div>
                  <div class="s-row">
                    <div class="s-row-info">
                      <span class="s-row-label mcp-editor__label">
                        <label for={`${componentId}-client-id`}
                          >{t('mcp.oauthClientId')}</label
                        >
                        <InfoHint
                          text={t('mcp.oauthClientIdHelp')}
                          ariaLabel={t('mcp.oauthClientIdHelpAria')}
                        />
                      </span>
                    </div>
                    <div class="s-row-control">
                      <TextField
                        id={`${componentId}-client-id`}
                        code
                        value={draft.oauth_client_id}
                        disabled={panelState.busy}
                        onInput={(value) => set('oauth_client_id', value)}
                      />
                    </div>
                  </div>
                  {#if draft.oauth_client_id.trim()}
                    <div class="s-row">
                      <div class="s-row-info">
                        <label
                          class="s-row-label"
                          for={`${componentId}-client-secret`}
                        >
                          {t('mcp.oauthClientSecret')}
                        </label>
                        <div
                          class="s-row-desc"
                          id={`${componentId}-client-secret-desc`}
                        >
                          {t('mcp.oauthClientSecretHelp')}
                        </div>
                      </div>
                      <div class="s-row-control">
                        <TextField
                          id={`${componentId}-client-secret`}
                          aria-describedby={`${componentId}-client-secret-desc`}
                          code
                          value={draft.oauth_client_secret}
                          disabled={panelState.busy}
                          pattern="[A-Za-z_][A-Za-z0-9_]*"
                          onInput={(value) => set('oauth_client_secret', value)}
                        />
                      </div>
                    </div>
                  {/if}
                  <div class="s-row">
                    <div class="s-row-info">
                      <label class="s-row-label" for={`${componentId}-scopes`}>
                        {t('mcp.oauthScopes')}
                      </label>
                      <div class="s-row-desc" id={`${componentId}-scopes-desc`}>
                        {t('mcp.oauthScopesHelp')}
                      </div>
                    </div>
                    <div class="s-row-control">
                      <TextField
                        id={`${componentId}-scopes`}
                        aria-describedby={`${componentId}-scopes-desc`}
                        code
                        value={draft.oauth_scopes}
                        disabled={panelState.busy}
                        onInput={(value) => set('oauth_scopes', value)}
                      />
                    </div>
                  </div>
                {/if}
                {#each mappingFields as mapping (mapping.key)}
                  <div class="s-row s-row--stacked">
                    <div class="s-row-info">
                      <div
                        class="s-row-label"
                        id={`${componentId}-${mapping.key}-label`}
                      >
                        {mapping.label}
                      </div>
                    </div>
                    <div
                      class="mcp-list"
                      role="group"
                      aria-labelledby={`${componentId}-${mapping.key}-label`}
                    >
                      {#each draft[mapping.key] as entry, index (index)}
                        <div class="mcp-list__entry mcp-list__entry--pair">
                          <TextField
                            id={`${componentId}-${mapping.key}-${index}-name`}
                            code
                            placeholder={t('mcp.entryName')}
                            ariaLabel={t('mcp.entryField', {
                              field: t('mcp.entryName'),
                              number: index + 1,
                            })}
                            value={entry.name}
                            required
                            disabled={panelState.busy}
                            onInput={(value) =>
                              setMapping(mapping.key, index, 'name', value)}
                          />
                          <TextField
                            id={`${componentId}-${mapping.key}-${index}-value`}
                            code
                            placeholder={mapping.value}
                            ariaLabel={t('mcp.entryField', {
                              field: mapping.value,
                              number: index + 1,
                            })}
                            value={entry.value}
                            required={mapping.key !== 'environment'}
                            disabled={panelState.busy}
                            onInput={(value) =>
                              setMapping(mapping.key, index, 'value', value)}
                          />
                          <Button
                            variant="tertiary"
                            disabled={panelState.busy}
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
                      <div class="mcp-list__add">
                        <Button
                          variant="secondary"
                          disabled={panelState.busy}
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
                    </div>
                  </div>
                {/each}
                <div class="s-group__block s-group__note">
                  {t('mcp.secretsHelp')}
                </div>
              </div>
            </div>
          </section>

          {#if original}
            <p class="mcp-editor__note">{t('mcp.saveHelp')}</p>
          {/if}
          {#if panelState.error}
            <div bind:this={errorBanner}>
              <Banner variant="error" role="alert">{panelState.error}</Banner>
            </div>
          {/if}
        </div>

        <div class="modal-footer">
          <Button variant="secondary" disabled={panelState.busy} onClick={close}
            >{t('common.cancel')}</Button
          >
          <Button
            variant="primary"
            type="submit"
            disabled={panelState.busy}
            loading={panelState.busy}>{t('mcp.save')}</Button
          >
        </div>
      </form>
    {/snippet}
  </Modal>
{/if}
