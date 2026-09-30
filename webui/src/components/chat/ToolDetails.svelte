<script>
  // The expanded body of a Tool row: Args, live Stdout/Stderr, the files the
  // Tool changed, and its Result, in that order. Ordinary Run rows and
  // standalone Tool events share it.
  import {
    toolDetailPresentation,
    toolFileChanges,
  } from '$lib/chatToolDetails.js';
  import { t } from '$lib/i18n.js';
  import ToolDetailSection from './ToolDetailSection.svelte';
  import ToolDiff from './ToolDiff.svelte';

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
  } = $props();

  let fileChanges = $derived(toolFileChanges(tool));
  // A diff already shows what an edit Tool's hidden arguments said.
  let showArgs = $derived(
    fileChanges.length === 0 ||
      toolDetailPresentation(args, { toolName, tool }).kind !== 'empty',
  );
</script>

<div class="tool-event-body tool-event-details">
  {#if showArgs}
    <ToolDetailSection
      label={t('chat.toolArgs')}
      value={args}
      {toolName}
      {tool}
    />
  {/if}
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
  {#if fileChanges.length > 0}
    <ToolDiff changes={fileChanges} />
  {/if}
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
</div>
