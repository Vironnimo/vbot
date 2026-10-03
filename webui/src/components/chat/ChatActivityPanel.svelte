<script>
  import { onDestroy, onMount, tick } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  import {
    backgroundBashStatusDetails,
    backgroundTasks,
    reflectionChangeItems,
    reflectionElapsedLabel,
    reflectionHasChanges,
    reflectionOutcomeLabel,
    subAgentStatusDetails,
  } from '$lib/chatTimelinePresentation.js';
  import { t } from '$lib/i18n.js';
  import { formatMoment } from '$lib/timeText.js';
  import { tooltip } from '$lib/tooltip.js';

  import Button from '../ui/Button.svelte';
  import ChangeStats from './ChangeStats.svelte';
  import CopyableValueCard from './CopyableValueCard.svelte';

  let {
    timelineItems = [],
    subAgentStatuses = {},
    backgroundBashStatuses = {},
    backgroundBashProcesses = {},
    reflectionTasks = [],
    // The displayed Session's change statistics: undefined until read, null
    // when it changed no files. The panel asks for them while it is open.
    sessionStats = undefined,
    onSessionStatsWanted = () => {},
    parentSession = null,
    onNavigateToSubAgent = () => {},
    onNavigateToParentSession = () => {},
    onOpenReflection = () => {},
    onLoadReflectionChanges = () => {},
    onUndoReflection = async () => {},
    onCancelSubAgent = () => {},
    onCancelBackgroundProcess = () => {},
  } = $props();

  let open = $state(false);
  let bashExpanded = $state(null);
  let defaultBashOpen = $derived(
    subagentTasks.length === 0 && reflectionTasks.length === 0,
  );
  const cancellingTaskIds = new SvelteSet();
  // Finished reviews whose change list is open, and those asking to confirm
  // their undo; both are view state only.
  const expandedReviews = new SvelteSet();
  const confirmingUndo = new SvelteSet();
  let tasks = $derived(
    backgroundTasks(
      timelineItems,
      subAgentStatuses,
      backgroundBashStatuses,
      backgroundBashProcesses,
      nowMs,
    ),
  );
  let activeTasks = $derived(
    tasks.filter((task) => task.dotStatus === 'running'),
  );
  let subagentTasks = $derived(
    tasks.filter((task) => task.kind === 'subagent'),
  );
  let activeSubagentCount = $derived(
    subagentTasks.filter((task) => task.dotStatus === 'running').length,
  );
  let bashTasks = $derived(tasks.filter((task) => task.kind === 'bash'));
  let activeBashCount = $derived(
    bashTasks.filter((task) => task.dotStatus === 'running').length,
  );
  let activeReflections = $derived(
    reflectionTasks.filter((row) => row.status === 'running'),
  );
  let finishedReflections = $derived(
    reflectionTasks.filter((row) => row.status !== 'running'),
  );
  let runningTaskCount = $derived(
    activeTasks.length + activeReflections.length,
  );
  $effect(() => {
    onSessionStatsWanted(open);
  });

  const panelId = $props.id();
  const runningLabel = (count) => t('chat.activity.runningCount', { count });

  const togglePanel = () => {
    open = !open;
  };

  const closePanel = () => {
    open = false;
  };

  const handleKeydown = (event) => {
    if (open && event.key === 'Escape') {
      const railElement = document.getElementById(`${panelId}-toggle`);
      if (railElement?.parentElement?.contains(document.activeElement)) {
        railElement.focus();
      }
      closePanel();
    }
  };

  onMount(() => {
    document.addEventListener('keydown', handleKeydown);
  });

  onDestroy(() => {
    if (typeof document !== 'undefined') {
      document.removeEventListener('keydown', handleKeydown);
    }
  });

  // Elapsed times tick only while the panel is open and something is actually
  // running (background Bash rows or reflections). The effect depends on the
  // value-stable `needsClock` boolean — depending on the task lists directly
  // would re-fire on every tick because those lists recompute with `nowMs`.
  let needsClock = $derived(
    open && (activeTasks.length > 0 || activeReflections.length > 0),
  );
  let nowMs = $state(Date.now());
  $effect(() => {
    if (!needsClock) {
      return;
    }
    nowMs = Date.now();
    const intervalId = setInterval(() => {
      nowMs = Date.now();
    }, 1000);
    return () => clearInterval(intervalId);
  });

  const statusLabel = (status) => {
    if (status === 'running') {
      return t('chat.activity.status.running');
    }
    if (status === 'success' || status === 'completed') {
      return t('chat.activity.status.completed');
    }
    if (status === 'failed') {
      return t('chat.activity.status.failed');
    }
    if (status === 'cancelled') {
      return t('chat.activity.status.cancelled');
    }
    if (status === 'interrupted') {
      return t('chat.activity.status.interrupted');
    }
    return t('chat.activity.status.unknown');
  };

  // The panel's status icons key off the shared dot vocabulary; reflection
  // rows arrive with run-status words, so they map onto the same symbols.
  const reflectionDotStatus = (status) =>
    status === 'running'
      ? 'running'
      : status === 'completed'
        ? 'success'
        : status;

  const reflectionScopeLabel = (row) => {
    if (row.scope === 'memory') {
      return t('chat.activity.reflectionScope.memory');
    }
    if (row.scope === 'skill') {
      return t('chat.activity.reflectionScope.skill');
    }
    return t('chat.activity.reflectionScope.combined');
  };

  // Details behind a status icon: the state in words, since when, how long,
  // and for a process its exit code.
  const statusDetails = (task) => {
    const details =
      task.kind === 'bash'
        ? backgroundBashStatusDetails(task.tool, task.rowState, Date.now())
        : task.kind === 'subagent'
          ? subAgentStatusDetails(
              task.tool,
              task.dotStatus,
              subAgentStatuses,
              Date.now(),
            )
          : reflectionStatusDetails(task.row);
    return { ...details, placement: 'left' };
  };

  const reflectionStatusDetails = (row) => {
    const started = formatMoment(row?.startedAt, { seconds: true });
    return {
      title: statusLabel(row?.status),
      rows: started
        ? [{ label: t('chat.details.started'), value: started }]
        : [],
    };
  };

  const reflectionRowLabel = (row) =>
    t('chat.activity.reflectionOpenAria', {
      scope: reflectionScopeLabel(row),
      status: statusLabel(row.status),
    });

  const outcomeToggleId = (row) => `${panelId}-${row.runId}-outcome`;

  const toggleReviewChanges = (row) => {
    if (expandedReviews.has(row.runId)) {
      expandedReviews.delete(row.runId);
      confirmingUndo.delete(row.runId);
      return;
    }
    expandedReviews.add(row.runId);
    onLoadReflectionChanges(row);
  };

  // Leaving the confirmation removes its buttons; focus returns to the
  // review's summary so keyboard users keep their place.
  const leaveUndoConfirmation = async (row) => {
    confirmingUndo.delete(row.runId);
    await tick();
    document.getElementById(outcomeToggleId(row))?.focus();
  };

  const confirmUndo = async (row) => {
    await onUndoReflection(row);
    await leaveUndoConfirmation(row);
  };

  const parentSessionLabel = () =>
    t('chat.activity.openParentSession', {
      session: parentSession?.displayName ?? '',
    });

  const taskLabel = (task) => {
    if (task.kind === 'bash') {
      return t('chat.activity.bashTaskAria', {
        command: task.command,
        status: statusLabel(task.dotStatus),
      });
    }
    return t('chat.activity.taskAria', {
      agent: task.agentId,
      status: statusLabel(task.dotStatus),
    });
  };

  const cancelTaskLabel = (task) => {
    if (task.kind === 'bash') {
      return t('chat.activity.cancelBashAria', { command: task.command });
    }
    return t('chat.activity.cancelSubAgentAria', { agent: task.agentId });
  };

  const isTaskCancelling = (task) => cancellingTaskIds.has(task.id);
  const handleCancelTask = async (task) => {
    if (isTaskCancelling(task)) {
      return;
    }
    cancellingTaskIds.add(task.id);
    try {
      if (task.kind === 'bash') {
        await onCancelBackgroundProcess({
          processId: task.processId,
        });
      } else {
        await onCancelSubAgent({ tool: task.tool });
      }
    } finally {
      cancellingTaskIds.delete(task.id);
    }
  };

  let railLabel = $derived(
    open
      ? t('chat.activity.close')
      : runningTaskCount === 1
        ? t('chat.activity.openOneRunning')
        : runningTaskCount > 1
          ? t('chat.activity.openManyRunning', { count: runningTaskCount })
          : t('chat.activity.open'),
  );
