<script>
  import { flushSync, onDestroy, onMount, tick } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  import {
    activitySections,
    backgroundCommandStatusDetails,
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
    // What each Sub-Agent still runs besides its Run, by Sub-Agent id, as
    // last read; a row asks for it while the user looks at it.
    subAgentWork = {},
    onSubAgentWorkWanted = () => {},
    backgroundCommandStatuses = {},
    commandStatuses = {},
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
    onCancelBackgroundCommand = () => {},
  } = $props();

  let open = $state(false);
  const cancellingTaskIds = new SvelteSet();
  // Finished reviews whose change list is open, and those asking to confirm
  // their undo; both are view state only.
  const expandedReviews = new SvelteSet();
  const confirmingUndo = new SvelteSet();
  let tasks = $derived(
    backgroundTasks(
      timelineItems,
      subAgentStatuses,
      backgroundCommandStatuses,
      commandStatuses,
      nowMs,
    ),
  );
  // Running work first, then finished work; each entry names its kind.
  let sections = $derived(activitySections(tasks, reflectionTasks));
  let runningTaskCount = $derived(sections.running.length);
  $effect(() => {
    onSessionStatsWanted(open);
  });

  const panelId = $props.id();
  const railElement = () => document.getElementById(`${panelId}-toggle`);

  // The rail hides while the panel is open, and the panel's own close button
  // takes its place; focus moves between the two so keyboard users keep it.
  const openPanel = () => {
    const railHadFocus = document.activeElement === railElement();
    open = true;
    flushSync();
    if (railHadFocus) {
      document.getElementById(`${panelId}-close`)?.focus();
    }
  };

  const closePanel = () => {
    const rail = railElement();
    const focusInside = rail?.parentElement?.contains(document.activeElement);
    open = false;
    flushSync();
    if (focusInside) {
      rail.focus();
    }
  };

  const handleKeydown = (event) => {
    if (open && event.key === 'Escape') {
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
  let needsClock = $derived(open && runningTaskCount > 0);
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
  // and for a command its terminal id.
  const statusDetails = (task) => {
    const details =
      task.kind === 'command'
        ? backgroundCommandStatusDetails(task.tool, task.rowState, Date.now())
        : task.kind === 'subagent'
          ? subAgentStatusDetails(
              task.tool,
              task.dotStatus,
              subAgentStatuses,
              Date.now(),
              subAgentWork[task.subAgentId],
            )
          : reflectionStatusDetails(task.row);
    // A Sub-Agent's title shows on one line; its card carries it in full.
    const text =
      task.kind === 'subagent' && task.description
        ? task.description
        : details.text;
    return { ...details, text, placement: 'left' };
  };

  // The row's card; `_work` only marks which reading of the Sub-Agent's work
  // the card belongs to.
  const rowDetails = (task, _work) => () => statusDetails(task);

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
  const changesId = (row) => `${panelId}-${row.runId}-changes`;

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
    if (task.kind === 'command') {
      return t('chat.activity.commandTaskAria', {
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
    if (task.kind === 'command') {
      return t('chat.activity.cancelCommandAria', { command: task.command });
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
      if (task.kind === 'command') {
        await onCancelBackgroundCommand({
          terminalId: task.terminalId,
        });
      } else {
        await onCancelSubAgent({ tool: task.tool });
      }
    } finally {
      cancellingTaskIds.delete(task.id);
    }
  };

  let railLabel = $derived(
    runningTaskCount === 1
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
    aria-hidden="true"
  >
    {#if task.dotStatus === 'running'}
      <span class="chat-activity__working-dot"></span>
    {:else if task.dotStatus === 'success'}
      <svg viewBox="0 0 16 16" width="11" height="11">
        <path d="m3.5 8.2 2.8 2.8 6.2-6.2" />
      </svg>
    {:else if task.dotStatus === 'cancelled'}
      <svg viewBox="0 0 16 16" width="11" height="11">
        <path d="M4 8h8" />
      </svg>
    {:else if task.dotStatus === 'failed'}
      <svg viewBox="0 0 16 16" width="11" height="11">
        <path d="M8 3.2 13 12H3L8 3.2Z" />
        <path d="M8 6.3v2.8m0 1.6v.1" />
      </svg>
    {:else}
      <svg viewBox="0 0 16 16" width="11" height="11">
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

<!-- Every row reads as two lines: what the work is about, then a quiet line
     with its state symbol, its kind and how long it ran. -->
{#snippet taskRow(task)}
  {#if task.kind === 'command'}
    <div
      class="chat-activity__task-row chat-activity__task-row--bash"
      aria-label={taskLabel(task)}
    >
      <span class="chat-activity__task-copy">
        <!-- The command must receive focus so its complete text and Copy
             action reach keyboard users. -->
        <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
        <span
          class="chat-activity__task-title chat-activity__task-command"
          tabindex={task.fullCommand ? 0 : undefined}
        >
          {task.command}
          <CopyableValueCard
            value={task.fullCommand}
            mono
            copyLabel={t('chat.copyCommand')}
            copiedLabel={t('chat.commandCopied')}
            whenTruncated={task.command === task.fullCommand}
            placement="left"
          />
        </span>
        <span
          class="chat-activity__task-meta"
          use:tooltip={() => statusDetails(task)}
        >
          {@render statusIcon(task)}
          <span class="chat-activity__task-kind"
            >{t('chat.activity.kind.command')}</span
          >
          {#if task.timeLabel}
            <span class="chat-activity__task-time">· {task.timeLabel}</span>
          {/if}
        </span>
      </span>
      {@render cancelButton(task)}
    </div>
  {:else}
    <!-- The whole row shows the Sub-Agent's status details, not only its
         status symbol. -->
    <!-- A new function whenever the Sub-Agent's work was read again, so an
         open card shows what arrived. Pointer and focus only ask for that
         work; the row's controls stay the interactive elements. -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="chat-activity__task-row"
      use:tooltip={rowDetails(task, subAgentWork[task.subAgentId])}
      onpointerenter={() => onSubAgentWorkWanted({ tool: task.tool })}
      onfocusin={() => onSubAgentWorkWanted({ tool: task.tool })}
    >
      <Button
        variant="tertiary"
        class="chat-activity__task-link"
        ariaLabel={taskLabel(task)}
        aria-describedby={task.description
          ? `${panelId}-${task.id}-description`
          : undefined}
        disabled={!task.target}
        onClick={() => task.target && onNavigateToSubAgent(task.target)}
      >
        <span class="chat-activity__task-copy">
          {#if task.description}
            <span
              id={`${panelId}-${task.id}-description`}
              class="chat-activity__task-title chat-activity__task-description"
              >{task.description}</span
            >
          {:else}
            <span class="chat-activity__task-title">{task.agentId}</span>
          {/if}
          <span class="chat-activity__task-meta">
            {@render statusIcon(task)}
            <span class="chat-activity__task-kind"
              >{t('chat.activity.kind.subagent')}</span
            >
            {#if task.description}
              <span class="chat-activity__task-name">{task.agentId}</span>
            {/if}
            {#if task.timeLabel}
              <span class="chat-activity__task-time">· {task.timeLabel}</span>
            {/if}
          </span>
        </span>
      </Button>
      {@render cancelButton(task)}
    </div>
  {/if}
{/snippet}

<!-- A Reflection's title already names its kind; its second line holds the
     outcome, which opens the changes when there are any. -->
{#snippet reflectionRow(row)}
  {@const statusTask = {
    kind: 'reflection',
    row,
    dotStatus: reflectionDotStatus(row.status),
  }}
  <div class="chat-activity__task-row chat-activity__task-row--reflection">
    <span class="chat-activity__task-copy">
      <Button
        variant="tertiary"
        class="chat-activity__task-link"
        ariaLabel={reflectionRowLabel(row)}
        disabled={!row.sessionId}
        onClick={() => row.sessionId && onOpenReflection(row)}
      >
        <span class="chat-activity__task-title">
          {reflectionScopeLabel(row)}
        </span>
      </Button>
      <span class="chat-activity__task-meta">
        <span
          class="chat-activity__task-state"
          use:tooltip={() => statusDetails(statusTask)}
        >
          {@render statusIcon(statusTask)}
          {#if row.status === 'running' && reflectionElapsedLabel(row.startedAt, nowMs)}
            <span class="chat-activity__task-time">
              {reflectionElapsedLabel(row.startedAt, nowMs)}
            </span>
          {:else if row.status !== 'running' && row.outcome && !reflectionHasChanges(row.outcome)}
            <span class="chat-activity__outcome"
              >{reflectionOutcomeLabel(row.outcome)}</span
            >
          {/if}
        </span>
        {#if row.status !== 'running' && reflectionHasChanges(row.outcome)}
          {@render outcomeToggle(row)}
        {/if}
      </span>
    </span>
  </div>
  {#if row.status !== 'running' && reflectionHasChanges(row.outcome) && expandedReviews.has(row.runId)}
    {@render reviewChanges(row)}
  {/if}
{/snippet}

{#snippet outcomeToggle(row)}
  {@const expanded = expandedReviews.has(row.runId)}
  <Button
    id={outcomeToggleId(row)}
    variant="tertiary"
    class="chat-activity__outcome chat-activity__outcome-toggle"
    aria-expanded={expanded}
    aria-controls={expanded ? changesId(row) : undefined}
    onClick={() => toggleReviewChanges(row)}
  >
    {reflectionOutcomeLabel(row.outcome)}
    <svg
      class="chat-activity__disclosure"
      viewBox="0 0 16 16"
      width="10"
      height="10"
      aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg
    >
  </Button>
{/snippet}

{#snippet reviewChanges(row)}
  <div id={changesId(row)} class="chat-activity__changes">
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
    onClick={() => (open ? closePanel() : openPanel())}
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
        <Button
          id={`${panelId}-close`}
          variant="tertiary"
          icon
          class="chat-activity__close"
          ariaLabel={t('chat.activity.close')}
          tooltip={{ text: t('chat.activity.close'), placement: 'left' }}
          aria-expanded="true"
          aria-controls={panelId}
          onClick={closePanel}
        >
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
            <path d="m6 4 4 4-4 4" />
          </svg>
        </Button>
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
            {#each [['running', sections.running], ['finished', sections.finished]] as [section, entries] (section)}
              {#if entries.length > 0}
                <section
                  class={`chat-activity__group chat-activity__group--${section}`}
                  aria-labelledby={`${panelId}-${section}`}
                >
                  <h3
                    id={`${panelId}-${section}`}
                    class="chat-activity__group-title"
                  >
                    {section === 'running'
                      ? t('chat.activity.running')
                      : t('chat.activity.finished')}
                    <span class="chat-activity__count">{entries.length}</span>
                  </h3>
                  <ul class="chat-activity__task-list">
                    {#each entries as entry (entry.key)}
                      <li>
                        {#if entry.kind === 'reflection'}
                          {@render reflectionRow(entry.row)}
                        {:else}
                          {@render taskRow(entry.task)}
                        {/if}
                      </li>
                    {/each}
                  </ul>
                </section>
              {/if}
            {/each}
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

  /* The panel grows with the Chat area (about 70% of its height) but never
     below a usable height nor past the area itself. */
  .chat-activity--open {
    display: flex;
    align-items: center;
    width: min(264px, calc(100% - 16px));
    height: min(calc(100% - 24px), max(70%, 360px));
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

  /* The open panel carries its own close button in its header. */
  .chat-activity--open :global(.chat-activity__rail.btn-tertiary) {
    display: none;
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
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 10px 8px 6px 16px;
  }
  :global(.chat-activity__close.btn-tertiary.btn-icon) {
    width: 24px;
    min-width: 24px;
    height: 24px;
    min-height: 24px;
    border: 0;
    color: var(--text-lo);
  }
  :global(.chat-activity__close.btn-tertiary.btn-icon:hover) {
    background: var(--surface-2);
    color: var(--text-hi);
  }
  :global(.chat-activity__close svg) {
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.5;
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
    margin-top: 10px;
  }
  .chat-activity__group-title {
    display: flex;
    align-items: center;
    gap: 7px;
    margin: 0;
    padding: 4px 8px;
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
  .chat-activity__task-list {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .chat-activity__task-row {
    display: flex;
    width: 100%;
    align-items: center;
    gap: 4px;
    padding: 3px 8px;
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
    min-height: 0;
    flex: 1;
    justify-content: flex-start;
    padding: 0;
    border: 0;
    border-radius: var(--r-sm);
    color: var(--text-hi);
    font-weight: 400;
    text-align: left;
  }
  :global(.chat-activity__task-link.btn-tertiary:hover) {
    background: transparent;
  }
  .chat-activity__task-row--reflection :global(.chat-activity__task-link) {
    flex: 0 1 auto;
    align-self: flex-start;
    max-width: 100%;
  }
  .chat-activity__task-copy {
    display: flex;
    min-width: 0;
    flex: 1;
    flex-direction: column;
    gap: 1px;
  }
  .chat-activity__task-title {
    min-width: 0;
    overflow: hidden;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
    font-weight: 400;
    line-height: 1.3;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .chat-activity__task-command {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    font-weight: 400;
  }
  /* The quiet second line: state symbol, kind, name and time, small enough
     that the symbol never makes the line taller. */
  .chat-activity__task-meta {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 4px;
    color: var(--text-lo);
    font-family: var(--font-ui);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
    font-weight: 400;
    line-height: 1.3;
    white-space: nowrap;
  }
  .chat-activity__task-state {
    display: inline-flex;
    min-width: 0;
    align-items: center;
    gap: 4px;
  }
  .chat-activity__task-kind {
    flex: 0 0 auto;
    color: var(--text-med);
  }
  .chat-activity__task-name,
  .chat-activity__task-time,
  .chat-activity__outcome {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .chat-activity__task-time {
    flex: 0 0 auto;
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
    width: 12px;
    height: 12px;
    flex: 0 0 12px;
    align-items: center;
    justify-content: center;
    color: var(--text-med);
  }
  .chat-activity__status svg {
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.8;
  }
  .chat-activity__working-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--amber);
  }
  .chat-activity__status--success {
    color: var(--green);
  }
  .chat-activity__status--failed {
    color: var(--red);
  }
  :global(.chat-activity__outcome-toggle.btn-tertiary) {
    min-width: 0;
    min-height: 0;
    gap: 3px;
    margin: 0 0 0 -3px;
    padding: 0 3px;
    border: 0;
    border-radius: var(--r-sm);
    color: var(--text-med);
    font-size: inherit;
    font-weight: 400;
    line-height: inherit;
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
    padding: 2px 8px 10px 24px;
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
  :global(.chat-activity__close.btn-tertiary:focus-visible),
  :global(.chat-activity__outcome-toggle.btn-tertiary:focus-visible),
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
  }
</style>
