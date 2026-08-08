<script>
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import { t } from '$lib/i18n.js';
  import { reasoningMarkdownSource } from '$lib/markdown.js';
  import { floatingHoverCard, tooltip } from '$lib/tooltip.js';
  import {
    avatarForItem,
    compactToolValue,
    formatTime,
    isRowCancellable,
    isRunChildWorking,
    isStartingForegroundSubAgent,
    isSubAgentSpawnTool,
    isTextToSpeechTool,
    isToolPreparing,
    runMetaParts,
    speechArtifactFromTool,
    subAgentAgentId,
    subAgentDisplayResult,
    subAgentDotStatus,
    subAgentLastToolName,
    subAgentNavigationTarget,
    subAgentPreview,
    subAgentResultKey,
    subAgentToolStatusLabel,
    timestampForItem,
    toolRowPresentation,
    toolArguments,
    toolNameForRunTool,
    toolStatus,
    toolStatusLabel,
    visibleRunChildren,
  } from '$lib/chatTimelinePresentation.js';

  import ChatCompactionSeparator from './ChatCompactionSeparator.svelte';
  import MarkdownContent from './MarkdownContent.svelte';

  let {
    item,
    agentName = '',
    subAgentStatuses = {},
    subAgentResults = {},
    isReasoningOpen = () => false,
    onReasoningOpenChange = () => {},
    onNavigateToSubAgent = () => {},
    onCancelToolCall = () => {},
    onCancelSubAgent = () => {},
    nowMs = Date.now(),
  } = $props();

  let pendingActions = $state({});

  let answerCopyText = $derived(
    visibleRunChildren(item)
      .filter((child) => child.type === 'assistant_output')
      .map((child) =>
        typeof child.content === 'string' ? child.content.trim() : '',
      )
      .filter(Boolean)
      .join('\n\n'),
  );

  async function handleSubAgentNavigate(event, tool) {
    event.preventDefault();
    event.stopPropagation();

    const target = subAgentNavigationTarget(tool);
    if (target) {
      await onNavigateToSubAgent(target);
    }
  }

  async function handleCancelToolCall(event, tool) {
    // The cancel button lives inside <details><summary> — keep the disclosure
    // closed/toggled state untouched so the rest of the row keeps its layout.
    event.preventDefault();
    event.stopPropagation();

    const runId = item?.runId ?? '';
    const toolCallId = tool?.toolCallId ?? '';
    if (!runId || !toolCallId) {
      return;
    }
    const actionKey = `tool:${toolCallId}`;
    pendingActions[actionKey] = true;
    try {
      await onCancelToolCall({ runId, toolCallId });
    } finally {
      pendingActions[actionKey] = false;
    }
  }

  async function handleCancelSubAgent(event, tool) {
    event.preventDefault();
    event.stopPropagation();

    const actionKey = `subagent:${tool?.toolCallId ?? tool?.id ?? ''}`;
    pendingActions[actionKey] = true;
    try {
      await onCancelSubAgent({ tool });
    } finally {
      pendingActions[actionKey] = false;
    }
  }
</script>

