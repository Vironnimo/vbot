<script>
  import { toolDetailImages } from '$lib/chatToolDetails.js';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import { t } from '$lib/i18n.js';
  import { reasoningMarkdownSource } from '$lib/markdown.js';
  import {
    INTENTIONAL_HOVER_SHOW_DELAY_MS,
    floatingHoverCard,
    tooltip,
  } from '$lib/tooltip.js';
  import {
    avatarForItem,
    backgroundBashDisplayResult,
    backgroundBashRowState,
    backgroundBashToolStatusLabel,
    changeStatsLabel,
    changeStatsParts,
    changeStatsTooltip,
    formatTime,
    isRowCancellable,
    isRunChildWorking,
    isStartingForegroundSubAgent,
    isSubAgentSpawnTool,
    isTextToSpeechTool,
    isToolPreparing,
    reasoningDurationLabel,
    runChangeStats,
    runFooterNotice,
    runFooterParts,
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
    toolDetailPresentation,
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
    chatWorkingMode = 'normal',
    subAgentStatuses = {},
    subAgentResults = {},
    isReasoningOpen = () => false,
    onReasoningOpenChange = () => {},
    onNavigateToSubAgent = () => {},
    onCancelToolCall = () => {},
    onCancelSubAgent = () => {},
    backgroundBashStatuses = {},
    backgroundBashProcesses = {},
    nowMs = Date.now(),
  } = $props();

  let pendingActions = $state({});
  let workingDisclosureState = $state({});

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

  function setWorkingOpen(groupId, open) {
    workingDisclosureState[groupId] = open;
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
        return isSubAgentSpawnTool(child)
          ? t('chat.subagent.label', 'Sub-agent')
          : toolNameForRunTool(child);
      }
    }
    return '';
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

