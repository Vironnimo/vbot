<script>
  import { onDestroy, onMount } from 'svelte';

  import {
    backgroundSubAgentTasks,
    isRowCancellable,
  } from '$lib/chatTimelinePresentation.js';
  import { t } from '$lib/i18n.js';

  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import StatusChip from '../ui/StatusChip.svelte';

  let {
    timelineItems = [],
    subAgentStatuses = {},
    onNavigateToSubAgent = () => {},
    onCancelSubAgent = () => {},
  } = $props();

  let open = $state(false);
  let tasks = $derived(
    backgroundSubAgentTasks(timelineItems, subAgentStatuses),
  );
  let runningTaskCount = $derived(
    tasks.filter((task) => task.dotStatus === 'running').length,
  );

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
      return t('chat.activity.status.running', 'Running');
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

  const statusVariant = (status) => {
    if (status === 'running') {
      return 'warn';
    }
    if (status === 'success') {
      return 'success';
    }
    if (status === 'failed') {
      return 'error';
    }
    return 'neutral';
  };

  let railLabel = $derived(
    open
      ? t('chat.activity.close', 'Close activity panel')
      : runningTaskCount === 1
        ? t(
            'chat.activity.openOneRunning',
            'Open activity panel · 1 task running',
          )
        : runningTaskCount > 1
          ? t(
              'chat.activity.openManyRunning',
              'Open activity panel · {count} tasks running',
              { count: runningTaskCount },
            )
          : t('chat.activity.open', 'Open activity panel'),
  );
</script>

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
        <div>
          <p class="chat-activity__eyebrow">
            {t('chat.activity.eyebrow', 'Session context')}
          </p>
          <h2 id="chat-activity-title" class="chat-activity__title">
            {t('chat.activity.title', 'Activity')}
          </h2>
        </div>
        <Button
          variant="tertiary"
          icon
          class="chat-activity__close"
          ariaLabel={t('chat.activity.close', 'Close activity panel')}
          tooltip={t('chat.activity.close', 'Close activity panel')}
          onClick={closePanel}
        >
          <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
            <path d="m6 3 5 5-5 5" />
          </svg>
        </Button>
      </header>

      <div class="chat-activity__section-heading">
        <span>{t('chat.activity.backgroundTasks', 'Background tasks')}</span>
        <span class="chat-activity__count">{tasks.length}</span>
      </div>

      <div class="chat-activity__tasks">
        {#if tasks.length === 0}
          <EmptyState
            density="compact"
            class="chat-activity__empty"
            title={t('chat.activity.emptyTitle', 'No background tasks')}
            description={t(
              'chat.activity.emptyDescription',
              'Background work started from this Session will stay visible here.',
            )}
          />
        {:else}
          {#each tasks as task (task.id)}
            <article class="chat-activity__task">
              <div class="chat-activity__task-topline">
                <span class="chat-activity__agent">{task.agentId}</span>
                <StatusChip variant={statusVariant(task.dotStatus)}>
                  {statusLabel(task.dotStatus)}
                </StatusChip>
              </div>
              <p class="chat-activity__task-preview">
                {task.preview ||
                  t('chat.activity.noDescription', 'No task description')}
              </p>
              {#if task.lastToolName || task.timeLabel}
                <p class="chat-activity__task-meta">
                  {#if task.lastToolName}
                    {t('chat.activity.currentTool', 'Current: {tool}', {
                      tool: task.lastToolName,
                    })}
                  {/if}
                  {#if task.lastToolName && task.timeLabel}
                    <span aria-hidden="true"> · </span>
                  {/if}
                  {task.timeLabel}
                </p>
              {/if}
              <div class="chat-activity__task-actions">
                <Button
                  variant="secondary"
                  class="chat-activity__open-task"
                  disabled={!task.target}
                  onClick={() =>
                    task.target && onNavigateToSubAgent(task.target)}
                >
                  {t('chat.activity.viewSession', 'View session')}
                </Button>
                {#if isRowCancellable( { kind: 'sub_agent', dotStatus: task.dotStatus }, )}
                  <Button
                    variant="danger"
                    class="chat-activity__cancel-task"
                    onClick={() => onCancelSubAgent({ tool: task.tool })}
                  >
                    {t('chat.cancelSubAgent', 'Cancel')}
                  </Button>
                {/if}
              </div>
            </article>
          {/each}
        {/if}
      </div>
    </aside>
  {/if}
</div>

<style>
  .chat-activity {
    --chat-activity-panel-width: 300px;

    display: flex;
    height: 100%;
    min-height: 0;
    flex-shrink: 0;
    background: var(--secondary-surface);
  }

  :global(.chat-activity__rail) {
    width: 26px;
    min-width: 26px;
    height: 100%;
    padding: 0;
    border-width: 0 0 0 1px;
    border-color: var(--border);
    border-radius: 0;
    background: var(--secondary-surface);
  }

  :global(.chat-activity__rail:hover),
  :global(.chat-activity__rail:focus-visible) {
    border-color: var(--border-2);
    background: var(--surface);
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
    width: var(--chat-activity-panel-width);
    min-width: 0;
    min-height: 0;
    flex-direction: column;
    border-left: 1px solid var(--border);
    background: var(--secondary-surface);
    animation: chat-activity-enter 150ms ease-out;
  }

  .chat-activity__header {
    display: flex;
    min-height: 60px;
    flex-shrink: 0;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 14px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .chat-activity__eyebrow,
  .chat-activity__title,
  .chat-activity__task-preview,
  .chat-activity__task-meta {
    margin: 0;
  }

  .chat-activity__eyebrow,
  .chat-activity__section-heading {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    font-weight: 500;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  .chat-activity__title {
    margin-top: 4px;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: var(--fs-heading-sm);
    font-weight: 600;
  }

  :global(.chat-activity__close) {
    flex-shrink: 0;
  }

  :global(.chat-activity__close svg) {
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.5;
  }

  .chat-activity__section-heading {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 14px 14px 10px;
  }

  .chat-activity__count {
    color: var(--text-med);
    letter-spacing: 0;
  }

  .chat-activity__tasks {
    display: flex;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    gap: 8px;
    overflow-y: auto;
    padding: 0 12px 14px;
  }

  :global(.chat-activity__empty) {
    margin-top: 4px;
  }

  .chat-activity__task {
    padding: 12px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface);
  }

  .chat-activity__task-topline {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .chat-activity__agent {
    min-width: 0;
    overflow: hidden;
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chat-activity__task-preview {
    display: -webkit-box;
    margin-top: 10px;
    overflow: hidden;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
    line-height: 1.4;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 3;
  }

  .chat-activity__task-meta {
    margin-top: 8px;
    overflow: hidden;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chat-activity__task-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 12px;
  }

  :global(.chat-activity__open-task) {
    flex: 1;
  }

  :global(.chat-activity__cancel-task) {
    flex-shrink: 0;
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

  @media (max-width: 960px) {
    .chat-activity {
      position: absolute;
      z-index: 30;
      top: 0;
      right: 0;
      bottom: 0;
      height: auto;
      box-shadow: var(--floating-elevation);
    }
  }

  @media (max-width: 640px) {
    .chat-activity {
      --chat-activity-panel-width: min(320px, calc(100vw - 40px));
    }

    :global(.chat-activity__rail) {
      width: 40px;
      min-width: 40px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .chat-activity__panel {
      animation: none;
    }

    .chat-activity__rail-arrow {
      transition: none;
    }
  }
</style>
