<script>
  // The expanded body of a Tool row. Ordinary Run rows and standalone Tool
  // events share it. It opens with the call's images, videos and audio.
  //
  // A Tool whose display declares detail blocks shows those to the user
  // (texts such as its command and output, the files it changed, what it
  // found, the Memory entries it changed, notices),
  // with the unfiltered raw call and result behind a disclosure. The live
  // Output (the command's newest output lines) takes the place of an Output
  // block, or follows the blocks while the call runs. Any other Tool shows
  // Args, live Output and its Result.
  import { toolDetailBlocks, toolDetailMedia } from '$lib/chatToolDetails.js';
  import { t } from '$lib/i18n.js';
  import CollapseRail from './CollapseRail.svelte';
  import ToolDetailSection from './ToolDetailSection.svelte';
  import ToolDiff from './ToolDiff.svelte';
  import ToolMedia from './ToolMedia.svelte';
  import ToolMemoryChanges from './ToolMemoryChanges.svelte';
  import ToolNotice from './ToolNotice.svelte';
  import ToolResults from './ToolResults.svelte';
  import { timelineViewState } from './timelineViewState.svelte.js';

  let {
    tool,
    open = true,
    toolName = '',
    args,
    // Live output while the call runs: the command's newest output lines.
    output = '',
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
  // First open pays for formatting and media. Keep that mounted afterwards so
  // collapsing a Tool cannot reset playback, selection or internal scroll.
  let revealed = $state(false);
  $effect(() => {
    if (open) revealed = true;
  });

  const TEXT_LABELS = {
    command: () => t('chat.toolDetailLabel.command'),
    content: () => t('chat.toolDetailLabel.content'),
    input: () => t('chat.toolDetailLabel.input'),
    output: () => t('chat.toolDetailLabel.output'),
    page: () => t('chat.toolDetailLabel.page'),
    query: () => t('chat.toolDetailLabel.query'),
    response: () => t('chat.toolDetailLabel.response'),
    results: () => t('chat.toolDetailLabel.results'),
    screen: () => t('chat.toolDetailLabel.screen'),
    scrollback: () => t('chat.toolDetailLabel.scrollback'),
    task: () => t('chat.toolDetailLabel.task'),
  };

  // Readers mostly want a command's output, so its command starts at two
  // lines and opens on request.
  const CLAMPED_TEXT_LINES = { command: 2 };

  let blocks = $derived(toolDetailBlocks(tool, { args, result }));
  let media = $derived(toolDetailMedia(tool, result));
  let streamed = $derived(Boolean(output));
  let streamsReplaceOutput = $derived(
    streamed &&
      Boolean(
        blocks?.some(
          (block) => block.type === 'text' && block.label === 'output',
        ),
      ),
  );
  let rawKey = $derived(`${viewKey}:raw-call`);
</script>

{#snippet argsSection(mode)}
  <ToolDetailSection
    label={t('chat.toolArgs')}
    value={args}
    {mode}
    {toolName}
    {tool}
  />
{/snippet}

{#snippet outputSections()}
  {#if output}
    <ToolDetailSection
      label={t('chat.toolDetailLabel.output')}
      value={output}
      follow={live}
    />
  {/if}
{/snippet}

{#snippet resultSection(mode = 'result')}
  {#if showResult}
    <ToolDetailSection
      label={t('chat.toolResultLabel')}
      value={result}
      isError={resultFailed}
      {mode}
      {toolName}
      {tool}
    />
  {/if}
{/snippet}

{#if open || revealed}
  <div class="tool-event-body tool-event-details collapse-host">
    <CollapseRail />
    <ToolMedia items={media} />
    {#if blocks}
      {#each blocks as block, index (index)}
        {#if block.type === 'text'}
          {#if block.label === 'output' && streamed}
            {@render outputSections()}
          {:else}
            {@const clampKey = `${viewKey}:${block.label}:${index}`}
            <ToolDetailSection
              label={TEXT_LABELS[block.label]()}
              value={block.text}
              mode="literal"
              clampLines={CLAMPED_TEXT_LINES[block.label] ?? 0}
              expanded={viewState.isOpen(clampKey)}
              onExpandedChange={(next) => viewState.setOpen(clampKey, next)}
            />
          {/if}
        {:else if block.type === 'file_changes'}
          <ToolDiff changes={block.files} />
        {:else if block.type === 'results'}
          <ToolResults items={block.items} />
        {:else if block.type === 'memory_changes'}
          <ToolMemoryChanges {block} />
        {:else}
          <ToolNotice notice={block} />
        {/if}
      {/each}
      {#if !streamsReplaceOutput}
        {@render outputSections()}
      {/if}
      {#if blocks.length > 0 || media.length > 0}
        <details
          class="tool-raw-call"
          open={viewState.isOpen(rawKey)}
          ontoggle={(event) =>
            viewState.setOpen(rawKey, event.currentTarget.open)}
        >
          <summary class="tool-raw-call__summary">
            {t('chat.toolRawCall')}
          </summary>
          {#if viewState.isOpen(rawKey)}
            <div class="tool-raw-call__body">
              {@render argsSection('raw')}
              {@render resultSection('raw-result')}
            </div>
          {/if}
        </details>
      {:else}
        {@render argsSection('raw')}
        {@render resultSection()}
      {/if}
    {:else}
      {@render argsSection('arguments')}
      {@render outputSections()}
      {@render resultSection()}
    {/if}
  </div>
{/if}
