<script>
  // The expanded body of a Tool row. Ordinary Run rows and standalone Tool
  // events share it. It opens with the call's images, videos and audio.
  //
  // A Tool whose display declares detail blocks shows those to the user
  // (texts such as its command and output, the files it changed, what it
  // found, the Memory entries it changed, notices),
  // with the raw call and result behind a disclosure. The live Output (the
  // command's current screen) takes the place of an Output block, or follows
  // the blocks while the call runs. Any other Tool shows Args, live Output
  // and its Result.
  import { toolDetailBlocks, toolDetailMedia } from '$lib/chatToolDetails.js';
  import { t } from '$lib/i18n.js';
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
    // Live output while the call runs: the command's current screen.
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
  {#if output}
    <ToolDetailSection
      label={t('chat.toolDetailLabel.output')}
      value={output}
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

{#if open || revealed}
  <div class="tool-event-body tool-event-details">
    <ToolMedia items={media} />
    {#if blocks}
      {#each blocks as block, index (index)}
        {#if block.type === 'text'}
          {#if block.label === 'output' && streamed}
            {@render outputSections()}
          {:else}
            <ToolDetailSection
              label={TEXT_LABELS[block.label]()}
              value={block.text}
              literal
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
              {@render argsSection(true)}
              {@render resultSection()}
            </div>
          {/if}
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
{/if}
