<script>
  import Banner from '../ui/Banner.svelte';
  import AudioPlayer from '../ui/AudioPlayer.svelte';
  import Button from '../ui/Button.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import { t } from '$lib/i18n.js';
  import { formatMoment } from '$lib/timeText.js';
  import { INTENTIONAL_HOVER_SHOW_DELAY_MS, tooltip } from '$lib/tooltip.js';
  import ChangeStats from './ChangeStats.svelte';
  import CopyableValueCard from './CopyableValueCard.svelte';
  import ChatReasoning from './ChatReasoning.svelte';
  import ToolDetails from './ToolDetails.svelte';
  import ToolPrimaryLine from './ToolPrimaryLine.svelte';
  import {
    avatarForItem,
    backgroundCommandRowState,
    backgroundCommandStatusDetails,
    backgroundCommandToolStatusLabel,
    changeStatsLabel,
    formatTime,
    isRowCancellable,
    isRunChildWorking,
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
  } from '$lib/chatTimelinePresentation.js';

  import ChatTimelineEntry from './ChatTimelineEntry.svelte';
  import ChatCompactionSeparator from './ChatCompactionSeparator.svelte';
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
    onCancelToolCall = () => {},
    onBackgroundToolCall = () => {},
    backgroundToolCallIds = [],
    onCancelSubAgent = () => {},
    backgroundCommandStatuses = {},
    commandStatuses = {},
    nowMs = Date.now(),
  } = $props();

  // Disclosure and pending-action state lives in the timeline's view state,
  // so it survives this Run being unmounted and mounted again.
  const viewState = timelineViewState();

  let runDisplayGroups = $derived(
    groupRunChildren(visibleRunChildren(item), chatWorkingMode),
  );

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
    const visibleChildren = visibleRunChildren(item);
    const latestVisibleChild = visibleChildren.at(-1);
    return (
      item?.status === 'running' &&
      latestVisibleChild?.id === group.children.at(-1)?.id
    );
  }

  function workingGroupToolName(group) {
    for (let index = group.children.length - 1; index >= 0; index -= 1) {
      const child = group.children[index];
      if (child.type === 'tool_call') {
        if (isSubAgentSpawnTool(child)) {
          return t('chat.subagent.label');
        }
        return isSubAgentSendTool(child)
          ? t('chat.subagent.sendLabel')
          : toolNameForRunTool(child);
      }
    }
    return '';
  }

  // What a collapsed working block holds, for the tooltip on its summary.
  function workingGroupDetails(group) {
    const toolCalls = group.children.filter(
      (child) => child.type === 'tool_call',
    ).length;
    const reasoning = group.children.length - toolCalls;
    return {
      rows: [
        { label: t('chat.working.toolCalls'), value: String(toolCalls) },
        { label: t('chat.working.reasoning'), value: String(reasoning) },
      ].filter((row) => row.value !== '0'),
    };
  }

  function groupRunChildren(children, workingMode) {
    if (workingMode !== 'compact') {
      return children.map((child) => ({
        id: `child:${child.id}`,
        type: 'child',
        child,
      }));
    }

    const groups = [];
    let workingChildren = [];

    function flushWorkingChildren() {
      if (workingChildren.length === 1) {
        const [child] = workingChildren;
        groups.push({ id: `child:${child.id}`, type: 'child', child });
      } else if (workingChildren.length > 1) {
        groups.push({
          id: `working:${workingChildren[0].id}`,
          type: 'working',
          children: workingChildren,
        });
      }
      workingChildren = [];
    }

    for (const child of children) {
      if (child.type === 'reasoning' || child.type === 'tool_call') {
        workingChildren.push(child);
        continue;
      }

      flushWorkingChildren();
      groups.push({ id: `child:${child.id}`, type: 'child', child });
    }
    flushWorkingChildren();
    return groups;
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
  <div class="msg-header">
    <div class="msg-avatar">{avatarForItem(item)}</div>
    <span class="msg-author"
      >{agentName || t('chat.role.assistant').toUpperCase()}</span
    >
    {#if formatTime(timestampForItem(item))}
      <span
        class="msg-timestamp"
        use:tooltip={() => formatMoment(timestampForItem(item))}
        >{formatTime(timestampForItem(item))}</span
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
  <div class="msg-content assistant-run-content">
    {#snippet runChild(child)}
      {#if child.type === 'reasoning'}
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
                class:running={dotStatus === 'running'}
                class="te-dot"
                use:tooltip={statusDetails}>●</span
              >
              <span class="te-fn">
                {t('chat.subagent.label')}
              </span>
              <span class="subagent-agent">
                {t('agents.form.id')}: {subAgentAgentId(child)}
              </span>
              {#if lastToolName}
                <span
                  class="te-arg subagent-preview subagent-activity"
                  use:tooltip={statusDetails}
                >
                  {lastToolName}
                </span>
              {:else if subAgentPreview(child)}
                <!-- The preview must receive focus so the complete task and
                     its Copy action reach keyboard users. -->
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
              {#if subAgentNavigationTarget(child)}
                <Button
                  variant="tertiary"
                  icon
                  class="tool-row-action subagent-session-action subagent-link"
                  tooltip={t('chat.subagent.openSession')}
                  ariaLabel={t('chat.subagent.openSession')}
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
              {/if}
              {#if subAgentTimeLabel}
                <span
                  class="te-time"
                  class:cancelled={dotStatus === 'cancelled'}
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
                class:running={sendStatus === 'running'}
                class="te-dot"
                use:tooltip={sendStatusDetails}>●</span
              >
              <span class="te-fn">
                {t('chat.subagent.sendLabel')}
              </span>
              {#if sendTarget}
                <span class="subagent-agent">
                  {t('agents.form.id')}: {sendTarget.agentId}
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
                  use:tooltip={sendStatusDetails}
                >
                  {sendTimeLabel}
                </span>
              {/if}
            </summary>
            <ToolDetails
              tool={child}
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
                class:cancelled={rowDotStatus === 'cancelled'}
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
              {#if rowTimeLabel}
                <span
                  class="te-time"
                  class:cancelled={rowDotStatus === 'cancelled'}
                  class:partial={rowDotStatus === 'partial'}
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
            {#if speechArtifact}
              <!-- Only a live Run starts its fresh speech, once per source: a
                   player first mounted for a Run rebuilt from Session history,
                   or mounted again after scrolling away, stays paused, while
                   one kept through the handoff keeps playing. -->
              <AudioPlayer
                class="speech-audio-player"
                src={speechArtifact.url}
                autoplay={item.source === 'live' &&
                  viewState.claimAutoplay(speechArtifact.url)}
              />
            {/if}
          {/if}
        {/if}
      {:else if child.type === 'user_message'}
        <ChatTimelineEntry item={{ type: 'message', message: child.message }} />
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
          {child.from_model
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
      {/if}
    {/snippet}
    {#each runDisplayGroups as group (group.id)}
      {#if group.type === 'working'}
        {@const groupActive = workingGroupIsActive(group)}
        {@const groupOpen = viewState.isOpen(group.id)}
        {@const groupToolName = groupActive ? workingGroupToolName(group) : ''}
        <details
          class="working-block"
          open={groupOpen}
          ontoggle={(event) =>
            viewState.setOpen(group.id, event.currentTarget.open)}
        >
          <summary
            class="working-block__summary"
            use:tooltip={() => workingGroupDetails(group)}
          >
            <span class="working-block__label">
              {groupActive ? t('chat.working.active') : t('chat.working.done')}
            </span>
            {#if groupToolName}
              <span class="working-block__activity">
                {groupToolName}
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
          <div class="working-block__body">
            {#each group.children as child (child.id)}
              {@render runChild(child)}
            {/each}
          </div>
        </details>
      {:else}
        {@render runChild(group.child)}
      {/if}
    {/each}
    {#if runFooterParts(item, nowMs).length > 0}
      {@const footerParts = runFooterParts(item, nowMs)}
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