{#snippet toolDetailSection(
  label,
  value,
  isError = false,
  preferPayload = false,
  toolName = '',
  tool = null,
)}
  {@const displayValue = compactToolValue(value, {
    preferPayload,
    toolName,
    tool,
  })}
  <div class="teb-row">
    <span class="teb-label">{label}</span>
    <span class:error={isError} class="teb-code">{displayValue}</span>
    {#if displayValue !== t('chat.toolNoData', '—')}
      <CopyButton
        text={displayValue}
        class="chat-copy-action tool-detail-copy"
        label={t('chat.copyToolField', 'Copy {label}', { label })}
      />
    {/if}
  </div>
{/snippet}

{#snippet reasoningSummary(isStreaming = false, isOpen = false)}
  <summary class="reasoning-header">
    <svg class="reasoning-icon" viewBox="0 0 16 16" aria-hidden="true">
      <path
        d="M8 2a4 4 0 0 0-4 4c0 1.5.8 2.8 2 3.5V11h4V9.5A4 4 0 0 0 12 6a4 4 0 0 0-4-4z"
      />
      <path d="M6 13h4" />
    </svg>
    <span>{t('chat.event.thinking', 'Thinking').toUpperCase()}</span>
    {#if isStreaming}
      <span class="streaming-caret" aria-hidden="true"></span>
    {/if}
    <svg
      class="r-chevron"
      viewBox="0 0 16 16"
      width="10"
      height="10"
      style:transform={isOpen ? 'rotate(180deg)' : 'none'}
      aria-hidden="true"
    >
      <path d="M4 6l4 4 4-4" />
    </svg>
  </summary>
{/snippet}

{#snippet toolPrimaryLine(primary)}
  <span class="te-arg te-primary">
    <span class="te-arg-mark">(</span>
    <span class="te-primary-values">
      {#each primary as part, index (`${part.kind}:${index}`)}
        {#if index > 0}<span class="te-primary-separator">·</span>{/if}
        <!-- The truncated value itself must receive focus so the shared
             tooltip exposes its complete plain-text value to keyboard users. -->
        <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
        <span
          class="te-arg-value te-primary-value te-primary-value--{part.truncate}"
          class:te-primary-value--quoted={part.quote}
          tabindex={part.tooltipText ? 0 : undefined}
          use:tooltip={part.copyable ? '' : part.tooltipText}
        >
          {#if part.quote}<span class="te-primary-quote">"</span
            >{/if}{part.text}{#if part.quote}<span class="te-primary-quote"
              >"</span
            >{/if}{#if part.copyable && part.tooltipText}
            <div class="tool-primary-hover-card" use:floatingHoverCard>
              <span class="tool-primary-hover-card__value">{part.fullText}</span
              >
              <CopyButton
                text={part.fullText}
                class="tool-primary-hover-card__copy"
                label={t('chat.copyToolValue', 'Copy full value')}
                copiedLabel={t('chat.toolValueCopied', 'Full value copied')}
              />
            </div>
          {/if}</span
        >
      {/each}
    </span>
    <span class="te-arg-mark">)</span>
  </span>
{/snippet}

{#snippet toolFacts(facts)}
  {#each facts as fact, index (`${fact.kind}:${index}`)}
    <span
      class="te-fact"
      class:te-fact--added={fact.variant === 'added'}
      class:te-fact--removed={fact.variant === 'removed'}>{fact.text}</span
    >
  {/each}
{/snippet}

<article class="msg assistant assistant-run">
  <div class="msg-header">
    <div class="msg-avatar">{avatarForItem(item)}</div>
    <span class="msg-author"
      >{agentName || t('chat.role.assistant', 'Assistant').toUpperCase()}</span
    >
    {#if formatTime(timestampForItem(item))}
      <span class="msg-timestamp">{formatTime(timestampForItem(item))}</span>
    {/if}
    {#each runMetaParts(item, nowMs) as metaPart (metaPart)}
      <span class="msg-meta-extra">· {metaPart}</span>
    {/each}
    {#if answerCopyText}
      <CopyButton
        text={answerCopyText}
        class="chat-copy-action message-copy"
        label={t('chat.copyAnswer', 'Copy answer')}
        copiedLabel={t('chat.answerCopied', 'Answer copied')}
      />
    {/if}
  </div>
  <div class="msg-content assistant-run-content">
    {#each visibleRunChildren(item) as child (child.id)}
      {#if child.type === 'reasoning'}
        {@const working = isRunChildWorking(item, child)}
        <details
          class="reasoning-block"
          open={isReasoningOpen(child.id)}
          ontoggle={(event) =>
            onReasoningOpenChange(child.id, event.currentTarget.open)}
        >
          {@render reasoningSummary(working, isReasoningOpen(child.id))}
          <div class="reasoning-body">
            <div class="reasoning-body__actions">
              <CopyButton
                text={reasoningMarkdownSource(child.content ?? '')}
                class="chat-copy-action reasoning-copy"
                label={t('chat.copyReasoning', 'Copy thinking')}
                copiedLabel={t('chat.reasoningCopied', 'Thinking copied')}
              />
            </div>
            <MarkdownContent
              source={child.content ?? ''}
              streaming={working}
              reasoning
              class="reasoning-markdown"
            />
          </div>
        </details>
      {:else if child.type === 'tool_call'}
        {#if isSubAgentSpawnTool(child)}
          {@const dotStatus = subAgentDotStatus(child, subAgentStatuses)}
          {@const subAgentResult =
            subAgentResults[subAgentResultKey(child, subAgentStatuses)]}
          {@const subAgentTimeLabel = subAgentToolStatusLabel(
            child,
            dotStatus,
            subAgentStatuses,
            nowMs,
          )}
          {@const lastToolName =
            dotStatus === 'running'
              ? subAgentLastToolName(child, subAgentStatuses)
              : ''}
          <details class="tool-event run-tool-event subagent-tool-event">
            <summary class="tool-event-line subagent-line">
              <span
                class:done={dotStatus === 'success'}
                class:error={dotStatus === 'failed'}
                class:cancelled={dotStatus === 'cancelled'}
                class:running={dotStatus === 'running'}
                class="te-dot">●</span
              >
              <span class="te-fn">
                {t('chat.subagent.label', 'Sub-agent')}
              </span>
              <span class="subagent-agent">
                {t('agents.form.id', 'Agent ID')}: {subAgentAgentId(child)}
              </span>
              {#if lastToolName}
                <span class="te-arg subagent-preview subagent-activity">
                  {lastToolName}
                </span>
              {:else if subAgentPreview(child)}
                <span class="te-arg subagent-preview">
                  {subAgentPreview(child)}
                </span>
              {/if}
              {#if subAgentNavigationTarget(child)}
                <Button
                  variant="tertiary"
                  icon
                  class="tool-row-action subagent-session-action subagent-link"
                  tooltip={t(
                    'chat.subagent.openSession',
                    'Open Sub-Agent Session',
                  )}
                  ariaLabel={t(
                    'chat.subagent.openSession',
                    'Open Sub-Agent Session',
                  )}
                  onClick={(event) => handleSubAgentNavigate(event, child)}
                >
                  <svg
                    viewBox="0 0 16 16"
                    width="14"
                    height="14"
                    aria-hidden="true"
                  >
                    <path d="M2.5 3.5h7v6h-4l-2.5 2v-2h-.5z" />
                    <path d="M9 6h4.5v4.5M13.5 6 8 11.5" />
                  </svg>
                </Button>
              {:else if dotStatus === 'running' && isStartingForegroundSubAgent(child)}
                <span class="subagent-state">
                  {t('chat.subagent.starting', 'starting')}
                </span>
              {/if}
              {#if subAgentResult?.loading}
                <span class="subagent-state">
                  {t('chat.subagent.loadingResult', 'loading result…')}
                </span>
              {/if}
              {#if subAgentTimeLabel}
                <span
                  class="te-time"
                  class:cancelled={dotStatus === 'cancelled'}
                >
                  {subAgentTimeLabel}
                </span>
              {/if}
              {#if isRowCancellable({ kind: 'sub_agent', dotStatus })}
                <Button
                  variant="danger"
                  icon
                  class="tool-row-action row-cancel"
                  data-cancel="subagent"
                  loading={Boolean(
                    pendingActions[
                      `subagent:${child?.toolCallId ?? child?.id ?? ''}`
                    ],
                  )}
                  tooltip={t(
                    'chat.cancelSubAgentAria',
                    'Cancel running sub-agent',
                  )}
                  ariaLabel={t(
                    'chat.cancelSubAgentAria',
                    'Cancel running sub-agent',
                  )}
                  onClick={(event) => handleCancelSubAgent(event, child)}
                >
                  <svg
                    viewBox="0 0 16 16"
                    width="13"
                    height="13"
                    aria-hidden="true"
                  >
                    <path d="m4 4 8 8M12 4l-8 8" />
                  </svg>
                </Button>
              {/if}
            </summary>
            <div class="tool-event-body">
              {@render toolDetailSection(
                t('chat.toolArgs', 'Args'),
                toolArguments(child),
                false,
                false,
                toolNameForRunTool(child),
                child,
              )}
              {#if child.stdout}
                {@render toolDetailSection(
                  t('chat.toolStdout', 'Stdout'),
                  child.stdout,
                )}
              {/if}
              {#if child.stderr}
                {@render toolDetailSection(
                  t('chat.toolStderr', 'Stderr'),
                  child.stderr,
                  true,
                )}
              {/if}
              {@render toolDetailSection(
                t('chat.toolResultLabel', 'Result'),
                subAgentDisplayResult(child, subAgentResult),
                toolStatus(child) === 'failed',
                true,
                toolNameForRunTool(child),
                child,
              )}
            </div>
          </details>
        {:else}
          {@const isToolCancellable = isRowCancellable({
            kind: 'tool_call',
            toolName: toolNameForRunTool(child),
            toolStatus: toolStatus(child),
            streaming: Boolean(child.streaming),
          })}
          {@const preparing = isToolPreparing(child)}
          {@const rowPresentation = toolRowPresentation(child)}
          <details class="tool-event run-tool-event">
            <summary class="tool-event-line">
              <span
                class:done={toolStatus(child) === 'success'}
                class:error={toolStatus(child) === 'failed'}
                class:cancelled={toolStatus(child) === 'cancelled'}
                class:preparing
                class:running={toolStatus(child) === 'running' && !preparing}
                class="te-dot">●</span
              >
              <span class="te-fn">{toolNameForRunTool(child)}</span>
              {#if rowPresentation.primary.length > 0}
                {@render toolPrimaryLine(rowPresentation.primary)}
              {/if}
              {@render toolFacts(rowPresentation.facts)}
              {#if toolStatusLabel(child, nowMs)}
                <span
                  class="te-time"
                  class:cancelled={toolStatus(child) === 'cancelled'}
                >
                  {toolStatusLabel(child, nowMs)}
                </span>
              {/if}
              {#if isToolCancellable}
                <Button
                  variant="danger"
                  icon
                  class="tool-row-action row-cancel"
                  data-cancel="tool"
                  loading={Boolean(
                    pendingActions[`tool:${child?.toolCallId ?? ''}`],
                  )}
                  tooltip={t(
                    'chat.cancelToolCallAria',
                    'Cancel running tool call',
                  )}
                  ariaLabel={t(
                    'chat.cancelToolCallAria',
                    'Cancel running tool call',
                  )}
                  onClick={(event) => handleCancelToolCall(event, child)}
                >
                  <svg
                    viewBox="0 0 16 16"
                    width="13"
                    height="13"
                    aria-hidden="true"
                  >
                    <path d="m4 4 8 8M12 4l-8 8" />
                  </svg>
                </Button>
              {/if}
            </summary>
            <div class="tool-event-body">
              {@render toolDetailSection(
                t('chat.toolArgs', 'Args'),
                toolArguments(child),
                false,
                false,
                toolNameForRunTool(child),
                child,
              )}
              {#if child.stdout}
                {@render toolDetailSection(
                  t('chat.toolStdout', 'Stdout'),
                  child.stdout,
                )}
              {/if}
              {#if child.stderr}
                {@render toolDetailSection(
                  t('chat.toolStderr', 'Stderr'),
                  child.stderr,
                  true,
                )}
              {/if}
              {@render toolDetailSection(
                t('chat.toolResultLabel', 'Result'),
                child.result,
                toolStatus(child) === 'failed',
                true,
                toolNameForRunTool(child),
                child,
              )}
            </div>
          </details>
          {#if isTextToSpeechTool(child)}
            {@const speechArtifact = speechArtifactFromTool(child)}
            {#if speechArtifact}
              <audio
                class="speech-audio-player"
                src={speechArtifact.url}
                controls
                oncanplay={(event) =>
                  event.currentTarget.play().catch(() => {})}
              ></audio>
            {/if}
          {/if}
        {/if}
      {:else if child.type === 'assistant_output'}
        {@const working = isRunChildWorking(item, child)}
        <MarkdownContent
          source={child.content ?? ''}
          streaming={working}
          caret={working}
          class={`msg-markdown${working ? ' streaming-text' : ''}`}
        />
      {:else if child.type === 'model_fallback'}
        <Banner variant="info" class="run-inline-banner">
          {t('chat.modelFallbackActivated', 'Switched to {model}', {
            model: child.to_model,
          })}
        </Banner>
      {:else if child.type === 'compaction_separator'}
        <ChatCompactionSeparator item={child} inRun />
      {/if}
    {/each}
  </div>
</article>
