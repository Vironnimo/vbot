<script>
  import { onDestroy, onMount } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  import {
    backgroundTasks,
    sessionChangeStats,
    changeStatsLabel,
    changeStatsParts,
    changeStatsTooltip,
    reflectionElapsedLabel,
  } from '$lib/chatTimelinePresentation.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';

  import Button from '../ui/Button.svelte';

  let {
    timelineItems = [],
    subAgentStatuses = {},
    backgroundBashStatuses = {},
    backgroundBashProcesses = {},
    reflectionTasks = [],
    parentSession = null,
    onNavigateToSubAgent = () => {},
    onNavigateToParentSession = () => {},
    onOpenReflection = () => {},
    onCancelSubAgent = () => {},
    onCancelBackgroundProcess = () => {},
  } = $props();

  let open = $state(false);
  let bashExpanded = $state(null);
  let defaultBashOpen = $derived(
    subagentTasks.length === 0 && reflectionTasks.length === 0,
  );
  const cancellingTaskIds = new SvelteSet();
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
  let sessionStats = $derived(sessionChangeStats(timelineItems));
  let sessionStatsParts = $derived(changeStatsParts(sessionStats));
  let sessionStatsTooltip = $derived(changeStatsTooltip(sessionStats));
  let sessionStatsLabel = $derived(changeStatsLabel(sessionStats));

  const panelId = $props.id();
  const runningLabel = (count) =>
    t('chat.activity.runningCount', '{count} running', { count });

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
      return t('chat.activity.status.running', 'Working');
    }
    if (status === 'success' || status === 'completed') {
      return t('chat.activity.status.completed', 'Completed');
    }
    if (status === 'failed') {
      return t('chat.activity.status.failed', 'Failed');
    }
    if (status === 'cancelled') {
      return t('chat.activity.status.cancelled', 'Cancelled');
    }
    if (status === 'interrupted') {
      return t('chat.activity.status.interrupted', 'Interrupted');
    }
    return t('chat.activity.status.unknown', 'Unknown');
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
      return t('chat.activity.reflectionScope.memory', 'Memory review');
    }
    if (row.scope === 'skill') {
      return t('chat.activity.reflectionScope.skill', 'Skill review');
    }
    return t('chat.activity.reflectionScope.combined', 'Memory & skill review');
  };

  const reflectionRowLabel = (row) =>
    t('chat.activity.reflectionOpenAria', 'Open {scope} Session · {status}', {
      scope: reflectionScopeLabel(row),
      status: statusLabel(row.status),
    });

  const parentSessionLabel = () =>
    t('chat.activity.openParentSession', 'Open parent Session · {session}', {
      session: parentSession?.displayName ?? '',
    });

  const taskLabel = (task) => {
    if (task.kind === 'bash') {
      return t('chat.activity.bashTaskAria', 'Bash · {command} · {status}', {
        command: task.command,
        status: statusLabel(task.dotStatus),
      });
    }
    return t('chat.activity.taskAria', 'Open {agent} Session · {status}', {
      agent: task.agentId,
      status: statusLabel(task.dotStatus),
    });
  };

  const cancelTaskLabel = (task) => {
    if (task.kind === 'bash') {
      return t(
        'chat.activity.cancelBashAria',
        'Cancel Bash background process · {command}',
        { command: task.command },
      );
    }
    return t(
      'chat.activity.cancelSubAgentAria',
      'Cancel {agent} background task',
      { agent: task.agentId },
    );
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
      ? t('chat.activity.close', 'Close session info')
      : runningTaskCount === 1
        ? t(
            'chat.activity.openOneRunning',
            'Open session info · 1 task running',
          )
        : runningTaskCount > 1
          ? t(
              'chat.activity.openManyRunning',
              'Open session info · {count} tasks running',
              { count: runningTaskCount },
            )
          : t('chat.activity.open', 'Open session info'),
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
    use:tooltip={statusLabel(task.dotStatus)}
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
      tooltip={cancelTaskLabel(task)}
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
      <span class="chat-activity__task-name" use:tooltip={task.command}>
        {task.command}
        {#if task.timeLabel}
          <span class="chat-activity__task-time">· {task.timeLabel}</span>
        {/if}
      </span>
      {@render statusIcon(task)}
      {@render cancelButton(task)}
    </div>
  {:else}
    <div class="chat-activity__task-row">
      <Button
        variant="tertiary"
        class="chat-activity__task-link"
        ariaLabel={taskLabel(task)}
        aria-describedby={task.preview
          ? `${panelId}-${task.id}-preview`
          : undefined}
        tooltip={task.preview}
        disabled={!task.target}
        onClick={() => task.target && onNavigateToSubAgent(task.target)}
      >
        <span class="chat-activity__task-copy">
          <span class="chat-activity__task-name">{task.agentId}</span>
          {#if task.preview}
            <span
              id={`${panelId}-${task.id}-preview`}
              class="chat-activity__task-preview">{task.preview}</span
            >
          {/if}
        </span>
      </Button>
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
    {@render statusIcon({ dotStatus: reflectionDotStatus(row.status) })}
  </div>
{/snippet}

<div class:chat-activity--open={open} class="chat-activity">
  <Button
    id={`${panelId}-toggle`}
    variant="tertiary"
    class="chat-activity__rail"
    ariaLabel={railLabel}
    tooltip={railLabel}
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
          {t('chat.activity.title', 'Session')}
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
              {t('chat.activity.parentSession', 'Parent Session')}
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
              <span class="chat-activity__parent-name">
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
            {t('chat.activity.statsTitle', 'Session stats')}
          </h3>
          {#if sessionStats}
            <!-- The change block is focusable so keyboard users reach the
               file-list tooltip; the aria-label already carries the full
               summary for screen readers. -->
            <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
            <p
              class="chat-activity__stats-value"
              aria-label={sessionStatsLabel}
              use:tooltip={sessionStatsTooltip}
              tabindex="0"
            >
              {#each sessionStatsParts as changePart (changePart.kind)}
                <span
                  class="chat-activity__stats-part"
                  class:chat-activity__stats-part--added={changePart.kind ===
                    'added'}
                  class:chat-activity__stats-part--removed={changePart.kind ===
                    'removed'}>{changePart.text}</span
                >
              {/each}
            </p>
          {:else}
            <p class="chat-activity__stats-empty">
              {t('chat.activity.statsEmpty', 'No changes yet')}
            </p>
          {/if}
        </section>

        <div class="chat-activity__tasks">
          {#if tasks.length === 0 && reflectionTasks.length === 0}
            <p class="chat-activity__empty">
              {t('chat.activity.empty', 'No background tasks')}
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
                  {t('chat.activity.subagents', 'Subagent Runs')}
                  <span class="chat-activity__count"
                    >{subagentTasks.length}</span
                  >
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
                  {t('chat.activity.reflections', 'Reflections')}
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
                  {t('chat.activity.bash', 'Bash')}
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
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    color: var(--text-med);
  }
  .chat-activity__stats-value {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    border-radius: var(--r-sm);
    cursor: default;
  }
  .chat-activity__stats-part--added {
    color: var(--green);
  }
  .chat-activity__stats-part--removed {
    color: var(--red);
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
    font-size: var(--fs-mono-xs);
    font-family: var(--font-mono);
    font-weight: 400;
  }
  .chat-activity__running-count {
    margin-left: auto;
    color: var(--amber);
    font-size: var(--fs-mono-xs);
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
    font-weight: 400;
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
  :global(.chat-activity__rail.btn-tertiary:focus-visible),
  summary:focus-visible,
  .chat-activity__stats-value:focus-visible,
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