{#snippet toolDetailSection(
  label,
  value,
  isError = false,
  preferPayload = false,
  toolName = '',
  tool = null,
)}
  {@const images = toolDetailImages(value, { preferPayload, tool })}
  {@const presentation = toolDetailPresentation(value, {
    preferPayload,
    toolName,
    tool,
  })}
  <div
    class="teb-row teb-section"
    class:teb-section--error={isError}
    class:teb-section--success={preferPayload && !isError}
  >
    <div class="teb-section-header">
      <span class="teb-label">{label}</span>
      {#if presentation.copyText !== t('chat.toolNoData', '—')}
        <CopyButton
          text={presentation.copyText}
          class="chat-copy-action tool-detail-copy"
          label={t('chat.copyToolField', 'Copy {label}', { label })}
        />
      {/if}
    </div>
    {#if images.length > 0}
      <div class="tool-image-previews">
        {#each images as image (image.src)}
          <a
            class="tool-image-preview"
            href={image.src}
            target="_blank"
            rel="noreferrer"
            aria-label={image.filename}
          >
            <img
              src={image.src}
              alt={image.filename}
              loading="lazy"
              onerror={(event) => {
                event.currentTarget.hidden = true;
              }}
            />
            <span
              class="image-unavailable"
              role="img"
              aria-label={t('chat.image.unavailable', 'Image not available')}
            >
              <svg viewBox="0 0 32 24" aria-hidden="true"
                ><rect x="1" y="1" width="30" height="22" rx="2" /><circle
                  cx="10"
                  cy="8"
                  r="2"
                /><path d="m3 20 8-8 6 6 4-4 8 6M3 2l26 20" /></svg
              >
              <span>{t('chat.image.unavailable', 'Image not available')}</span>
            </span>
            <span>{image.filename}</span>
          </a>
        {/each}
      </div>
    {/if}
    {#if presentation.kind === 'fields'}
      <div class:error={isError} class="teb-code teb-fields">
        {#each presentation.fields as field (field.key)}
          <div class="teb-field">
            <span class="teb-field-key">{field.key}</span>
            <span
              class:error={isError}
              class={`teb-field-value teb-field-value--${field.kind}`}
              >{field.text}</span
            >
          </div>
        {/each}
      </div>
    {:else}
      <span
        class:error={isError}
        class={`teb-code teb-text teb-text--${presentation.kind}`}
        >{presentation.text}</span
      >
    {/if}
  </div>
{/snippet}

{#snippet reasoningSummary(
  isStreaming = false,
  isOpen = false,
  durationLabel = '',
)}
  <summary class="reasoning-header">
    <svg class="reasoning-icon" viewBox="0 0 16 16" aria-hidden="true">
      <path
        d="M8 2a4 4 0 0 0-4 4c0 1.5.8 2.8 2 3.5V11h4V9.5A4 4 0 0 0 12 6a4 4 0 0 0-4-4z"
      />
      <path d="M6 13h4" />
    </svg>
    <span
      >{isStreaming
        ? t('chat.reasoning.active', 'thinking...')
        : t('chat.reasoning.done', 'thought')}</span
    >
    {#if durationLabel}
      <span class="reasoning-duration">{durationLabel}</span>
    {/if}
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
    <span class="te-primary-values">
      {#each primary as part, index (`${part.kind}:${index}`)}
        {#if index > 0}<span class="te-primary-separator">·</span>{/if}
        <!-- The truncated value itself must receive focus so the shared
             tooltip exposes its complete plain-text value to keyboard users. -->
        <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
        <span
          class="te-arg-value te-primary-value te-primary-value--{part.truncate}"
          tabindex={part.tooltipText ? 0 : undefined}
          use:tooltip={part.copyable ? '' : part.tooltipText}
        >
          {part.text}{#if part.copyable && part.tooltipText}
            <div
              class="tool-primary-hover-card"
              use:floatingHoverCard={{
                showDelayMs: INTENTIONAL_HOVER_SHOW_DELAY_MS,
              }}
            >
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
    {#snippet runChild(child)}
      {#if child.type === 'reasoning'}
        {@const working = isRunChildWorking(item, child)}
        <details
          class="reasoning-block"
          open={isReasoningOpen(child.id)}
          ontoggle={(event) =>
            onReasoningOpenChange(child.id, event.currentTarget.open)}
        >
          {@render reasoningSummary(
            working,
            isReasoningOpen(child.id),
            reasoningDurationLabel(child, nowMs),
          )}
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
            <div class="tool-event-body tool-event-details">
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
          {@const bashRowState = backgroundBashRowState(
            child,
            backgroundBashStatuses,
            backgroundBashProcesses,
          )}
          {@const rowDotStatus = bashRowState?.dotStatus ?? toolStatus(child)}
          {@const rowTimeLabel = bashRowState
            ? backgroundBashToolStatusLabel(child, bashRowState, nowMs)
            : toolStatusLabel(child, nowMs)}
          <details class="tool-event run-tool-event">
            <summary class="tool-event-line">
              <span
                class:done={rowDotStatus === 'success'}
                class:error={rowDotStatus === 'failed'}
                class:partial={rowDotStatus === 'partial'}
                class:cancelled={rowDotStatus === 'cancelled'}
                class:preparing
                class:running={rowDotStatus === 'running' && !preparing}
                class="te-dot">●</span
              >
              <span class="te-fn">{toolNameForRunTool(child)}</span>
              {#if rowPresentation.primary.length > 0}
                {@render toolPrimaryLine(rowPresentation.primary)}
              {/if}
              {@render toolFacts(rowPresentation.facts)}
              {#if rowTimeLabel}
                <span
                  class="te-time"
                  class:cancelled={rowDotStatus === 'cancelled'}
                  class:partial={rowDotStatus === 'partial'}
                >
                  {rowTimeLabel}
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
            <div class="tool-event-body tool-event-details">
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
                bashRowState
                  ? backgroundBashDisplayResult(child, bashRowState)
                  : child.result,
                rowDotStatus === 'failed',
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
    {/snippet}
    {#each runDisplayGroups as group (group.id)}
      {#if group.type === 'working'}
        {@const groupActive = workingGroupIsActive(group)}
        {@const groupOpen = Boolean(workingDisclosureState[group.id])}
        {@const groupToolName = groupActive ? workingGroupToolName(group) : ''}
        <details
          class="working-block"
          open={groupOpen}
          ontoggle={(event) =>
            setWorkingOpen(group.id, event.currentTarget.open)}
        >
          <summary class="working-block__summary">
            <span class="working-block__label">
              {groupActive
                ? t('chat.working.active', 'working...')
                : t('chat.working.done', 'done working')}
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
      {@const changeParts = changeStatsParts(changeStats)}
      {@const changeTooltip = changeStatsTooltip(changeStats)}
      {@const footerLabel = [
        ...footerParts,
        ...(changeParts.length > 0 ? [changeStatsLabel(changeStats)] : []),
      ].join(' · ')}
      <div class="run-footer" aria-label={footerLabel}>
        {#each footerParts as footerPart, index (footerPart)}
          {#if index > 0}
            <span class="run-footer__sep" aria-hidden="true">·</span>
          {/if}
          <span class="run-footer__part">{footerPart}</span>
        {/each}
        {#if changeParts.length > 0}
          <span class="run-footer__sep" aria-hidden="true">·</span>
          <!-- The change block is focusable so keyboard users reach the
               file-list tooltip; the footer aria-label already carries the
               full summary for screen readers. -->
          <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
          <span
            class="run-footer__changes"
            use:tooltip={changeTooltip}
            tabindex="0"
          >
            {#each changeParts as changePart (changePart.kind)}
              <span
                class="run-footer__part"
                class:run-footer__part--added={changePart.kind === 'added'}
                class:run-footer__part--removed={changePart.kind === 'removed'}
                >{changePart.text}</span
              >
            {/each}
          </span>
        {/if}
      </div>
      {#if runFooterNotice(item)}
        <!-- Transient problem/liveness notices live on their own line so they
             can never push the stable footer parts onto a wrap line. -->
        <div class="run-footer__notice">{runFooterNotice(item)}</div>
      {/if}
    {/if}
  </div>
</article>
