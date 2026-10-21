<script>
  import Banner from '../ui/Banner.svelte';
  import AudioPlayer from '../ui/AudioPlayer.svelte';
  import Button from '../ui/Button.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import { isContextMenuKey } from '../ui/contextMenu.js';
  import { t } from '$lib/i18n.js';
  import { formatAbsoluteTime, formatMoment } from '$lib/timeText.js';
  import { INTENTIONAL_HOVER_SHOW_DELAY_MS, tooltip } from '$lib/tooltip.js';
  import ChangeStats from './ChangeStats.svelte';
  import CopyableValueCard from './CopyableValueCard.svelte';
  import ChatReasoning from './ChatReasoning.svelte';
  import CollapseRail from './CollapseRail.svelte';
  import ToolDetails from './ToolDetails.svelte';
  import ToolPrimaryLine from './ToolPrimaryLine.svelte';
  import ToolSecretsUsed from './ToolSecretsUsed.svelte';
  import {
    backgroundCommandRowState,
    backgroundCommandStatusDetails,
    backgroundCommandToolStatusLabel,
    changeStatsLabel,
    formatTime,
    groupRunChildren,
    isRowCancellable,
    isRunChildWorking,
    latestRunActivity,
    isSubAgentSendTool,
    isSubAgentSpawnTool,
    isTextToSpeechTool,
    isToolPreparing,
    reasoningDurationLabel,
    runChangeStats,
    runFooterDetails,
    runFooterNotice,
    runFooterParts,
    speechArtifactFromTool,
    subAgentAgentId,
    subAgentDescription,
    subAgentDotStatus,
    subAgentLastToolName,
    subAgentNavigationTarget,
    subAgentPreview,
    subAgentStatusDetails,
    subAgentTask,
    subAgentToolStatusLabel,
    timestampForItem,
    toolRowPresentation,
    toolArguments,
    toolNameForRunTool,
    toolStatus,
    toolStatusDetails,
    toolStatusLabel,
    visibleRunChildren,
    goalEventLabel,
    workingGroupDetails,
    workingGroupSummary,
  } from '$lib/chatTimelinePresentation.js';

  import ChatTimelineEntry from './ChatTimelineEntry.svelte';
  import ChatCompactionSeparator from './ChatCompactionSeparator.svelte';
  import ChatSubAgentMessage from './ChatSubAgentMessage.svelte';
  import MarkdownContent from './MarkdownContent.svelte';
  import { timelineViewState } from './timelineViewState.svelte.js';

  let {
    item,
    agentName = '',
    chatWorkingMode = 'normal',
    subAgentStatuses = {},
    isReasoningOpen = () => false,
    onReasoningOpenChange = () => {},
    onNavigateToSubAgent = () => {},
    // Called with (event, target) for a right click or the context menu key
    // on a Sub-Agent link; null keeps the native menu.
    onSubAgentContextMenu = null,
    onCancelToolCall = () => {},
    onBackgroundToolCall = () => {},
    backgroundToolCallIds = [],
    onCancelSubAgent = () => {},
    messageEditingDisabledReason = '',
    onEditMessage = async () => false,
    // Background command status by command id, durable from the Session and
    // live from the server. A view that follows no commands passes neither
    // and keeps their Tool rows plain; otherwise a command missing from both
    // has ended.
    backgroundCommandStatuses = null,
    commandStatuses = null,
    // This is the Session's latest Run and the Session waits for work that
    // will wake it: the footer marks the duration "(Waiting)".
    sessionWaiting = false,
    nowMs = Date.now(),
  } = $props();

  // Disclosure and pending-action state lives in the timeline's view state,
  // so it survives this Run being unmounted and mounted again.
  const viewState = timelineViewState();
  let revealedWorkingGroups = $state({});

  function setWorkingGroupOpen(id, open) {
    if (open) revealedWorkingGroups[id] = true;
    viewState.setOpen(id, open);
  }

  let runDisplayGroups = $derived(
    groupRunChildren(visibleRunChildren(item), chatWorkingMode),
  );

  // The running Run's latest activity, also when this item is one viewport
  // part of a long Run.
  let latestActivityId = $derived(latestRunActivity(item)?.id ?? null);

  let answerCopyText = $derived(
    (item.runItems ?? visibleRunChildren(item))
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

  function handleSubAgentContextMenu(event, tool) {
    if (!onSubAgentContextMenu || event.defaultPrevented) return;
    if (event.type === 'keydown' && !isContextMenuKey(event)) return;
    const target = subAgentNavigationTarget(tool);
    if (target) onSubAgentContextMenu(event, target);
  }

  function actionKey(kind, id) {
    return `action:${item?.runId ?? ''}:${kind}:${id ?? ''}`;
  }

  async function handleBackgroundToolCall(event, tool) {
    event.preventDefault();
    event.stopPropagation();
    const key = actionKey('background', tool.toolCallId);
    if (viewState.isPending(key)) return;
    await viewState.runPending(key, () =>
      onBackgroundToolCall({
        runId: item.runId,
        toolCallId: tool.toolCallId,
      }),
    );
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
    await viewState.runPending(actionKey('tool', toolCallId), () =>
      onCancelToolCall({ runId, toolCallId }),
    );
  }

  async function handleCancelSubAgent(event, tool) {
    event.preventDefault();
    event.stopPropagation();

    await viewState.runPending(
      actionKey('subagent', tool?.toolCallId ?? tool?.id),
      () => onCancelSubAgent({ tool }),
    );
  }

  function toolDisclosureKey(tool) {
    return `tool:${tool.id}`;
  }

  function workingGroupIsActive(group) {
    return (
      latestActivityId !== null &&
      latestActivityId === group.children.at(-1)?.id
    );
  }

  // The name a Tool row shows, also listed by the Working line.
  function runToolName(tool) {
    if (isSubAgentSpawnTool(tool)) return t('chat.subagent.label');
    if (isSubAgentSendTool(tool)) return t('chat.subagent.sendLabel');
    return toolNameForRunTool(tool);
  }

  // The dot state a Tool row shows, also summarized by the Working line.
  function toolRowStatus(tool) {
    if (isSubAgentSpawnTool(tool)) {
      return subAgentDotStatus(tool, subAgentStatuses);
    }
    if (isSubAgentSendTool(tool)) return toolStatus(tool);
    return (
      backgroundCommandRowState(
        tool,
        backgroundCommandStatuses,
        commandStatuses,
      )?.dotStatus ?? toolStatus(tool)
    );
  }

  function thoughtsLabel(count) {
    return count === 1
      ? t('chat.working.thoughtOne')
      : t('chat.working.thoughtCount', { count });
  }

  function summarizeWorkingGroup(group) {
    return workingGroupSummary(group, {
      active: workingGroupIsActive(group),
      nowMs,
      rowStatus: toolRowStatus,
      toolName: runToolName,
    });
  }
</script>

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
  {#if item.segment_first !== false}
    <div class="msg-header">
      <span class="msg-author"
        >{agentName || t('chat.role.assistant').toUpperCase()}</span
      >
      {#if formatTime(timestampForItem(item))}
        <button
          type="button"
          class="msg-timestamp"
          use:tooltip={() => formatMoment(timestampForItem(item))}
          >{formatTime(timestampForItem(item))}</button
        >
      {/if}
      {#if answerCopyText}
        <CopyButton
          text={answerCopyText}
          class="chat-copy-action message-copy"
          label={t('chat.copyAnswer')}
          copiedLabel={t('chat.answerCopied')}
        />
      {/if}
    </div>
  {/if}
  <div class="msg-content assistant-run-content">
    {#snippet runChild(child)}
      {#if child.type === 'history_gap'}
        <!-- The timeline's scroll controller loads the omitted steps once
             this marker comes near the visible area. -->
        <div
          class="run-history-gap"
          aria-hidden="true"
          data-history-gap={child.run_id}
          data-history-gap-before={child.before}
        ></div>
      {:else if child.type === 'reasoning'}
        {@const working = isRunChildWorking(item, child)}
        <ChatReasoning
          source={child.content ?? ''}
          summary={child.reasoningSummary}
          {working}
          open={isReasoningOpen(child.id)}
          durationLabel={reasoningDurationLabel(child, nowMs)}
          onOpenChange={(open) => onReasoningOpenChange(child.id, open)}
        />
      {:else if child.type === 'tool_call'}
        {#if isSubAgentSpawnTool(child)}
          {@const dotStatus = subAgentDotStatus(child, subAgentStatuses)}
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
          {@const statusDetails = () =>
            subAgentStatusDetails(
              child,
              dotStatus,
              subAgentStatuses,
              Date.now(),
            )}
          {@const task = subAgentTask(child)}
          {@const description = subAgentDescription(child)}
          <details
            class="tool-event run-tool-event subagent-tool-event"
            open={viewState.isOpen(toolDisclosureKey(child))}
            ontoggle={(event) =>
              viewState.setOpen(
                toolDisclosureKey(child),
                event.currentTarget.open,
              )}
          >
            <summary class="tool-event-line subagent-line">
              <span
                class:done={dotStatus === 'success'}
                class:error={dotStatus === 'failed'}
                class:cancelled={dotStatus === 'cancelled'}
                class:interrupted={dotStatus === 'interrupted'}
                class:running={dotStatus === 'running'}
                class="te-dot"
                use:tooltip={statusDetails}>●</span
              >
              <span class="te-fn">
                {t('chat.subagent.label')}
              </span>
              <span class="subagent-agent">
                {subAgentAgentId(child)}
              </span>
              {#if description}
                <span
                  class="te-arg subagent-preview"
                  use:tooltip={{ text: description, whenTruncated: true }}
                >
                  {description}
                </span>
              {:else if !lastToolName && subAgentPreview(child)}
                <!-- A call from before titles were required: the preview
                     must receive focus so the complete task and its Copy
                     action reach keyboard users. -->
                <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
                <span
                  class="te-arg subagent-preview"
                  tabindex={task ? 0 : undefined}
                >
                  {subAgentPreview(child)}<CopyableValueCard
                    value={task}
                    copyLabel={t('chat.subagent.copyTask')}
                    copiedLabel={t('chat.subagent.taskCopied')}
                    whenTruncated={subAgentPreview(child) === task}
                    showDelayMs={INTENTIONAL_HOVER_SHOW_DELAY_MS}
                  />
                </span>
              {/if}
              {#if lastToolName}
                <span
                  class="te-arg subagent-activity"
                  use:tooltip={statusDetails}
                >
                  {lastToolName}
                </span>
              {/if}
              {#if subAgentNavigationTarget(child)}
                <Button
                  variant="tertiary"
                  icon
                  class="tool-row-action subagent-session-action subagent-link"
                  tooltip={t('chat.subagent.openSession')}
                  ariaLabel={t('chat.subagent.openSession')}
                  onClick={(event) => handleSubAgentNavigate(event, child)}
                  oncontextmenu={(event) =>
                    handleSubAgentContextMenu(event, child)}
                  onkeydown={(event) => handleSubAgentContextMenu(event, child)}
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
              {/if}
              {#if subAgentTimeLabel}
                <span
                  class="te-time"
                  class:cancelled={dotStatus === 'cancelled'}
                  class:interrupted={dotStatus === 'interrupted'}
                  use:tooltip={statusDetails}
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
                  loading={viewState.isPending(
                    actionKey('subagent', child?.toolCallId ?? child?.id),
                  )}
                  tooltip={t('chat.cancelSubAgentAria')}
                  ariaLabel={t('chat.cancelSubAgentAria')}
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
            <ToolDetails
              tool={child}
              open={viewState.isOpen(toolDisclosureKey(child))}
              toolName={toolNameForRunTool(child)}
              args={toolArguments(child)}
              output={child.output}
              result={child.result}
              resultFailed={toolStatus(child) === 'failed'}
              live={toolStatus(child) === 'running'}
              viewKey={toolDisclosureKey(child)}
            />
          </details>
        {:else if isSubAgentSendTool(child)}
          {@const sendStatus = toolStatus(child)}
          {@const sendTimeLabel = toolStatusLabel(child, nowMs)}
          {@const sendStatusDetails = () =>
            toolStatusDetails(child, Date.now())}
          {@const message = subAgentTask(child)}
          {@const sendTarget = subAgentNavigationTarget(child)}
          <details
            class="tool-event run-tool-event subagent-tool-event"
            open={viewState.isOpen(toolDisclosureKey(child))}
            ontoggle={(event) =>
              viewState.setOpen(
                toolDisclosureKey(child),
                event.currentTarget.open,
              )}
          >
            <summary class="tool-event-line subagent-line">
              <span
                class:done={sendStatus === 'success'}
                class:error={sendStatus === 'failed'}
                class:cancelled={sendStatus === 'cancelled'}
                class:interrupted={sendStatus === 'interrupted'}
                class:running={sendStatus === 'running'}
                class="te-dot"
                use:tooltip={sendStatusDetails}>●</span
              >
              <span class="te-fn">
                {t('chat.subagent.sendLabel')}
              </span>
              {#if sendTarget}
                <span class="subagent-agent">
                  {sendTarget.agentId}
                </span>
              {/if}
              {#if subAgentPreview(child)}
                <!-- The preview must receive focus so the complete message
                     and its Copy action reach keyboard users. -->
                <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
                <span
                  class="te-arg subagent-preview"
                  tabindex={message ? 0 : undefined}
                >
                  {subAgentPreview(child)}<CopyableValueCard
                    value={message}
                    copyLabel={t('chat.subagent.copyMessage')}
                    copiedLabel={t('chat.subagent.messageCopied')}
                    whenTruncated={subAgentPreview(child) === message}
                    showDelayMs={INTENTIONAL_HOVER_SHOW_DELAY_MS}
                  />
                </span>
              {/if}
              {#if sendTarget}
                <Button
                  variant="tertiary"
                  icon
                  class="tool-row-action subagent-session-action subagent-link"
                  tooltip={t('chat.subagent.openSession')}
                  ariaLabel={t('chat.subagent.openSession')}
                  onClick={(event) => handleSubAgentNavigate(event, child)}
                  oncontextmenu={(event) =>
                    handleSubAgentContextMenu(event, child)}
                  onkeydown={(event) => handleSubAgentContextMenu(event, child)}
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
              {/if}
              {#if sendTimeLabel}
                <span
                  class="te-time"
                  class:cancelled={sendStatus === 'cancelled'}
                  class:interrupted={sendStatus === 'interrupted'}
                  use:tooltip={sendStatusDetails}
                >
                  {sendTimeLabel}
                </span>
              {/if}
            </summary>
            <ToolDetails
              tool={child}
              open={viewState.isOpen(toolDisclosureKey(child))}
              toolName={toolNameForRunTool(child)}
              args={toolArguments(child)}
              output={child.output}
              result={child.result}
              resultFailed={sendStatus === 'failed'}
              live={sendStatus === 'running'}
              viewKey={toolDisclosureKey(child)}
            />
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
          {@const commandRowState = backgroundCommandRowState(
            child,
            backgroundCommandStatuses,
            commandStatuses,
          )}
          {@const rowDotStatus =
            commandRowState?.dotStatus ?? toolStatus(child)}
          {@const rowTimeLabel = commandRowState
            ? backgroundCommandToolStatusLabel(child, commandRowState, nowMs)
            : toolStatusLabel(child, nowMs)}
          {@const rowStatusDetails = () =>
            commandRowState
              ? backgroundCommandStatusDetails(
                  child,
                  commandRowState,
                  Date.now(),
                )
              : toolStatusDetails(child, Date.now())}
          <details
            class="tool-event run-tool-event"
            open={viewState.isOpen(toolDisclosureKey(child))}
            ontoggle={(event) =>
              viewState.setOpen(
                toolDisclosureKey(child),
                event.currentTarget.open,
              )}
          >
            <summary class="tool-event-line">
              <span
                class:done={rowDotStatus === 'success'}
                class:error={rowDotStatus === 'failed'}
                class:partial={rowDotStatus === 'partial'}
                class:refused={rowDotStatus === 'refused'}
                class:cancelled={rowDotStatus === 'cancelled'}
                class:interrupted={rowDotStatus === 'interrupted'}
                class:ended={rowDotStatus === 'ended'}
                class:preparing
                class:running={rowDotStatus === 'running' && !preparing}
                class="te-dot"
                use:tooltip={rowStatusDetails}>●</span
              >
              <span class="te-fn">{toolNameForRunTool(child)}</span>
              {#if rowPresentation.primary.length > 0}
                <ToolPrimaryLine primary={rowPresentation.primary} />
              {/if}
              {@render toolFacts(rowPresentation.facts)}
              <ToolSecretsUsed uses={rowPresentation.secretsUsed} />
              {#if rowTimeLabel}
                <span
                  class="te-time"
                  class:cancelled={rowDotStatus === 'cancelled'}
                  class:interrupted={rowDotStatus === 'interrupted'}
                  class:partial={rowDotStatus === 'partial'}
                  class:refused={rowDotStatus === 'refused'}
                  use:tooltip={rowStatusDetails}
                >
                  {rowTimeLabel}
                </span>
              {/if}
              {#if isToolCancellable}
                {#if backgroundToolCallIds.includes(child.toolCallId)}
                  <Button
                    variant="tertiary"
                    icon
                    class="tool-row-action"
                    loading={viewState.isPending(
                      actionKey('background', child.toolCallId),
                    )}
                    tooltip={t('chat.moveToBackground')}
                    ariaLabel={t('chat.moveToBackground')}
                    onClick={(event) => handleBackgroundToolCall(event, child)}
                  >
                    <svg
                      viewBox="0 0 16 16"
                      width="13"
                      height="13"
                      aria-hidden="true"
                    >
                      <path d="M2 6V2h8M6 6h8v8H6zM3 3l6 6M5 9h4V5" />
                    </svg>
                  </Button>
                {/if}
                <Button
                  variant="danger"
                  icon
                  class="tool-row-action row-cancel"
                  data-cancel="tool"
                  loading={viewState.isPending(
                    actionKey('tool', child?.toolCallId),
                  )}
                  tooltip={t('chat.cancelToolCallAria')}
                  ariaLabel={t('chat.cancelToolCallAria')}
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
            <ToolDetails
              tool={child}
              open={viewState.isOpen(toolDisclosureKey(child))}
              toolName={toolNameForRunTool(child)}
              args={toolArguments(child)}
              output={child.output}
              result={child.result}
              resultFailed={rowDotStatus === 'failed'}
              live={toolStatus(child) === 'running'}
              viewKey={toolDisclosureKey(child)}
            />
          </details>
          {#if isTextToSpeechTool(child)}
            {@const speechArtifact = speechArtifactFromTool(child)}
            {@const playback = ['running', 'success'].includes(child.status)
              ? child.speechPlayback
              : ''}
            {#if speechArtifact || playback}
              <!-- Only a live Run starts its fresh speech, once per source: a
                   player first mounted for a Run rebuilt from Session history,
                   or mounted again after scrolling away, stays paused, while
                   one kept through the handoff keeps playing. -->
              <AudioPlayer
                class="speech-audio-player"
                src={speechArtifact?.url ?? ''}
                {playback}
                cancelled={['cancelled', 'interrupted', 'failed'].includes(
                  item.status,
                )}
                autoplay={item.source === 'live' &&
                  !['cancelled', 'interrupted', 'failed'].includes(
                    item.status,
                  ) &&
                  viewState.claimAutoplay(
                    `${item.runId ?? item.id}:${child.toolCallId ?? child.id}`,
                  )}
              />
            {/if}
          {/if}
        {/if}
      {:else if child.type === 'user_message'}
        <ChatTimelineEntry
          item={{ type: 'message', message: child.message }}
          {messageEditingDisabledReason}
          {onEditMessage}
        />
      {:else if child.type === 'assistant_output'}
        {@const working = isRunChildWorking(item, child)}
        <MarkdownContent
          contentId={child.id}
          source={child.content ?? ''}
          streaming={working}
          caret={working}
          class={`msg-markdown${working ? ' streaming-text' : ''}`}
        />
      {:else if child.type === 'model_fallback'}
        <Banner variant="info" class="run-inline-banner">
          {child.from_model && child.retry_at
            ? t('chat.modelFallbackUntil', {
                from: child.from_model,
                to: child.to_model,
                time: formatAbsoluteTime(child.retry_at),
              })
            : child.from_model
              ? t('chat.modelFallbackFrom', {
                  from: child.from_model,
                  to: child.to_model,
                })
              : t('chat.modelFallbackActivated', {
                  model: child.to_model,
                })}
        </Banner>
      {:else if child.type === 'compaction_separator'}
        <ChatCompactionSeparator item={child} inRun />
      {:else if child.type === 'goal_event'}
        <div class="date-sep goal-sep run-goal-sep">
          {goalEventLabel(child)}
        </div>
      {:else if child.type === 'subagent_message'}
        <ChatSubAgentMessage
          item={child}
          {onNavigateToSubAgent}
          {onSubAgentContextMenu}
        />
      {/if}
    {/snippet}
    {#each runDisplayGroups as group (group.id)}
      {#if group.type === 'working'}
        {@const summary = summarizeWorkingGroup(group)}
        {@const groupOpen = viewState.isOpen(group.id)}
        <details
          class="working-block"
          open={groupOpen}
          ontoggle={(event) =>
            setWorkingGroupOpen(group.id, event.currentTarget.open)}
        >
          <summary
            class="working-block__summary tool-event-line"
            use:tooltip={() => workingGroupDetails(summary)}
          >
            <!-- A heading over the rows it folds: a spinner and "Working…",
                 or "Thinking…" while the Agent thinks, while it is still at it
                 here, else a Tool icon; then how many calls, a chip per Tool
                 used, how many thoughts and how long it took. Row states stay
                 on the rows themselves. -->
            {#if summary.active}
              <svg
                class="working-block__icon working-block__icon--active"
                viewBox="1.4 1.4 13.2 13.2"
                aria-hidden="true"
              >
                <path d="M8 2.5a5.5 5.5 0 1 0 5.5 5.5" />
              </svg>
              <span class="working-block__label">
                {summary.thinking
                  ? t('chat.working.thinking')
                  : t('chat.working.active')}
              </span>
            {:else}
              <svg
                class="working-block__icon"
                viewBox="1.9 1.6 12.8 12.8"
                aria-hidden="true"
              >
                <path
                  d="M10.5 2.5a3 3 0 0 0-2.8 4.1l-4.9 4.9a1.2 1.2 0 0 0 1.7 1.7l4.9-4.9a3 3 0 0 0 4.1-2.8l-1.8 1.8-1.7-.4-.4-1.7z"
                />
              </svg>
            {/if}
            <span class="working-block__count">
              {#if summary.toolCount > 0}
                {summary.toolCount === 1
                  ? t('chat.working.toolCallOne')
                  : t('chat.working.toolCallCount', {
                      count: summary.toolCount,
                    })}
              {:else}
                {thoughtsLabel(summary.thoughtCount)}
              {/if}
            </span>
            {#if summary.toolKinds.length > 0}
              <span class="working-block__kinds">
                {#each summary.toolKinds as kind (kind.name)}
                  <span class="working-block__chip"
                    >{kind.name}{#if kind.count > 1}<span
                        class="working-block__chip-count">{kind.count}</span
                      >{/if}</span
                  >
                {/each}
                {#if summary.otherToolKinds.length > 0}
                  <span class="working-block__chip working-block__chip--more"
                    >{t('chat.working.moreTools', {
                      count: summary.otherToolKinds.length,
                    })}</span
                  >
                {/if}
              </span>
            {/if}
            {#if summary.toolCount > 0 && summary.thoughtCount > 0}
              <span class="working-block__note">
                {thoughtsLabel(summary.thoughtCount)}
              </span>
            {/if}
            {#if summary.failedCount > 0}
              <span class="working-block__note">
                {t('chat.working.failedCount', {
                  count: summary.failedCount,
                })}
              </span>
            {/if}
            {#if summary.runningCount > 0}
              <span class="working-block__note">
                {t('chat.working.runningCount', {
                  count: summary.runningCount,
                })}
              </span>
            {/if}
            {#if summary.durationLabel}
              <span class="working-block__time">
                {summary.active
                  ? summary.durationLabel
                  : t('chat.working.took', {
                      duration: summary.durationLabel,
                    })}
              </span>
            {/if}
            <svg
              class="working-block__chevron"
              viewBox="0 0 16 16"
              width="10"
              height="10"
              style:transform={groupOpen ? 'rotate(180deg)' : 'none'}
              aria-hidden="true"
            >
              <path d="M4 6l4 4 4-4" />
            </svg>
          </summary>
          <div class="working-block__body collapse-host">
            <CollapseRail />
            {#each group.children as child (child.id)}
              {#if groupOpen || revealedWorkingGroups[group.id] || isTextToSpeechTool(child)}
                <!-- Fresh speech starts even in a never-opened Working group.
                     Its keyed row stays mounted when that group opens. -->
                {@render runChild(child)}
              {/if}
            {/each}
          </div>
        </details>
      {:else}
        {@render runChild(group.child)}
      {/if}
    {/each}
    {#if item.segment_tail !== false && runFooterParts( item, nowMs, { waiting: sessionWaiting } ).length > 0}
      {@const footerParts = runFooterParts(item, nowMs, {
        waiting: sessionWaiting,
      })}
      {@const changeStats = runChangeStats(item)}
      {@const footerLabel = [
        ...footerParts,
        ...(changeStats ? [changeStatsLabel(changeStats)] : []),
      ].join(' · ')}
      <div class="run-footer" aria-label={footerLabel}>
        <span
          class="run-footer__summary"
          use:tooltip={() => runFooterDetails(item, Date.now())}
        >
          {#each footerParts as footerPart, index (footerPart)}
            {#if index > 0}
              <span class="run-footer__sep" aria-hidden="true">·</span>
            {/if}
            <span class="run-footer__part">{footerPart}</span>
          {/each}
        </span>
        {#if changeStats}
          <span class="run-footer__sep" aria-hidden="true">·</span>
          <ChangeStats stats={changeStats} class="run-footer__changes" />
        {/if}
      </div>
      {#if runFooterNotice(item, nowMs)}
        <!-- Transient problem/liveness notices live on their own line so they
             can never push the stable footer parts onto a wrap line. -->
        <div class="run-footer__notice">{runFooterNotice(item, nowMs)}</div>
      {/if}
    {/if}
  </div>
</article>
