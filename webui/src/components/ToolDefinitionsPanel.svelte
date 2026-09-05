<script>
  // SystemPromptView owns prompt edits and request reconciliation. This reader
  // owns the separate, searchable Tool-definition surface, without edit state.
  import { t } from '$lib/i18n.js';
  import Button from './ui/Button.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import TextField from './ui/TextField.svelte';

  let { tools = [], onToast = () => {} } = $props();
  let query = $state('');
  let selectedName = $state('');
  let filtered = $derived(
    tools.filter(({ definition }) =>
      `${definition.name} ${definition.description}`
        .toLocaleLowerCase()
        .includes(query.trim().toLocaleLowerCase()),
    ),
  );
  let selected = $derived(
    filtered.find(({ definition }) => definition.name === selectedName) ??
      filtered[0],
  );

  async function copyDefinition() {
    try {
      await navigator.clipboard.writeText(
        JSON.stringify(selected.definition, null, 2),
      );
      onToast({ title: t('common.copied', 'Copied'), variant: 'success' });
    } catch {
      onToast({
        title: t('systemPrompt.error.copyFailed', 'Failed to copy'),
        variant: 'error',
      });
    }
  }
</script>

<div class="tool-inspector">
  <p class="tool-context-note">
    {t(
      'systemPrompt.tools.note',
      'These definitions accompany the System Prompt. MCP exposes a connection Tool here; individual functions are discovered later during the conversation.',
    )}
  </p>
  {#if tools.length === 0}
    <EmptyState
      title={t('systemPrompt.tools.empty', 'No Tools in this preview')}
      description={t(
        'systemPrompt.tools.emptyHint',
        'The selected Agent currently has no available Tool definitions.',
      )}
    />
  {:else}
    <div class="tool-inspector-layout">
      <div class="tool-index">
        <TextField
          type="search"
          value={query}
          onInput={(value) => (query = value)}
          ariaLabel={t('systemPrompt.tools.search', 'Search Tools')}
          placeholder={t('systemPrompt.tools.search', 'Search Tools')}
        />
        <span class="tool-index-count"
          >{t('systemPrompt.tools.matches', '{shown} of {total} Tools', {
            shown: filtered.length,
            total: tools.length,
          })}</span
        >
        <nav
          class="tool-list"
          aria-label={t('systemPrompt.tools.list', 'Tool definitions')}
        >
          {#each filtered as entry (entry.definition.name)}
            <Button
              variant="secondary"
              class="tool-index-item"
              aria-current={selected?.definition.name === entry.definition.name
                ? 'true'
                : undefined}
              onClick={() => (selectedName = entry.definition.name)}
            >
              <span class="tool-name">{entry.definition.name}</span>
              <span class="tool-size"
                >{t('systemPrompt.preview.tokenCount', '~{count} tokens', {
                  count: entry.tokens,
                })}</span
              >
            </Button>
          {/each}
        </nav>
      </div>
      {#if selected}
        <article class="tool-detail" aria-label={selected.definition.name}>
          <header class="tool-detail-header">
            <div>
              <h3>{selected.definition.name}</h3>
              <span class="tool-size"
                >{t('systemPrompt.preview.tokenCount', '~{count} tokens', {
                  count: selected.tokens,
                })}</span
              >
            </div>
            <Button variant="secondary" onClick={copyDefinition}
              >{t('systemPrompt.tools.copy', 'Copy definition')}</Button
            >
          </header>
          <div class="tool-detail-body">
            <h4>{t('systemPrompt.tools.description', 'Description')}</h4>
            <p class="tool-description">{selected.definition.description}</p>
            <h4>
              {t('systemPrompt.tools.parameters', 'Parameters · JSON Schema')}
            </h4>
            <pre class="tool-schema">{JSON.stringify(
                selected.definition.parameters,
                null,
                2,
              )}</pre>
            <details class="tool-original">
              <summary
                >{t(
                  'systemPrompt.tools.original',
                  'Complete definition · JSON',
                )}</summary
              >
              <pre class="tool-schema">{JSON.stringify(
                  selected.definition,
                  null,
                  2,
                )}</pre>
            </details>
          </div>
        </article>
      {:else}
        <EmptyState
          title={t('systemPrompt.tools.noMatches', 'No matching Tools')}
          description={t(
            'systemPrompt.tools.searchHint',
            'Try a Tool name or a word from its description.',
          )}
        />
      {/if}
    </div>
  {/if}
</div>

<style>
  .tool-context-note {
    margin: 0 0 20px;
    color: var(--text-med);
    font-size: var(--fs-body-md);
    line-height: 1.6;
    max-width: 85ch;
  }
  .tool-inspector-layout {
    display: grid;
    grid-template-columns: minmax(200px, 260px) minmax(0, 1fr);
    gap: 20px;
    align-items: start;
  }
  .tool-index {
    position: sticky;
    top: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }
  .tool-index-count,
  .tool-size {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .tool-list {
    display: flex;
    flex-direction: column;
    gap: 4px;
    max-height: 55vh;
    overflow-y: auto;
  }
  .tool-list :global(.tool-index-item) {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 6px;
    padding: 12px;
    border-color: transparent;
    text-align: left;
    white-space: normal;
  }
  .tool-list :global(.tool-index-item[aria-current='true']) {
    background: var(--accent-dim);
    border-color: var(--accent-30);
    color: var(--text-hi);
  }
  .tool-name {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-body);
    overflow-wrap: anywhere;
    color: var(--text-hi);
  }
  .tool-detail {
    min-width: 0;
    border: 1px solid var(--border-2);
    border-radius: var(--r-lg);
    background: var(--surface);
  }
  .tool-detail-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    padding: 20px;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .tool-detail-header h3 {
    margin: 0 0 6px;
    color: var(--text-hi);
    font: 500 var(--fs-body-lg)/1.5 var(--font-mono);
    overflow-wrap: anywhere;
  }
  .tool-detail-body {
    padding: 20px;
  }
  h4 {
    margin: 0 0 12px;
    color: var(--text-med);
    font-size: var(--fs-label-md);
    font-weight: 500;
  }
  .tool-description {
    margin: 0 0 28px;
    font-size: var(--fs-body-lg);
    line-height: 1.7;
    color: var(--text-hi);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .tool-schema {
    margin: 0;
    padding: 16px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    color: var(--text-hi);
    font: var(--fs-mono-body)/1.7 var(--font-mono);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .tool-original {
    margin-top: 20px;
  }
  summary {
    color: var(--text-med);
    cursor: pointer;
    padding: 8px 0;
    font-size: var(--fs-body-md);
  }
  summary:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 4px;
  }
  @media (max-width: 960px) {
    .tool-inspector-layout {
      grid-template-columns: 1fr;
    }
    .tool-index {
      position: static;
    }
    .tool-list {
      max-height: 200px;
    }
  }
  @media (max-width: 640px) {
    .tool-detail-header,
    .tool-detail-body {
      padding: 16px;
    }
    .tool-list :global(.tool-index-item) {
      min-height: 40px;
    }
  }
</style>
