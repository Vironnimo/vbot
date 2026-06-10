<script>
  import { t } from '$lib/i18n.js';
  import { renderMarkdown, renderMarkdownStreaming } from '$lib/markdown.js';
  import {
    avatarForItem,
    compactToolValue,
    formatTime,
    isRowCancellable,
    isStartingBlockingSubAgent,
    isSubAgentTool,
    isTextToSpeechTool,
    runMetaParts,
    speechArtifactFromTool,
    subAgentAgentId,
    subAgentDisplayResult,
    subAgentDotStatus,
    subAgentNavigationTarget,
    subAgentNeedsStatusVerification,
    subAgentPreview,
    subAgentResultKey,
    subAgentRunId,
    subAgentShouldFetchResult,
    subAgentToolStatusLabel,
    timestampForItem,
    toolArgumentSummary,
    toolArguments,
    toolNameForRunTool,
    toolStatus,
    toolStatusLabel,
    visibleRunChildren,
  } from '$lib/chatTimelinePresentation.js';

  let {
    item,
    agentName = '',
    subAgentStatuses = {},
    subAgentResults = {},
    isReasoningOpen = () => false,
    onReasoningOpenChange = () => {},
    onNavigateToSubAgent = () => {},
    onRequestSubAgentResult = () => {},
    onVerifySubAgentStatus = () => {},
    onRetry = () => {},
    onCancelToolCall = () => {},
    onCancelSubAgent = () => {},
    showRetry = false,
  } = $props();

  function handleSubAgentNavigate(event, tool) {
    event.preventDefault();
    event.stopPropagation();

    const target = subAgentNavigationTarget(tool);
    if (target) {
      onNavigateToSubAgent(target);
    }
  }

  function handleCancelToolCall(event, tool) {
    // The cancel button lives inside <details><summary> — keep the disclosure
    // closed/toggled state untouched so the rest of the row keeps its layout.
    event.preventDefault();
    event.stopPropagation();

    const runId = item?.runId ?? '';
    const toolCallId = tool?.toolCallId ?? '';
    if (!runId || !toolCallId) {
      return;
    }
    onCancelToolCall({ runId, toolCallId });
  }

  function handleCancelSubAgent(event, tool) {
    event.preventDefault();
    event.stopPropagation();

    onCancelSubAgent({ tool });
  }

  // Once a non-blocking sub-agent run finishes (dot flips to success) and we have
  // no fetched result yet, request its final output so it appears automatically.
  const subAgentResultFetchTargets = $derived(
    visibleRunChildren(item)
      .filter((child) => isSubAgentTool(child))
      .filter((child) =>
        subAgentShouldFetchResult(
          child,
          subAgentDotStatus(child, item, subAgentStatuses),
        ),
      )
      .filter((child) => !subAgentResults[subAgentResultKey(child)])
      .map((child) => subAgentNavigationTarget(child))
      .filter(Boolean),
  );

  $effect(() => {
    for (const target of subAgentResultFetchTargets) {
      onRequestSubAgentResult(target.agentId, target.sessionId);
    }
  });

  // A sub-agent row whose "running" dot comes only from the frozen persisted
  // descriptor (no live `run:`/`session:` status has ever arrived) needs a
  // verification round-trip against chat.history so it can settle to a terminal
  // state. The parent (ChatView) owns the once-per-key guard and the history
  // call; we just surface the rows that need it.
  const subAgentVerificationTargets = $derived(
    visibleRunChildren(item)
      .filter((child) => isSubAgentTool(child))
      .filter((child) =>
        subAgentNeedsStatusVerification(
          child,
          subAgentDotStatus(child, item, subAgentStatuses),
          subAgentStatuses,
        ),
      )
      .map((child) => {
        const target = subAgentNavigationTarget(child);
        if (!target) {
          return null;
        }
        return {
          ...target,
          runId: subAgentRunId(child),
        };
      })
      .filter(Boolean),
  );

  $effect(() => {
    for (const target of subAgentVerificationTargets) {
      onVerifySubAgentStatus(target.agentId, target.sessionId, target.runId);
    }
  });
</script>

{#snippet toolDetailSection(
  label,
  value,
  isError = false,
  preferPayload = false,
  toolName = '',
  tool = null,
)}
  <div class="teb-row">
    <span class="teb-label">{label}</span>
    <span class:error={isError} class="teb-code"
      >{compactToolValue(value, { preferPayload, toolName, tool })}</span
    >
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

