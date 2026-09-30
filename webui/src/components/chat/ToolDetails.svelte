<script>
  // The expanded body of a Tool row. Ordinary Run rows and standalone Tool
  // events share it.
  //
  // A Tool whose display declares detail blocks shows those to the user (the
  // files it changed, notices), with the raw call and result behind a
  // disclosure. Any other Tool shows Args, live Stdout/Stderr and its Result.
  import { toolDetailBlocks } from '$lib/chatToolDetails.js';
  import { t } from '$lib/i18n.js';
  import ToolDetailSection from './ToolDetailSection.svelte';
  import ToolDiff from './ToolDiff.svelte';
  import ToolNotice from './ToolNotice.svelte';
  import { timelineViewState } from './timelineViewState.svelte.js';

  let {
    tool,
    toolName = '',
    args,
    stdout = '',
    stderr = '',
    result,
    resultFailed = false,
    showResult = true,
    // Output is still arriving: its sections keep showing the newest lines.
    live = false,
    // The Tool row's stable key; the raw-call disclosure keeps its state
    // under it while the row is unmounted.
    viewKey = '',
  } = $props();

  const viewState = timelineViewState();

  let blocks = $derived(toolDetailBlocks(tool));
  let rawKey = $derived(`${viewKey}:raw-call`);
</script>

{#snippet argsSection(raw)}
  <ToolDetailSection
    label={t('chat.toolArgs')}
    value={args}
    {raw}
    {toolName}
    {tool}
  />
{/snippet}

{#snippet outputSections()}
  {#if stdout}
    <ToolDetailSection
      label={t('chat.toolStdout')}
      value={stdout}
      follow={live}
    />
  {/if}
  {#if stderr}
    <ToolDetailSection
      label={t('chat.toolStderr')}
      value={stderr}
      isError
      follow={live}
    />
  {/if}
{/snippet}

{#snippet resultSection()}
  {#if showResult}
    <ToolDetailSection
      label={t('chat.toolResultLabel')}
      value={result}
      isError={resultFailed}
      preferPayload
      {toolName}
      {tool}
    />
  {/if}
{/snippet}

<div class="tool-event-body tool-event-details">
  {#if blocks}
    {@render outputSections()}
    {#each blocks as block, index (index)}
      {#if block.type === 'file_changes'}
        <ToolDiff changes={block.files} />
      {:else}
        <ToolNotice notice={block} />
      {/if}
    {/each}
    {#if blocks.length > 0}
      <details
        class="tool-raw-call"
        open={viewState.isOpen(rawKey)}
        ontoggle={(event) =>
          viewState.setOpen(rawKey, event.currentTarget.open)}
      >
        <summary class="tool-raw-call__summary">
          {t('chat.toolRawCall')}
        </summary>
        <div class="tool-raw-call__body">
          {@render argsSection(true)}
          {@render resultSection()}
        </div>
      </details>
    {:else}
      {@render argsSection(true)}
      {@render resultSection()}
    {/if}
  {:else}
    {@render argsSection(false)}
    {@render outputSections()}
    {@render resultSection()}
  {/if}
</div>
