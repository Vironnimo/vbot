<script>
  import { onDestroy, onMount } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  import {
    backgroundTasks,
    sessionChangeStats,
    changeStatsLabel,
    changeStatsParts,
    changeStatsTooltip,
  } from '$lib/chatTimelinePresentation.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';

  import Button from '../ui/Button.svelte';

  let {
    timelineItems = [],
    subAgentStatuses = {},
    backgroundBashStatuses = {},
    onNavigateToSubAgent = () => {},
    onCancelSubAgent = () => {},
    onCancelBackgroundProcess = () => {},
  } = $props();

  let open = $state(false);
  const cancellingTaskIds = new SvelteSet();
  let tasks = $derived(
    backgroundTasks(timelineItems, subAgentStatuses, backgroundBashStatuses),
  );
  let activeTasks = $derived(
    tasks.filter((task) => task.dotStatus === 'running'),
  );
  let finishedTasks = $derived(
    tasks.filter((task) => task.dotStatus !== 'running'),
  );
  let runningTaskCount = $derived(activeTasks.length);
  let sessionStats = $derived(sessionChangeStats(timelineItems));
  let sessionStatsParts = $derived(changeStatsParts(sessionStats));
  let sessionStatsTooltip = $derived(changeStatsTooltip(sessionStats));
  let sessionStatsLabel = $derived(changeStatsLabel(sessionStats));

  const panelId = 'chat-activity-panel';

  const togglePanel = () => {
    open = !open;
  };

  const closePanel = () => {
    open = false;
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

  const statusLabel = (status) => {
    if (status === 'running') {
      return t('chat.activity.status.running', 'Working');
    }
    if (status === 'success') {
      return t('chat.activity.status.completed', 'Completed');
    }
    if (status === 'failed') {
      return t('chat.activity.status.failed', 'Failed');
    }
    if (status === 'cancelled') {
      return t('chat.activity.status.cancelled', 'Cancelled');
    }
    return t('chat.activity.status.unknown', 'Unknown');
  };

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
          processSessionId: task.processSessionId,
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
    aria-hidden="true"
  >
    {#if task.dotStatus === 'running'}
      <svg viewBox="0 0 16 16" width="14" height="14">
        <circle cx="8" cy="8" r="5" />
        <path d="M8 3a5 5 0 0 1 5 5" />
      </svg>
    {:else if task.dotStatus === 'success'}
      <svg viewBox="0 0 16 16" width="14" height="14">
        <path d="m3.5 8.2 2.8 2.8 6.2-6.2" />
      </svg>
    {:else if task.dotStatus === 'cancelled'}
      <svg viewBox="0 0 16 16" width="14" height="14">
        <path d="m4.5 4.5 7 7m0-7-7 7" />
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
      <span class="chat-activity__bash-mark" aria-hidden="true">$</span>
      <span class="chat-activity__task-name">{task.command}</span>
      {@render statusIcon(task)}
      {@render cancelButton(task)}
    </div>
  {:else}
    <div class="chat-activity__task-row">
      <Button
        variant="tertiary"
        class="chat-activity__task-link"
        ariaLabel={taskLabel(task)}
        disabled={!task.target}
        onClick={() => task.target && onNavigateToSubAgent(task.target)}
      >
        <span class="chat-activity__task-name">{task.agentId}</span>
      </Button>
      {@render statusIcon(task)}
      {@render cancelButton(task)}
    </div>
  {/if}
{/snippet}

<div class:chat-activity--open={open} class="chat-activity">
  <Button
    variant="tertiary"
    class="chat-activity__rail"
    ariaLabel={railLabel}
    tooltip={railLabel}
    aria-expanded={open}
    aria-controls={panelId}
    onClick={togglePanel}
  >
    <span class="chat-activity__rail-mark">
      <svg
        class="chat-activity__rail-arrow"
        viewBox="0 0 16 16"
        width="12"
        height="12"
        aria-hidden="true"
      >
        <path d="M10 3 5 8l5 5" />
      </svg>
      {#if runningTaskCount > 0}
        <span class="chat-activity__rail-dot" aria-hidden="true"></span>
      {/if}
    </span>
  </Button>

  {#if open}
    <aside
      id={panelId}
      class="chat-activity__panel"
      aria-labelledby="chat-activity-title"
    >
      <header class="chat-activity__header">
        <h2 id="chat-activity-title" class="chat-activity__title">
          {t('chat.activity.title', 'Session')}
        </h2>
      </header>

      <section
        class="chat-activity__stats"
        aria-labelledby="chat-activity-stats-title"
      >
        <h3 id="chat-activity-stats-title" class="chat-activity__group-title">
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
        {#if tasks.length === 0}
          <p class="chat-activity__empty">
            {t('chat.activity.empty', 'No background tasks')}
          </p>
        {:else}
          {#if activeTasks.length > 0}
            <section
              class="chat-activity__group chat-activity__group--active"
              aria-labelledby="chat-activity-active-title"
            >
              <h3
                id="chat-activity-active-title"
                class="chat-activity__group-title"
              >
                {t('chat.activity.active', 'Active')}
              </h3>
              <ul class="chat-activity__task-list">
                {#each activeTasks as task (task.id)}
                  <li>{@render taskRow(task)}</li>
                {/each}
              </ul>
            </section>
          {/if}

          {#if finishedTasks.length > 0}
            <section
              class="chat-activity__group chat-activity__group--finished"
              aria-labelledby="chat-activity-finished-title"
            >
              <h3
                id="chat-activity-finished-title"
                class="chat-activity__group-title"
              >
                {t('chat.activity.finished', 'Finished')}
              </h3>
              <ul class="chat-activity__task-list">
                {#each finishedTasks as task (task.id)}
                  <li>{@render taskRow(task)}</li>
                {/each}
              </ul>
            </section>
          {/if}
        {/if}
      </div>
    </aside>
  {/if}
</div>

<style>
  .chat-activity {
    --chat-activity-rail-width: 26px;

    position: absolute;
    z-index: 30;
    top: 50%;
    right: 8px;
    display: flex;
    width: var(--chat-activity-rail-width);
    height: clamp(132px, 30%, 220px);
    min-height: 0;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: color-mix(in srgb, var(--secondary-surface) 94%, transparent);
    box-shadow: var(--floating-elevation);
    transform: translateY(-50%);
    transition:
      width 180ms ease,
      height 180ms ease,
      border-color 120ms ease;
  }

  .chat-activity--open {
    width: min(276px, calc(100% - 24px));
    height: clamp(210px, 48%, 360px);
    border-color: var(--border-2);
  }

  :global(.chat-activity__rail) {
    width: var(--chat-activity-rail-width);
    min-width: var(--chat-activity-rail-width);
    height: 100%;
    padding: 0;
    border: 0;
    border-radius: 0;
    background: transparent;
  }

  :global(.chat-activity__rail:hover),
  :global(.chat-activity__rail:focus-visible) {
    color: var(--text-med);
    background: var(--surface-2);
  }

  .chat-activity__rail-mark {
    display: flex;
    align-items: center;
    gap: 6px;
    writing-mode: vertical-rl;
  }

  .chat-activity__rail-arrow {
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.5;
    transition: transform 150ms ease;
  }

  .chat-activity--open .chat-activity__rail-arrow {
    transform: rotate(180deg);
  }

  .chat-activity__rail-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--amber);
  }

  .chat-activity__panel {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    border-left: 1px solid var(--border);
    background: var(--secondary-surface);
    animation: chat-activity-enter 150ms ease-out;
  }

  .chat-activity__header {
    display: flex;
    min-height: 42px;
    flex-shrink: 0;
    align-items: center;
    padding: 0 12px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .chat-activity__title,
  .chat-activity__empty {
    margin: 0;
  }

  .chat-activity__title {
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
    font-weight: 600;
  }

  .chat-activity__tasks {
    min-height: 0;
    flex: 1;
    overflow-y: auto;
    padding: 5px;
  }

  .chat-activity__stats {
    flex: 0 0 auto;
    padding: 5px 5px 0;
  }

  .chat-activity__stats .chat-activity__group-title {
    padding-bottom: 2px;
  }

  .chat-activity__stats-value,
  .chat-activity__stats-empty {
    margin: 0;
    padding: 5px 8px;
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
  }

  .chat-activity__stats-value {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    border-radius: var(--r-sm);
    color: var(--text-med);
    cursor: default;
  }

  .chat-activity__stats-value:focus-visible {
    outline: none;
    box-shadow: 0 0 0 2px var(--accent-16);
  }

  .chat-activity__stats-part--added {
    color: var(--green);
  }

  .chat-activity__stats-part--removed {
    color: var(--red);
  }

  .chat-activity__stats-empty {
    color: var(--text-lo);
  }

  .chat-activity__empty {
    padding: 9px 8px;
    color: var(--text-lo);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
  }

  .chat-activity__task-list {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .chat-activity__group + .chat-activity__group {
    margin-top: 5px;
    padding-top: 5px;
    border-top: 1px solid var(--border);
  }

  .chat-activity__group-title {
    margin: 0;
    padding: 5px 8px 4px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    font-weight: 500;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .chat-activity__task-list li + li {
    border-top: 1px solid var(--border);
  }

  .chat-activity__task-row {
    display: flex;
    width: 100%;
    min-height: 34px;
    align-items: center;
    gap: 6px;
    padding: 5px 8px;
    color: var(--text-med);
    text-align: left;
  }

  :global(.chat-activity__task-link.btn-tertiary) {
    min-width: 0;
    min-height: 24px;
    flex: 1;
    justify-content: flex-start;
    padding: 0;
    border: 0;
    border-radius: var(--r-sm);
    color: var(--text-med);
    text-align: left;
  }

  :global(.chat-activity__task-link.btn-tertiary:hover) {
    color: var(--text-hi);
    background: transparent;
  }

  :global(.chat-activity__task-link.btn-tertiary:focus-visible) {
    box-shadow: inset 0 0 0 1px var(--accent);
  }

  .chat-activity__task-row--bash {
    cursor: default;
  }

  .chat-activity__bash-mark {
    width: 10px;
    flex: 0 0 10px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
  }

  .chat-activity__task-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  :global(.chat-activity__cancel.btn-danger.btn-icon) {
    width: 24px;
    min-width: 24px;
    height: 24px;
    min-height: 24px;
    flex: 0 0 24px;
    border-color: transparent;
    background: transparent;
  }

  :global(.chat-activity__cancel.btn-danger.btn-icon:hover),
  :global(.chat-activity__cancel.btn-danger.btn-icon:focus-visible) {
    border-color: rgba(252, 129, 129, 0.35);
    background: rgba(252, 129, 129, 0.1);
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
    color: var(--text-lo);
  }

  .chat-activity__status svg {
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.6;
  }

  .chat-activity__status--running {
    color: var(--amber);
  }

  .chat-activity__status--running circle {
    opacity: 0.22;
  }

  .chat-activity__status--running path {
    animation: chat-activity-spin 950ms linear infinite;
    transform-origin: center;
  }

  .chat-activity__status--success {
    color: var(--green);
  }

  .chat-activity__status--failed {
    color: var(--red);
  }

  .chat-activity__status--cancelled {
    color: var(--text-lo);
  }

  @keyframes chat-activity-enter {
    from {
      opacity: 0;
      transform: translateX(8px);
    }
    to {
      opacity: 1;
      transform: translateX(0);
    }
  }

  @keyframes chat-activity-spin {
    to {
      transform: rotate(360deg);
    }
  }

  @media (max-width: 640px) {
    .chat-activity {
      --chat-activity-rail-width: 32px;
      right: 6px;
      height: clamp(120px, 30%, 190px);
    }

    .chat-activity--open {
      width: min(268px, calc(100% - 16px));
      height: clamp(200px, 50%, 320px);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .chat-activity,
    .chat-activity__panel {
      transition: none;
      animation: none;
    }

    .chat-activity__rail-arrow,
    .chat-activity__status--running path {
      transition: none;
      animation: none;
    }
  }
</style>
