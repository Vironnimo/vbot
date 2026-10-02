<script>
  // Imports MCP servers from setup text written for another client (an
  // mcpServers or servers JSON block, Codex TOML, `claude mcp add` or another
  // command line, or a server URL): paste or open it, review each server's
  // program or URL, credentials and warnings, then import the chosen ones.
  // Secrets in the text become vBot credentials on the server; the preview
  // never carries their values back.
  import { onDestroy, onMount, untrack } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextField from '../ui/TextField.svelte';
  import { t } from '$lib/i18n.js';
  import { mcpCredentialSlot, mcpImportPlan } from '$lib/mcpSettings.js';

  const noop = () => {};
  // Re-reading the preview waits until the user paused typing a new id.
  const ID_PREVIEW_DELAY_MS = 400;

  let {
    controller,
    busy = false,
    error = '',
    initialSource = '',
    onClose = noop,
    // Receives the ids of the imported connections.
    onImported = noop,
  } = $props();

  const componentId = $props.id();
  let source = $state(untrack(() => initialSource));
  let servers = $state(null);
  let ids = $state({});
  let values = $state({});
  const chosen = new SvelteSet();
  let previewError = $state('');
  let previewing = $state(false);
  let idsPending = $state(false);
  let fileInput = $state();
  let request = 0;
  let idTimer;
  let plan = $derived(servers ? mcpImportPlan(servers, chosen, values) : null);
  let working = $derived(busy || previewing || idsPending);

  onMount(() => {
    if (source.trim()) void preview();
  });
  onDestroy(() => clearTimeout(idTimer));

  const blocked = (server) => Boolean(server.error || server.conflict);

  async function preview(changedIds = null) {
    const current = ++request;
    previewing = true;
    previewError = '';
    try {
      const result = await controller.previewImport(source, changedIds);
      if (current !== request) return;
      const previous = servers;
      servers = result.servers;
      ids = Object.fromEntries(
        result.servers.map((server) => [server.name, server.id]),
      );
      for (const server of result.servers) {
        const before = previous?.find((item) => item.name === server.name);
        // A first preview proposes its selection; later ones keep the
        // user's, choosing a server whose new id resolved its conflict.
        const choose = blocked(server)
          ? false
          : before
            ? chosen.has(server.name) || blocked(before)
            : server.selected;
        if (choose) chosen.add(server.name);
        else chosen.delete(server.name);
      }
    } catch (failure) {
      if (current === request) previewError = failure.message;
    } finally {
      if (current === request) {
        previewing = false;
        idsPending = false;
      }
    }
  }

  function edit() {
    ++request;
    servers = null;
    previewError = '';
    previewing = false;
    idsPending = false;
    clearTimeout(idTimer);
  }

  function setId(server, value) {
    ids = { ...ids, [server.name]: value.trim() };
    idsPending = true;
    clearTimeout(idTimer);
    idTimer = setTimeout(() => void preview(ids), ID_PREVIEW_DELAY_MS);
  }

  async function readFile(event) {
    const [file] = event.currentTarget.files ?? [];
    event.currentTarget.value = '';
    if (!file) return;
    try {
      source = await file.text();
      servers = null;
      await preview();
    } catch (failure) {
      previewError = failure.message;
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (!servers) {
      if (source.trim()) await preview();
      return;
    }
    if (!plan?.servers.length || working) return;
    const result = await controller.importServers(source, plan);
    if (result.saved) onImported(plan.servers.map((name) => plan.ids[name]));
  }

  // The program and its arguments as one line; an argument with spaces is quoted.
  function commandLine(connection) {
    return [connection.command, ...(connection.args ?? [])]
      .map((part) => (/\s/.test(part) || !part ? `"${part}"` : part))
      .join(' ');
  }

  function transportLabel(connection) {
    return {
      stdio: t('mcp.local'),
      http: t('mcp.http'),
      sse: t('mcp.sse'),
    }[connection.transport];
  }

  function credentialText(credential) {
    if (credential.state === 'provided')
      return t('mcp.importCredentialProvided', { name: credential.name });
    if (credential.state === 'set')
      return t('mcp.importCredentialSet', { name: credential.name });
    return t('mcp.importCredentialMissing', { name: credential.name });
  }
</script>

<Modal
  title={t('mcp.importTitle')}
  closeDisabled={busy}
  onClose={busy ? noop : onClose}
  class="mcp-modal"
>
  {#snippet body()}
    <form
      id={`${componentId}-import`}
      class="modal-body mcp-editor mcp-import"
      onsubmit={submit}
    >
      {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
      {#if previewError}<Banner variant="error" role="alert"
          >{previewError}</Banner
        >{/if}
      {#if !servers}
        <FormField
          controlId={`${componentId}-source`}
          label={t('mcp.importSource')}
          help={t('mcp.importSourceHelp')}
          full
        >
          {#snippet children(field)}<TextArea
              id={field.controlId}
              aria-describedby={field.describedBy}
              code
              rows={10}
              spellcheck="false"
              autocomplete="off"
              value={source}
              disabled={working}
              onInput={(value) => {
                source = value;
              }}
            />{/snippet}
        </FormField>
        <div class="mcp-actions">
          <Button
            variant="secondary"
            disabled={working}
            onClick={() => fileInput.click()}>{t('mcp.importFile')}</Button
          >
          <input
            bind:this={fileInput}
            class="mcp-import__file"
            type="file"
            accept=".json,.jsonc,.toml,.txt,application/json,text/plain"
            tabindex="-1"
            aria-hidden="true"
            onchange={readFile}
          />
        </div>
        <p>{t('mcp.importSafety')}</p>
      {:else}
        <p>{t('mcp.importReview')}</p>
        <ul class="mcp-import__servers">
          {#each servers as server, index (server.name)}
            {@const connection = server.connection}
            <li class="mcp-import__server">
              <div class="mcp-heading">
                <Checkbox
                  checked={chosen.has(server.name)}
                  disabled={working || blocked(server)}
                  onChange={(checked) => {
                    if (checked) chosen.add(server.name);
                    else chosen.delete(server.name);
                  }}
                  ><span class="mcp-import__name">{server.name}</span></Checkbox
                >
                {#if server.error}
                  <StatusChip variant="error"
                    >{t('mcp.importInvalid')}</StatusChip
                  >
                {:else if server.conflict}
                  <StatusChip variant="warn">{t('mcp.importExists')}</StatusChip
                  >
                {:else if !server.enabled}
                  <StatusChip variant="neutral"
                    >{t('mcp.importDisabled')}</StatusChip
                  >
                {/if}
              </div>
              {#if server.error}
                <p class="mcp-connection__error">{server.error}</p>
              {:else}
                <p>{transportLabel(connection)}</p>
                <p class="mcp-endpoint">
                  {connection.transport === 'stdio'
                    ? commandLine(connection)
                    : connection.url}
                </p>
                <FormField
                  controlId={`${componentId}-id-${index}`}
                  label={t('mcp.name')}
                  error={server.conflict
                    ? t('mcp.importConflict', { id: server.id })
                    : ''}
                >
                  {#snippet children(field)}<TextField
                      id={field.controlId}
                      aria-describedby={field.describedBy}
                      invalid={field.invalid}
                      value={ids[server.name] ?? server.id}
                      disabled={busy}
                      pattern={'[a-z][a-z0-9_]{0,31}'}
                      onInput={(value) => setId(server, value)}
                    />{/snippet}
                </FormField>
                {#each server.credentials as credential (credential.target)}
                  {#if credential.state === 'missing'}
                    <FormField
                      controlId={`${componentId}-secret-${index}-${credential.target}`}
                      label={t('mcp.importCredentialValue', {
                        target: credential.target,
                      })}
                      help={[credentialText(credential), credential.description]
                        .filter(Boolean)
                        .join(' ')}
                    >
                      {#snippet children(field)}<TextField
                          id={field.controlId}
                          aria-describedby={field.describedBy}
                          type="password"
                          autocomplete="off"
                          value={values[
                            mcpCredentialSlot(server, credential)
                          ] ?? ''}
                          disabled={busy}
                          onInput={(value) => {
                            values = {
                              ...values,
                              [mcpCredentialSlot(server, credential)]: value,
                            };
                          }}
                        />{/snippet}
                    </FormField>
                  {:else}
                    <p>{credential.target}: {credentialText(credential)}</p>
                  {/if}
                {/each}
                {#each server.warnings as warning, warningIndex (warningIndex)}
                  <Banner variant="warn">{warning.message}</Banner>
                {/each}
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    </form>
  {/snippet}
  {#snippet footer()}
    {#if servers}
      <Button variant="secondary" disabled={busy} onClick={edit}
        >{t('mcp.importEdit')}</Button
      >
    {/if}
    <Button variant="secondary" disabled={busy} onClick={onClose}
      >{t('common.cancel')}</Button
    >
    <Button
      variant="primary"
      type="submit"
      form={`${componentId}-import`}
      disabled={working || (servers ? !plan?.servers.length : !source.trim())}
      loading={busy || previewing}
      >{servers
        ? t('mcp.importApply', { count: plan?.servers.length ?? 0 })
        : t('mcp.importPreview')}</Button
    >
  {/snippet}
</Modal>
