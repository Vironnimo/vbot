<script>
  import { onMount, onDestroy } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { defaultChoiceValue, markDefaultOption } from '$lib/defaultChoice.js';
  import {
    listExternalSources,
    listExternalSessions,
    readExternalSession,
    readExternalMemories,
    importExternalSession,
    importExternalMemory,
  } from '$lib/api.js';
  import { parseAgentAddress } from '$lib/agentAddress.js';
  import Modal from '../ui/Modal.svelte';
  import FormField from '../ui/FormField.svelte';
  import Button from '../ui/Button.svelte';
  import Banner from '../ui/Banner.svelte';
  import PathField from '../ui/PathField.svelte';
  import TextField from '../ui/TextField.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import Dropdown from '../Dropdown.svelte';

  let {
    agentAddress,
    projects = [],
    // The Identity Agent's default Project id, '' for its Workspace, or null
    // while unknown; it is the place option marked "(default)".
    defaultProjectId = null,
    onClose,
    onImported,
  } = $props();
  const id = $props.id();
  let sources = $state([]);
  let source = $state('codex');
  let mode = $state('sessions');
  let path = $state('');
  let query = $state('');
  let page = $state(null);
  let selected = $state(null);
  let preview = $state(null);
  let facts = $state('');
  let project = $state('default');
  let scope = $state('agent');
  let memoryProject = $state('');
  let busy = $state(false);
  let saving = $state(false);
  let sourceChanged = $state(false);
  let error = $state('');
  let result = $state(null);
  let disposed = false;
  const request = new AbortController();
  onDestroy(() => {
    disposed = true;
    request.abort();
  });
  let owner = $derived(parseAgentAddress(agentAddress));
  let effectiveScope = $derived(owner.projectId ? 'project' : scope);
  let destinationProject = $derived(owner.projectId || memoryProject);

  async function perform(action) {
    if (busy) return;
    busy = true;
    error = '';
    try {
      await action();
    } catch (failure) {
      if (!disposed) {
        error = failure.message;
        if (failure.code === 'external_memory_changed') sourceChanged = true;
      }
    } finally {
      if (!disposed) {
        busy = false;
        saving = false;
      }
    }
  }
  onMount(() => {
    void perform(async () => {
      const response = await listExternalSources({ signal: request.signal });
      if (!disposed) sources = response.items;
    });
  });

  function reset() {
    page = null;
    selected = null;
    preview = null;
    facts = '';
    result = null;
    error = '';
    sourceChanged = false;
  }
  function browse(offset = 0, charOffset = 0) {
    return perform(async () => {
      const params = {
        source,
        ...(path.trim() ? { path: path.trim() } : {}),
        offset,
        limit: 20,
      };
      const response =
        mode === 'sessions'
          ? await listExternalSessions(
              { ...params, query },
              { signal: request.signal },
            )
          : await readExternalMemories(
              { ...params, char_offset: charOffset },
              { signal: request.signal },
            );
      if (!disposed) {
        page = response;
        selected = null;
        preview = null;
        result = null;
        sourceChanged = false;
      }
    });
  }
  function choose(item) {
    selected = item;
    result = null;
    if (mode === 'memories') {
      // Only seed complete documents. A long source stays fully preserved;
      // the user selects facts from its paged preview instead of a silent cut.
      facts =
        item.char_offset === 0 && [...item.content].length === item.characters
          ? item.content
          : '';
      preview = null;
    } else {
      void readPreview();
    }
  }
  function readPreview(offset = 0, charOffset = 0) {
    return perform(async () => {
      const response = await readExternalSession(
        {
          source,
          source_session_id: selected.id,
          path: selected.path,
          offset,
          char_offset: charOffset,
          limit: 12,
        },
        { signal: request.signal },
      );
      if (!disposed) preview = response;
    });
  }
  function submit() {
    return perform(async () => {
      saving = true;
      const params = { source, path: selected.path };
      if (mode === 'sessions') {
        const response = await importExternalSession({
          ...params,
          source_session_id: selected.id,
          agent_id: agentAddress,
          ...(!owner.projectId && project !== 'default'
            ? { working_project_id: project || null }
            : {}),
        });
        if (!disposed) onImported(response);
      } else {
        const response = await importExternalMemory({
          ...params,
          scope: effectiveScope,
          expected_digest: selected.digest,
          ...(effectiveScope === 'project'
            ? { project_id: destinationProject }
            : { agent_id: agentAddress }),
          entries: facts
            .split(/\n\s*\n|\n(?=\s*[-*] )|\n§\n/)
            .map((value) => value.trim().replace(/^[-*] /, ''))
            .filter(Boolean),
        });
        if (!disposed) result = response;
      }
    });
  }
</script>

<Modal
  title={t('external.title')}
  description={t('external.target', { agent: agentAddress })}
  size="lg"
  closeDisabled={saving}
  {onClose}