</script>

{#snippet statusIcon(task)}
  <span
    class="chat-activity__status"
    class:chat-activity__status--running={task.dotStatus === 'running'}
    class:chat-activity__status--success={task.dotStatus === 'success'}
    class:chat-activity__status--failed={task.dotStatus === 'failed'}
    class:chat-activity__status--cancelled={task.dotStatus === 'cancelled'}
    data-status={task.dotStatus}
    use:tooltip={() => statusDetails(task)}
    aria-hidden="true"
  >
    {#if task.dotStatus === 'running'}
      <span class="chat-activity__working-dot"></span>
    {:else if task.dotStatus === 'success'}
      <svg viewBox="0 0 16 16" width="14" height="14">
        <path d="m3.5 8.2 2.8 2.8 6.2-6.2" />
      </svg>
    {:else if task.dotStatus === 'cancelled'}
      <svg viewBox="0 0 16 16" width="14" height="14">
        <path d="M4 8h8" />
      </svg>
    {:else if task.dotStatus === 'failed'}
      <svg viewBox="0 0 16 16" width="14" height="14">
        <path d="M8 3.2 13 12H3L8 3.2Z" />
        <path d="M8 6.3v2.8m0 1.6v.1" />
      </svg>
    {:else}
      <svg viewBox="0 0 16 16" width="14" height="14">
        <circle cx="8" cy="8" r="2" />
      </svg>
    {/if}
  </span>
{/snippet}

{#snippet cancelButton(task)}
  {#if task.dotStatus === 'running'}
    <Button
      variant="danger"
      icon
      class="chat-activity__cancel"
      ariaLabel={cancelTaskLabel(task)}
      tooltip={{ text: cancelTaskLabel(task), placement: 'left' }}
      loading={isTaskCancelling(task)}
      data-cancel-kind={task.kind}
      onClick={() => handleCancelTask(task)}
    >
      <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
        <path d="m4.5 4.5 7 7m0-7-7 7" />
      </svg>
    </Button>
  {/if}
{/snippet}

{#snippet taskRow(task)}
  {#if task.kind === 'bash'}
    <div
      class="chat-activity__task-row chat-activity__task-row--bash"
      aria-label={taskLabel(task)}
    >
      <!-- The command must receive focus so its complete text and Copy
           action reach keyboard users. -->
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <span
        class="chat-activity__task-name"
        tabindex={task.fullCommand ? 0 : undefined}
      >
        {task.command}
        {#if task.timeLabel}
          <span class="chat-activity__task-time">· {task.timeLabel}</span>
        {/if}
        <CopyableValueCard
          value={task.fullCommand}
          mono
          copyLabel={t('chat.copyCommand')}
          copiedLabel={t('chat.commandCopied')}
          whenTruncated={task.command === task.fullCommand}
          placement="left"
        />
      </span>
      {@render statusIcon(task)}
      {@render cancelButton(task)}
    </div>
  {:else}
    <div class="chat-activity__task-row">
      <!-- The complete task and its Copy action live in a card beside the
           link; a card cannot sit inside a button. -->
      <span class="chat-activity__task-anchor">
        <Button
          variant="tertiary"
          class="chat-activity__task-link"
          ariaLabel={taskLabel(task)}
          aria-describedby={task.preview
            ? `${panelId}-${task.id}-preview`
            : undefined}
          disabled={!task.target}
          onClick={() => task.target && onNavigateToSubAgent(task.target)}
        >
          <span class="chat-activity__task-copy">
            <span class="chat-activity__task-name">
              {task.agentId}
              {#if task.timeLabel}
                <span class="chat-activity__task-time">· {task.timeLabel}</span>
              {/if}
            </span>
            {#if task.preview}
              <span
                id={`${panelId}-${task.id}-preview`}
                class="chat-activity__task-preview">{task.preview}</span
              >
            {/if}
          </span>
        </Button>
        <CopyableValueCard
          value={task.taskText}
          copyLabel={t('chat.subagent.copyTask')}
          copiedLabel={t('chat.subagent.taskCopied')}
          whenTruncated={task.preview === task.taskText}
          placement="left"
        />
      </span>
      {@render statusIcon(task)}
      {@render cancelButton(task)}
    </div>
  {/if}
{/snippet}

{#snippet reflectionRow(row)}
  <div class="chat-activity__task-row">
    <Button
      variant="tertiary"
      class="chat-activity__task-link"
      ariaLabel={reflectionRowLabel(row)}
      disabled={!row.sessionId}
      onClick={() => row.sessionId && onOpenReflection(row)}
    >
      <span class="chat-activity__task-name">
        {reflectionScopeLabel(row)}
        {#if row.status === 'running' && reflectionElapsedLabel(row.startedAt, nowMs)}
          <span class="chat-activity__reflection-elapsed">
            · {reflectionElapsedLabel(row.startedAt, nowMs)}
          </span>
        {/if}
      </span>
    </Button>
    {@render statusIcon({
      kind: 'reflection',
      row,
      dotStatus: reflectionDotStatus(row.status),
    })}
  </div>
  {#if row.status !== 'running' && row.outcome}
    {@render reviewOutcome(row)}
  {/if}
{/snippet}

{#snippet reviewOutcome(row)}
  {#if !reflectionHasChanges(row.outcome)}
    <p class="chat-activity__outcome">{reflectionOutcomeLabel(row.outcome)}</p>
  {:else}
    {@const expanded = expandedReviews.has(row.runId)}
    {@const changesId = `${panelId}-${row.runId}-changes`}
    <Button
      id={outcomeToggleId(row)}
      variant="tertiary"
      class="chat-activity__outcome chat-activity__outcome-toggle"
      aria-expanded={expanded}
      aria-controls={expanded ? changesId : undefined}
      onClick={() => toggleReviewChanges(row)}
    >
      <svg
        class="chat-activity__disclosure"
        viewBox="0 0 16 16"
        width="12"
        height="12"
        aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg
      >
      {reflectionOutcomeLabel(row.outcome)}
    </Button>
    {#if expanded}
      <div id={changesId} class="chat-activity__changes">
        {#if row.details?.changes}
          <ul class="chat-activity__change-list">
            {#each reflectionChangeItems(row.details?.changes) as item (item.key)}
              <li
                class="chat-activity__change"
                class:chat-activity__change--undone={item.undone}
              >
                <span class="chat-activity__change-head">
                  <span class="chat-activity__change-kind">{item.kind}</span>
                  <span
                    class="chat-activity__change-place"
                    class:chat-activity__change-place--mono={item.mono}
                    >{item.place}</span
                  >
                  {#if item.undone}
                    <span class="chat-activity__change-undone"
                      >{t('chat.activity.change.undone')}</span
                    >
                  {/if}
                </span>
                {#if item.detail}
                  <span
                    class="chat-activity__change-detail"
                    class:chat-activity__change-detail--mono={item.mono}
                    >{item.detail}</span
                  >
                {/if}
              </li>
            {/each}
          </ul>
        {:else if row.details?.loading}
          <p class="chat-activity__changes-note">
            {t('chat.activity.changes.loading')}
          </p>
        {/if}
        {#if row.details?.loadError}
          <p class="chat-activity__changes-error" role="alert">
            {t('chat.activity.changes.loadError', {
              message: row.details?.loadError,
            })}
          </p>
        {/if}
        {#if !row.outcome.undone}
          {#if confirmingUndo.has(row.runId)}
            <div
              class="chat-activity__undo-confirm"
              role="group"
              aria-label={t('chat.activity.undoConfirm')}
            >
              <span class="chat-activity__undo-question"
                >{t('chat.activity.undoConfirm')}</span
              >
              <Button
                variant="danger"
                class="chat-activity__undo-action"
                loading={row.details?.undoing}
                onClick={() => confirmUndo(row)}
              >
                {t('chat.activity.undoConfirmAction')}
              </Button>
              <Button
                variant="tertiary"
                class="chat-activity__undo-action"
                disabled={row.details?.undoing}
                onClick={() => leaveUndoConfirmation(row)}
              >
                {t('chat.activity.undoKeep')}
              </Button>
            </div>
          {:else}
            <Button
              variant="secondary"
              class="chat-activity__undo-action chat-activity__undo"
              loading={row.details?.undoing}
              onClick={() => confirmingUndo.add(row.runId)}
            >
              {t('chat.activity.undo')}
            </Button>
          {/if}
        {/if}
        {#if row.details?.undoError}
          <p class="chat-activity__changes-error" role="alert">
            {row.details?.undoError}
          </p>
        {/if}
      </div>
    {/if}
  {/if}
{/snippet}

<div class:chat-activity--open={open} class="chat-activity">
  <Button
    id={`${panelId}-toggle`}
    variant="tertiary"
    class="chat-activity__rail"
    ariaLabel={railLabel}
    tooltip={{ text: railLabel, placement: 'left' }}
    aria-expanded={open}
    aria-controls={panelId}
    onClick={togglePanel}
  >
    <svg
      class="chat-activity__rail-arrow"
      viewBox="0 0 16 16"
      width="14"
      height="14"
      aria-hidden="true"
    >
      <path d="m10 4-4 4 4 4" />
    </svg>
    {#if runningTaskCount > 0}
      <span class="chat-activity__rail-dot" aria-hidden="true"></span>
    {/if}
  </Button>

  {#if open}
    <aside
      id={panelId}
      class="chat-activity__panel"
      aria-labelledby={`${panelId}-title`}
    >
      <header class="chat-activity__header">
        <h2 id={`${panelId}-title`} class="chat-activity__title">
          {t('chat.activity.title')}
        </h2>
      </header>

      <div class="chat-activity__body">
        {#if parentSession}
          <section
            class="chat-activity__parent"
            aria-labelledby={`${panelId}-parent-title`}
          >
            <h3
              id={`${panelId}-parent-title`}
              class="chat-activity__group-title"
            >
              {t('chat.activity.parentSession')}
            </h3>
            <Button
              variant="tertiary"
              class="chat-activity__parent-link"
              ariaLabel={parentSessionLabel()}
              onClick={() => onNavigateToParentSession(parentSession.target)}
            >
              <svg
                viewBox="0 0 16 16"
                width="14"
                height="14"
                aria-hidden="true"
              >
                <path d="M6.5 4 2.5 8l4 4" />
                <path d="M3 8h6.25a4.25 4.25 0 0 1 4.25 4.25V13" />
              </svg>
              <span
                class="chat-activity__parent-name"
                use:tooltip={{
                  text: parentSession.displayName,
                  whenTruncated: true,
                  placement: 'left',
                }}
              >
                {parentSession.displayName}
              </span>
            </Button>
          </section>
        {/if}

        <section
          class="chat-activity__stats"
          aria-labelledby={`${panelId}-stats-title`}
        >
          <h3 id={`${panelId}-stats-title`} class="chat-activity__group-title">
            {t('chat.activity.statsTitle')}
          </h3>
          {#if sessionStats}
            <!-- The panel sits at the right edge, so its card opens to the left. -->
            <p class="chat-activity__stats-value">
              <ChangeStats stats={sessionStats} placement="left" />
            </p>
          {:else if sessionStats === null}
            <p class="chat-activity__stats-empty">
              {t('chat.activity.statsEmpty')}
            </p>
          {/if}
        </section>

        <div class="chat-activity__tasks">
          {#if tasks.length === 0 && reflectionTasks.length === 0}
            <p class="chat-activity__empty">
              {t('chat.activity.empty')}
            </p>
          {:else}
            {#if subagentTasks.length > 0}
              <section
                class="chat-activity__group chat-activity__group--subagents"
                aria-labelledby={`${panelId}-subagents`}
              >
                <h3
                  id={`${panelId}-subagents`}
                  class="chat-activity__group-title"
                >
                  {t('chat.activity.subagents')}
                  <span class="chat-activity__count"
                    >{subagentTasks.length}</span
                  >
                  {#if activeSubagentCount > 0}
                    <span class="chat-activity__running-count"
                      >{runningLabel(activeSubagentCount)}</span
                    >
                  {/if}
                </h3>
                <ul class="chat-activity__task-list">
                  {#each subagentTasks as task (task.id)}
                    <li>{@render taskRow(task)}</li>
                  {/each}
                </ul>
              </section>
            {/if}
            {#if reflectionTasks.length > 0}
              <section
                class="chat-activity__group chat-activity__group--reflections"
                aria-labelledby={`${panelId}-reflections`}
              >
                <h3
                  id={`${panelId}-reflections`}
                  class="chat-activity__group-title"
                >
                  {t('chat.activity.reflections')}
                  <span class="chat-activity__count"
                    >{reflectionTasks.length}</span
                  >
                </h3>
                <ul class="chat-activity__task-list">
                  {#each [...activeReflections, ...finishedReflections] as row (row.runId)}
                    <li>{@render reflectionRow(row)}</li>
                  {/each}
                </ul>
              </section>
            {/if}
            {#if bashTasks.length > 0}
              <details
                class="chat-activity__group chat-activity__group--bash"
                bind:open={
                  () => bashExpanded ?? defaultBashOpen,
                  (value) => (bashExpanded = value)
                }
              >
                <summary class="chat-activity__group-title">
                  <svg
                    class="chat-activity__disclosure"
                    viewBox="0 0 16 16"
                    width="12"
                    height="12"
                    aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg
                  >
                  {t('chat.activity.bash')}
                  <span class="chat-activity__count">{bashTasks.length}</span>
                  {#if activeBashCount > 0}
                    <span class="chat-activity__running-count"
                      >{runningLabel(activeBashCount)}</span
                    >
                  {/if}
                </summary>
                <ul class="chat-activity__task-list">
                  {#each bashTasks as task (task.id)}
                    <li>{@render taskRow(task)}</li>
                  {/each}
                </ul>
              </details>
            {/if}
          {/if}
        </div>
      </div>
    </aside>
  {/if}
</div>

<style>
  .chat-activity {
    position: absolute;
    z-index: 30;
    top: 50%;
    right: 8px;
    width: 24px;
    height: 40px;
    transform: translateY(-50%);
  }

  .chat-activity--open {
    display: flex;
    align-items: center;
    width: min(308px, calc(100% - 40px));
    height: min(480px, calc(100% - 24px));
  }

  :global(.chat-activity__rail.btn-tertiary) {
    position: absolute;
    top: 50%;
    right: 0;
    display: flex;
    width: 24px;
    min-width: 24px;
    height: 40px;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--secondary-surface);
    color: var(--text-med);
    transform: translateY(-50%);
  }

  .chat-activity--open :global(.chat-activity__rail.btn-tertiary) {
    right: 100%;
    width: 20px;
    min-width: 20px;
    border-right: 0;
    border-radius: var(--r-sm) 0 0 var(--r-sm);
  }

  :global(.chat-activity__rail.btn-tertiary:hover) {
    color: var(--text-hi);
    background: var(--surface-2);
  }
  .chat-activity__rail-arrow {
    flex: 0 0 14px;
  }

  .chat-activity__rail-arrow,
  .chat-activity__disclosure {
    fill: none;
    stroke: currentColor;
    stroke-width: 1.5;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .chat-activity--open .chat-activity__rail-arrow {
    transform: rotate(180deg);
  }
  .chat-activity__rail-dot {
    position: absolute;
    top: 5px;
    right: 4px;
    width: 4px;
    height: 4px;
    border-radius: 50%;
    background: var(--amber);
  }
  .chat-activity__panel {
    display: flex;
    width: 100%;
    max-height: 100%;
    min-height: 0;
    flex-direction: column;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--secondary-surface);
    box-shadow: var(--floating-elevation);
  }
  .chat-activity__header {
    flex: 0 0 auto;
    padding: 14px 16px 8px;
  }
  .chat-activity__title {
    margin: 0;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: var(--fs-body-md);
    font-weight: 600;
  }
  .chat-activity__body {
    min-height: 0;
    overflow-y: auto;
    padding: 0 8px 10px;
    scrollbar-width: thin;
  }
  .chat-activity__parent {
    margin-bottom: 8px;
  }
  :global(.chat-activity__parent-link.btn-tertiary) {
    width: 100%;
    justify-content: flex-start;
    gap: 8px;
    padding: 6px 8px;
    border: 0;
    color: var(--text-med);
  }
  :global(.chat-activity__parent-link svg) {
    flex: 0 0 14px;
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.5;
  }
  .chat-activity__parent-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .chat-activity__stats {
    padding-bottom: 12px;
  }
  .chat-activity__stats-value,
  .chat-activity__stats-empty {
    margin: 0;
    padding: 4px 8px;
    font-size: var(--fs-body-sm);
    font-variant-numeric: tabular-nums;
    color: var(--text-med);
  }
  .chat-activity__empty {
    margin: 0;
    padding: 8px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .chat-activity__group + .chat-activity__group {
    margin-top: 16px;
  }
  .chat-activity__group-title {
    display: flex;
    align-items: center;
    gap: 7px;
    margin: 0;
    padding: 6px 8px;
    color: var(--text-med);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
    font-weight: 500;
  }
  .chat-activity__count {
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
    font-weight: 400;
  }
  .chat-activity__running-count {
    margin-left: auto;
    color: var(--amber);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
    font-weight: 400;
  }
  summary.chat-activity__group-title {
    cursor: pointer;
    list-style: none;
    border-radius: var(--r-sm);
  }
  summary::-webkit-details-marker {
    display: none;
  }
  summary:hover {
    background: var(--surface-2);
    color: var(--text-hi);
  }
  details[open] .chat-activity__disclosure {
    transform: rotate(90deg);
  }
  .chat-activity__task-list {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .chat-activity__task-row {
    display: flex;
    width: 100%;
    min-height: 34px;
    align-items: center;
    gap: 6px;
    padding: 6px 8px;
    border-radius: var(--r-sm);
    color: var(--text-med);
    text-align: left;
  }
  .chat-activity__task-row:has(:global(button:hover)),
  .chat-activity__task-row:focus-within {
    background: var(--surface-2);
  }
  :global(.chat-activity__task-link.btn-tertiary) {
    min-width: 0;
    min-height: 24px;
    flex: 1;
    justify-content: flex-start;
    padding: 0;
    border: 0;
    border-radius: var(--r-sm);
    color: var(--text-hi);
    text-align: left;
  }
  :global(.chat-activity__task-link.btn-tertiary:hover) {
    background: transparent;
  }
  .chat-activity__task-anchor {
    display: flex;
    min-width: 0;
    flex: 1;
  }
  .chat-activity__task-copy {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 3px;
  }
  .chat-activity__task-name {
    min-width: 0;
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--fs-body-sm);
    font-weight: 500;
  }
  .chat-activity__task-preview {
    display: -webkit-box;
    overflow: hidden;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    color: var(--text-med);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
    line-height: 1.4;
    font-weight: 400;
    overflow-wrap: anywhere;
    white-space: normal;
  }
  .chat-activity__task-row--bash .chat-activity__task-name {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }
  .chat-activity__task-time,
  .chat-activity__reflection-elapsed {
    color: var(--text-med);
    font-variant-numeric: tabular-nums;
    font-weight: 400;
  }
  /* The Bash command itself stays mono; its elapsed time is UI metadata. */
  .chat-activity__task-row--bash .chat-activity__task-time {
    font-family: var(--font-ui);
    font-size: var(--fs-label-sm);
  }
  :global(.chat-activity__cancel.btn-danger.btn-icon) {
    width: 24px;
    min-width: 24px;
    height: 24px;
    min-height: 24px;
    flex: 0 0 24px;
    border-color: transparent;
    background: transparent;
    color: var(--text-med);
  }
  :global(.chat-activity__cancel.btn-danger.btn-icon:hover),
  :global(.chat-activity__cancel.btn-danger.btn-icon:focus-visible) {
    background: var(--surface-3);
    color: var(--red);
  }
  :global(.chat-activity__cancel svg) {
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-width: 1.7;
  }
  .chat-activity__status {
    display: inline-flex;
    width: 18px;
    height: 18px;
    flex: 0 0 18px;
    align-items: center;
    justify-content: center;
    color: var(--text-med);
  }
  .chat-activity__status svg {
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.6;
  }
  .chat-activity__working-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--amber);
  }
  .chat-activity__status--success {
    color: var(--green);
  }
  .chat-activity__status--failed {
    color: var(--red);
  }
  .chat-activity__outcome {
    margin: 0;
    padding: 0 8px 6px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    font-variant-numeric: tabular-nums;
  }
  :global(.chat-activity__outcome-toggle.btn-tertiary) {
    min-height: 24px;
    gap: 6px;
    margin: 0 0 4px;
    padding: 2px 8px;
    border: 0;
    border-radius: var(--r-sm);
    color: var(--text-med);
    font-weight: 400;
  }
  :global(.chat-activity__outcome-toggle.btn-tertiary:hover) {
    background: var(--surface-2);
    color: var(--text-hi);
  }
  :global(.chat-activity__outcome-toggle[aria-expanded='true'])
    .chat-activity__disclosure {
    transform: rotate(90deg);
  }
  .chat-activity__changes {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
    padding: 0 8px 10px 26px;
  }
  .chat-activity__change-list {
    display: flex;
    width: 100%;
    flex-direction: column;
    gap: 6px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .chat-activity__change {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 2px;
    font-size: var(--fs-body-sm);
  }
  .chat-activity__change-head {
    display: flex;
    min-width: 0;
    align-items: baseline;
    gap: 6px;
  }
  .chat-activity__change-kind,
  .chat-activity__change-undone {
    flex: 0 0 auto;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
  }
  .chat-activity__change-place {
    min-width: 0;
    overflow: hidden;
    color: var(--text-hi);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .chat-activity__change-place--mono,
  .chat-activity__change-detail--mono {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }
  .chat-activity__change-detail {
    display: -webkit-box;
    overflow: hidden;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 3;
    color: var(--text-med);
    line-height: 1.4;
    overflow-wrap: anywhere;
  }
  .chat-activity__change--undone .chat-activity__change-place,
  .chat-activity__change--undone .chat-activity__change-detail {
    color: var(--text-lo);
    text-decoration: line-through;
  }
  .chat-activity__changes-note,
  .chat-activity__changes-error {
    margin: 0;
    font-size: var(--fs-body-sm);
    line-height: 1.4;
  }
  .chat-activity__changes-note {
    color: var(--text-med);
  }
  .chat-activity__changes-error {
    color: var(--red);
    overflow-wrap: anywhere;
  }
  .chat-activity__undo-confirm {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .chat-activity__undo-question {
    flex: 1 0 100%;
    color: var(--text-hi);
    font-size: var(--fs-body-sm);
  }
  :global(.chat-activity__undo-action) {
    min-height: 26px;
    padding: 2px 10px;
    font-size: var(--fs-body-sm);
  }
  :global(.chat-activity__rail.btn-tertiary:focus-visible),
  :global(.chat-activity__outcome-toggle.btn-tertiary:focus-visible),
  summary:focus-visible,
  :global(.chat-activity__task-link.btn-tertiary:focus-visible),
  :global(.chat-activity__parent-link.btn-tertiary:focus-visible) {
    outline: 1px solid var(--accent);
    outline-offset: -1px;
  }
  @media (max-width: 640px) {
    .chat-activity {
      right: 6px;
    }
    :global(.chat-activity__rail.btn-tertiary) {
      height: 44px;
    }
    .chat-activity--open {
      width: min(308px, calc(100% - 40px));
    }
  }
</style>