{#snippet toolArgumentLine(summary)}
  <span class="te-arg">
    <span class="te-arg-mark">(</span>
    <span class="te-arg-value">{summary}</span>
    <span class="te-arg-mark">)</span>
  </span>
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
    {#each runMetaParts(item) as metaPart (metaPart)}
      <span class="msg-meta-extra">· {metaPart}</span>
    {/each}
    {#if showRetry}
      <button type="button" class="retry-btn" onclick={onRetry}
        >{t('chat.retryRun', 'Retry last turn')}</button
      >
    {/if}
  </div>
  <div class="msg-content assistant-run-content">
    {#each visibleRunChildren(item) as child (child.id)}
      {#if child.type === 'reasoning'}
        <details
          class="reasoning-block"
          open={isReasoningOpen(child.id)}
          ontoggle={(event) =>
            onReasoningOpenChange(child.id, event.currentTarget.open)}
        >
          {@render reasoningSummary(
            Boolean(child.streaming),
            isReasoningOpen(child.id),
          )}
          <div class="reasoning-body">{child.content}</div>
        </details>
      {:else if child.type === 'tool_call'}
        {#if isSubAgentTool(child)}
          {@const dotStatus = subAgentDotStatus(child, item, subAgentStatuses)}
          {@const subAgentResult = subAgentResults[subAgentResultKey(child)]}
          {@const subAgentTimeLabel = subAgentToolStatusLabel(
            child,
            dotStatus,
            subAgentStatuses,
          )}
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
              {#if subAgentPreview(child)}
                <span class="te-arg subagent-preview">
                  {subAgentPreview(child)}
                </span>
              {/if}
              {#if subAgentNavigationTarget(child)}
                <button
                  type="button"
                  class="subagent-link"
                  onclick={(event) => handleSubAgentNavigate(event, child)}
                >
                  {t('chat.subagent.viewSession', 'view session')}
                </button>
              {:else if isStartingBlockingSubAgent(child)}
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
                <button
                  type="button"
                  class="row-cancel"
                  data-cancel="subagent"
                  onclick={(event) => handleCancelSubAgent(event, child)}
                  aria-label={t(
                    'chat.cancelSubAgentAria',
                    'Cancel running sub-agent',
                  )}
                >
                  {t('chat.cancelSubAgent', 'Cancel')}
                </button>
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
          })}
          <details class="tool-event run-tool-event">
            <summary class="tool-event-line">
              <span
                class:done={toolStatus(child) === 'success'}
                class:error={toolStatus(child) === 'failed'}
                class:cancelled={toolStatus(child) === 'cancelled'}
                class:running={toolStatus(child) === 'running'}
                class="te-dot">●</span
              >
              <span class="te-fn">{toolNameForRunTool(child)}</span>
              {#if toolArgumentSummary(child)}
                {@render toolArgumentLine(toolArgumentSummary(child))}
              {/if}
              {#if toolStatusLabel(child)}
                <span
                  class="te-time"
                  class:cancelled={toolStatus(child) === 'cancelled'}
                >
                  {toolStatusLabel(child)}
                </span>
              {/if}
              {#if isToolCancellable}
                <button
                  type="button"
                  class="row-cancel"
                  data-cancel="tool"
                  onclick={(event) => handleCancelToolCall(event, child)}
                  aria-label={t(
                    'chat.cancelToolCallAria',
                    'Cancel running tool call',
                  )}
                >
                  {t('chat.cancelToolCall', 'Cancel')}
                </button>
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
        <div class="msg-markdown" class:streaming-text={child.streaming}>
          <!-- eslint-disable-next-line svelte/no-at-html-tags -->
          {@html child.streaming
            ? renderMarkdownStreaming(child.content ?? '')
            : renderMarkdown(child.content ?? '')}
          {#if child.streaming}<span class="streaming-caret" aria-hidden="true"
            ></span>{/if}
        </div>
      {:else if child.type === 'model_fallback'}
        <div class="model-fallback-notice">
          {t('chat.modelFallbackActivated', 'Switched to {model}', {
            model: child.to_model,
          })}
        </div>
      {/if}
    {/each}
  </div>
</article>