>
  {#snippet body()}
    <div class="external-history">
      <FormField controlId={`${id}-mode`} label={t('external.kind')}>
        {#snippet children(field)}
          <Dropdown
            id={field.controlId}
            ariaLabelledby={field.labelId}
            value={mode}
            disabled={busy}
            options={[
              { value: 'sessions', label: t('external.sessions') },
              { value: 'memories', label: t('external.memories') },
            ]}
            onValueChange={(value) => {
              mode = value;
              reset();
            }}
          />
        {/snippet}
      </FormField>
      <FormField controlId={`${id}-source`} label={t('external.source')}>
        {#snippet children(field)}
          <Dropdown
            id={field.controlId}
            ariaLabelledby={field.labelId}
            value={source}
            disabled={busy}
            options={sources.map((item) => ({
              value: item.id,
              label: item.name,
            }))}
            onValueChange={(value) => {
              source = value;
              path = '';
              reset();
            }}
          />
        {/snippet}
      </FormField>
      <FormField
        controlId={`${id}-path`}
        label={t('external.path')}
        help={t('external.pathHelp')}
      >
        {#snippet children(field)}
          <PathField
            id={field.controlId}
            mode="any"
            value={path}
            disabled={busy}
            placeholder={sources.find((item) => item.id === source)?.path || ''}
            aria-describedby={field.describedBy}
            onInput={(value) => {
              path = value;
              reset();
            }}
          />
        {/snippet}
      </FormField>
      {#if mode === 'sessions'}
        <TextField
          value={query}
          ariaLabel={t('external.search')}
          placeholder={t('external.search')}
          disabled={busy}
          onInput={(value) => {
            query = value;
            reset();
          }}
        />
      {/if}
      <Button onClick={() => browse()} loading={busy && !saving}
        >{t('external.browse')}</Button
      >
      {#if error}<Banner variant="error">{error}</Banner>{/if}
      {#each page?.warnings || [] as warning, index (index)}<Banner
          variant="warn">{warning}</Banner
        >{/each}
      {#if page}
        <p>{t('external.found', { count: page.total })}</p>
        <div class="external-history__list">
          {#each page.items as item (`${item.path}:${item.id || item.char_offset}`)}
            <Button
              variant="tertiary"
              disabled={busy}
              aria-pressed={selected === item}
              onClick={() => choose(item)}
            >
              {item.title || item.name}
            </Button>
          {/each}
        </div>
        {#if page.next_offset !== null && page.next_offset !== undefined}
          <Button
            disabled={busy}
            onClick={() => browse(page.next_offset, page.next_char_offset || 0)}
            >{t('external.next')}</Button
          >
        {/if}
      {/if}
      {#if selected}
        <p class="external-history__path">{selected.path}</p>
        {#if mode === 'sessions'}
          <p>
            {t('external.fullHistory', {
              count: preview?.entry_count ?? selected.entry_count ?? '?',
            })}
          </p>
          {#each preview?.items || [] as entry (entry.ordinal)}
            <div>
              <span>{entry.role}</span>
              <pre>{entry.content}</pre>
            </div>
          {/each}
          {#if preview?.next_offset !== null && preview?.next_offset !== undefined}
            <Button
              disabled={busy}
              onClick={() =>
                readPreview(preview.next_offset, preview.next_char_offset)}
              >{t('external.next')}</Button
            >
          {/if}
          {#if !owner.projectId}
            <FormField
              controlId={`${id}-project`}
              label={t('external.project')}
            >
              {#snippet children(field)}
                <Dropdown
                  id={field.controlId}
                  ariaLabelledby={field.labelId}
                  value={defaultChoiceValue(project, {
                    defaultValue: defaultProjectId,
                    unsetValue: 'default',
                  })}
                  disabled={busy}
                  options={markDefaultOption(
                    [
                      { value: '', label: t('external.workspace') },
                      ...projects.map((item) => ({
                        value: item.id,
                        label: item.name || item.id,
                      })),
                    ],
                    {
                      defaultValue: defaultProjectId,
                      unsetValue: 'default',
                      fallbackLabel: t('external.defaultProject'),
                    },
                  )}
                  onValueChange={(value) => (project = value)}
                />
              {/snippet}
            </FormField>
          {/if}
        {:else}
          <pre>{selected.content}</pre>
          <p>{t('external.memoryHelp')}</p>
          {#if !owner.projectId}
            <Dropdown
              ariaLabel={t('external.scope')}
              value={scope}
              disabled={busy}
              options={[
                { value: 'agent', label: t('external.scope.agent') },
                { value: 'user', label: t('external.scope.user') },
                { value: 'project', label: t('external.scope.project') },
              ]}
              onValueChange={(value) => (scope = value)}
            />
          {/if}
          {#if effectiveScope === 'project' && !owner.projectId}
            <Dropdown
              ariaLabel={t('external.project')}
              value={memoryProject}
              disabled={busy}
              options={projects.map((item) => ({
                value: item.id,
                label: item.name || item.id,
              }))}
              onValueChange={(value) => (memoryProject = value)}
            />
          {/if}
          <TextArea
            value={facts}
            rows={6}
            ariaLabel={t('external.facts')}
            disabled={busy}
            onInput={(value) => {
              facts = value;
              result = null;
            }}
          />
        {/if}
      {/if}
      {#if result}
        <Banner variant="success"
          >{t('external.memoryImported', {
            count: result.entries.length,
          })}</Banner
        >
        <p class="external-history__path">
          {t('external.original', { path: result.saved_source_path })}
        </p>
      {/if}
    </div>
  {/snippet}
  {#snippet footer()}
    <Button disabled={saving} onClick={onClose}>{t('common.close')}</Button>
    <Button
      variant="primary"
      loading={saving}
      disabled={busy ||
        sourceChanged ||
        !selected ||
        Boolean(result) ||
        (mode === 'memories' &&
          (!facts.trim() ||
            (effectiveScope === 'project' && !destinationProject)))}
      onClick={submit}
    >
      {mode === 'sessions'
        ? t('external.continue')
        : t('external.importMemory')}
    </Button>
  {/snippet}
</Modal>

<style>
  .external-history {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }
  .external-history__list {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 4px;
  }
  .external-history__path {
    overflow-wrap: anywhere;
    color: var(--text-med);
    font-size: 12px;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 240px;
    overflow: auto;
    font-size: 12px;
    padding: 12px;
    background: var(--surface-2);
    border-radius: 6px;
  }
</style>
