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
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import { formatCost, formatTokens } from '$lib/statisticsView.js';
  import {
    formatAbsoluteTime,
    formatMoment,
    formatRelativeTime,
  } from '$lib/timeText.js';
  import { tooltip } from '$lib/tooltip.js';

  import Button from '../ui/Button.svelte';
  import ContextMenu from '../ui/ContextMenu.svelte';
  import { contextMenuAnchor, isContextMenuKey } from '../ui/contextMenu.js';
  import ChangeStats from './ChangeStats.svelte';
  import CommandOutput from './CommandOutput.svelte';
  import CopyableValueCard from './CopyableValueCard.svelte';

  let {
    timelineItems = [],
    subAgentStatuses = {},
    // What each Sub-Agent still runs besides its Run, by Sub-Agent id, as
    // last read; a row asks for it while the user looks at it.
    subAgentWork = {},
    onSubAgentWorkWanted = () => {},
    // The background commands each Sub-Agent (or one below it) still runs
    // after its Run, by Sub-Agent id (`runningSubAgentCommands`).
    subAgentCommands = {},
    backgroundCommandStatuses = {},
    commandStatuses = {},
    reflectionTasks = [],
    // The change statistics of the displayed Session and its Sub-Agents:
    // undefined until read, null when they changed no files. The panel asks
    // for them, the usage and the Sub-Agent tree while it is open.
    sessionStats = undefined,
    onSessionStatsWanted = () => {},
    // The displayed Session's list row as last read (`{ displayName,
    // createdAt, lastActiveAt }`), or null until then.
    session = null,
    // The usage of the Session's own Runs and its Sub-Agents' Runs
    // (`{ session, subAgents, total, subAgentCount }`, report Totals), or null.
    usage = null,
    // The Session's Sub-Agents at every depth (`subagent.tree` entries: id,
    // title, agent_id, session_id, parent_id, status), breadth first.
    subAgentTree = [],
    parentSession = null,
    onNavigateToSubAgent = () => {},
    // Open in split view for a Sub-Agent row (`{ label, open(target) }`, with
    // the row's `{ agentId, sessionId }` target); null offers no row menu.
    otherArea = null,
    onNavigateToParentSession = () => {},
    onOpenReflection = () => {},
    onLoadReflectionChanges = () => {},
    onUndoReflection = async () => {},
    onCancelSubAgent = () => {},
    // Stops everything a Sub-Agent whose Run ended still runs.
    onStopSubAgentWork = () => {},
    onStopBackgroundCommand = () => {},
    // Reads a background command's newest output for its output view
    // (`(commandId) => Promise`, see CommandOutput.svelte); null offers no
    // output view.
    onLoadCommandOutput = null,
    // Whether the panel is open; Chat reserves room for it beside the
    // timeline while it is.
    open = $bindable(false),
  } = $props();

  const cancellingTaskIds = new SvelteSet();
  // Finished reviews whose change list is open, and those asking to confirm
  // their undo; both are view state only.
  const expandedReviews = new SvelteSet();
  const confirmingUndo = new SvelteSet();
  // Command rows whose output view is open, by task id: view state only. An
  // open view reads the output while it shows (CommandOutput.svelte).
  const expandedOutputs = new SvelteSet();
  let tasks = $derived(
    backgroundTasks(
      timelineItems,
      subAgentStatuses,
      backgroundCommandStatuses,
      commandStatuses,
      subAgentCommands,
      nowMs,
    ),
  );
  // Running work first, then finished work; each entry names its kind.
  let sections = $derived(activitySections(tasks, reflectionTasks));
  let runningTaskCount = $derived(sections.running.length);
  // A long Finished section shows its newest entries until the user asks for
  // all of them; view state only.
  const FINISHED_PREVIEW = 5;
  let showAllFinished = $state(false);
  let visibleFinished = $derived(
    showAllFinished
      ? sections.finished
      : sections.finished.slice(0, FINISHED_PREVIEW),
  );
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

  // The open Sub-Agent row menu (../ui/ContextMenu.svelte), or null.
  let taskMenu = $state(null);

  const openTaskMenu = (task, event) => {
    if (!otherArea || !task.target || event.defaultPrevented) return;
    event.preventDefault();
    taskMenu = {
      ...contextMenuAnchor(event),
      label: taskLabel(task),
      items: [
        {
          id: 'open-in-other-area',
          label: otherArea.label,
          onSelect: () => otherArea.open(task.target),
        },
      ],
    };
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
    if (status === 'ended') {
      return t('chat.activity.status.ended');
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
  // and for a command its command id.
  const statusDetails = (task) => {
    const details =
      task.kind === 'command'
        ? backgroundCommandStatusDetails(task.tool, task.rowState, Date.now())
        : task.kind === 'subagent'
          ? subAgentStatusDetails(
              task.tool,
              task.runDotStatus,
              subAgentStatuses,
              Date.now(),
              remainingWork(task),
            )
          : reflectionStatusDetails(task.row);
    // A Sub-Agent's title shows on one line; its card carries it in full.
    const text =
      task.kind === 'subagent' && task.description
        ? task.description
        : details.text;
    const title = task.workRunning
      ? t('chat.activity.status.commandsRunning')
      : details.title;
    return { ...details, title, text, placement: 'left' };
  };

  // What a Sub-Agent still runs besides its Run: the commands below it that
  // still run, and what its last inspection found.
  const remainingWork = (task) => {
    const commandIds = new Set(task.runningCommands.map((work) => work.id));
    return [
      ...task.runningCommands,
      ...(subAgentWork[task.subAgentId] ?? []).filter(
        (work) => work.kind !== 'command' || !commandIds.has(work.id),
      ),
    ];
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
        status: task.rowState?.stillRunning
          ? t('chat.activity.status.stillRunning')
          : statusLabel(task.dotStatus),
      });
    }
    return t('chat.activity.taskAria', {
      agent: task.agentId,
      status: task.workRunning
        ? t('chat.activity.status.commandsRunning')
        : statusLabel(task.dotStatus),
    });
  };

  const cancelTaskLabel = (task) => {
    if (task.kind === 'command') {
      return t('chat.activity.cancelCommandAria', { command: task.command });
    }
    if (task.workRunning) {
      return t('chat.activity.stopSubAgentWorkAria', { agent: task.agentId });
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
        await onStopBackgroundCommand({ commandId: task.commandId });
      } else if (task.workRunning) {
        await onStopSubAgentWork({ tool: task.tool });
      } else {
        await onCancelSubAgent({ tool: task.tool });
      }
    } finally {
      cancellingTaskIds.delete(task.id);
    }
  };

  const outputId = (task) => `${panelId}-${task.id}-output`;

  const toggleOutput = (task) => {
    if (expandedOutputs.has(task.id)) {
      expandedOutputs.delete(task.id);
    } else {
      expandedOutputs.add(task.id);
    }
  };

  // The Sub-Agents each Sub-Agent works with, by its id; a row with any
  // offers to show them below it. Expanded rows are view state only.
  let subAgentChildren = $derived(
    (subAgentTree ?? []).reduce((children, entry) => {
      if (entry?.parent_id && entry.id) {
        (children[entry.parent_id] ??= []).push(entry);
      }
      return children;
    }, Object.create(null)),
  );
  const expandedSubAgents = new SvelteSet();
  const childrenOf = (subAgentId) =>
    (subAgentId && subAgentChildren[subAgentId]) || [];
  const nestedListId = (subAgentId) => `${panelId}-nested-${subAgentId}`;
  const toggleNested = (subAgentId) => {
    if (expandedSubAgents.has(subAgentId)) {
      expandedSubAgents.delete(subAgentId);
    } else {
      expandedSubAgents.add(subAgentId);
    }
  };
  // A nested Sub-Agent's state as `subagent.tree` reports it.
  const treeDotStatus = (status) =>
    status === 'working' ? 'running' : status === 'queued' ? 'queued' : 'idle';
  const treeStatusLabel = (status) =>
    status === 'working'
      ? t('chat.activity.status.running')
      : status === 'queued'
        ? t('chat.activity.status.queued')
        : t('chat.activity.status.idle');

  // Token totals and cost over the Session's and its Sub-Agents' Runs, cache
  // reads as a share of the input; with Sub-Agents, a card splits them.
  let usageFacts = $derived.by(() => {
    const total = usage?.total;
    const input = nonNegative(total?.input_tokens);
    const output = nonNegative(total?.output_tokens);
    if (input <= 0 && output <= 0) {
      return null;
    }
    const locale = activeLocaleTag();
    const cacheRead = nonNegative(total?.cache_read_tokens);
    const tokens = (totals) =>
      t('chat.activity.overview.tokensValue', {
        input: formatTokens(nonNegative(totals?.input_tokens), locale),
        output: formatTokens(nonNegative(totals?.output_tokens), locale),
      });
    const cost = (totals) => formatCost(totals?.cost_usd, locale);
    const split = (value) =>
      usage.subAgentCount > 0
        ? {
            title: t('chat.activity.overview.splitTitle'),
            rows: [
              {
                label: t('chat.activity.overview.ownRuns'),
                value: value(usage.session),
              },
              {
                label: t('chat.activity.overview.subAgentRuns', {
                  count: usage.subAgentCount,
                }),
                value: value(usage.subAgents),
              },
            ],
            placement: 'left',
          }
        : null;
    return {
      tokens: tokens(total),
      tokensSplit: split(tokens),
      cache:
        input > 0 && cacheRead > 0
          ? new Intl.NumberFormat(locale, {
              style: 'percent',
              maximumFractionDigits: 0,
            }).format(Math.min(1, cacheRead / input))
          : '',
      cost: Number.isFinite(total?.cost_usd) ? cost(total) : '',
      costSplit: split(cost),
    };
  });

  function nonNegative(value) {
    return Number.isFinite(value) && value > 0 ? value : 0;
  }

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
        <span class="chat-activity__task-meta">
          <span
            class="chat-activity__task-state"
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
          {#if onLoadCommandOutput}
            <span class="chat-activity__task-separator" aria-hidden="true"
              >·</span
            >
            {@render outputToggle(task)}
          {/if}
        </span>
      </span>
      {@render cancelButton(task)}
    </div>
    {#if onLoadCommandOutput && expandedOutputs.has(task.id)}
      <CommandOutput
        id={outputId(task)}
        commandId={task.commandId}
        running={task.dotStatus === 'running'}
        load={onLoadCommandOutput}
        label={t('chat.activity.commandOutput.aria', {
          command: task.command,
        })}
      />
    {/if}
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
        oncontextmenu={(event) => openTaskMenu(task, event)}
        onkeydown={(event) => {
          if (isContextMenuKey(event)) openTaskMenu(task, event);
        }}
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
      {@render nestedToggle(task.subAgentId, task.description || task.agentId)}
      {@render cancelButton(task)}
    </div>
    {#if expandedSubAgents.has(task.subAgentId)}
      {@render nestedList(task.subAgentId)}
    {/if}
  {/if}
{/snippet}

<!-- How many Sub-Agents a Sub-Agent works with; shows them below its row. -->
{#snippet nestedToggle(subAgentId, name)}
  {@const count = childrenOf(subAgentId).length}
  {#if count > 0}
    {@const expanded = expandedSubAgents.has(subAgentId)}
    {@const label = t('chat.activity.nested.toggle', { count, name })}
    <Button
      variant="tertiary"
      class="chat-activity__nested-toggle"
      ariaLabel={label}
      tooltip={{ text: label, placement: 'left' }}
      aria-expanded={expanded}
      aria-controls={expanded ? nestedListId(subAgentId) : undefined}
      onClick={() => toggleNested(subAgentId)}
    >
      <span class="chat-activity__nested-count">{count}</span>
      <svg
        class="chat-activity__disclosure"
        viewBox="0 0 16 16"
        width="10"
        height="10"
        aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg
      >
    </Button>
  {/if}
{/snippet}

<!-- The Sub-Agents below one Sub-Agent, each opening its own Session and
     showing its own Sub-Agents in turn. -->
{#snippet nestedList(parentId)}
  <ul id={nestedListId(parentId)} class="chat-activity__nested">
    {#each childrenOf(parentId) as child (child.id)}
      {@const name = child.title || child.agent_id}
      <li>
        <div class="chat-activity__task-row chat-activity__task-row--nested">
          <Button
            variant="tertiary"
            class="chat-activity__task-link"
            ariaLabel={t('chat.activity.taskAria', {
              agent: child.agent_id,
              status: treeStatusLabel(child.status),
            })}
            onClick={() =>
              onNavigateToSubAgent({
                agentId: child.agent_id,
                sessionId: child.session_id,
              })}
          >
            <span class="chat-activity__task-copy">
              <span
                class="chat-activity__task-title"
                use:tooltip={{
                  text: name,
                  whenTruncated: true,
                  placement: 'left',
                }}>{name}</span
              >
              <span class="chat-activity__task-meta">
                {@render statusIcon({
                  dotStatus: treeDotStatus(child.status),
                })}
                <span class="chat-activity__task-kind"
                  >{treeStatusLabel(child.status)}</span
                >
                {#if child.title}
                  <span class="chat-activity__task-name">{child.agent_id}</span>
                {/if}
              </span>
            </span>
          </Button>
          {@render nestedToggle(child.id, name)}
        </div>
        {#if expandedSubAgents.has(child.id)}
          {@render nestedList(child.id)}
        {/if}
      </li>
    {/each}
  </ul>
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

<!-- Opens the command's newest output under its row. -->
{#snippet outputToggle(task)}
  {@const expanded = expandedOutputs.has(task.id)}
  <Button
    variant="tertiary"
    class="chat-activity__outcome chat-activity__outcome-toggle"
    ariaLabel={t('chat.activity.commandOutput.aria', { command: task.command })}
    aria-expanded={expanded}
    aria-controls={expanded ? outputId(task) : undefined}
    onClick={() => toggleOutput(task)}
  >
    {t('chat.activity.commandOutput.title')}
    <svg
      class="chat-activity__disclosure"
      viewBox="0 0 16 16"
      width="10"
      height="10"
      aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg
    >
  </Button>
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
      aria-label={t('chat.activity.label')}
    >
      <header class="chat-activity__header">
        <h2
          class="chat-activity__title"
          use:tooltip={{
            text: session?.displayName ?? '',
            whenTruncated: true,
            placement: 'left',
          }}
        >
          {session?.displayName ?? t('chat.activity.title')}
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
          class="chat-activity__overview"
          aria-labelledby={`${panelId}-overview-title`}
        >
          <h3
            id={`${panelId}-overview-title`}
            class="chat-activity__group-title"
          >
            {t('chat.activity.overview.title')}
            {#if usage?.subAgentCount > 0}
              <!-- Usage and changes below cover the Sub-Agents too. -->
              <span class="chat-activity__scope"
                >{usage.subAgentCount === 1
                  ? t('chat.activity.overview.withSubAgentsOne')
                  : t('chat.activity.overview.withSubAgentsMany', {
                      count: usage.subAgentCount,
                    })}</span
              >
            {/if}
          </h3>
          <dl class="chat-activity__facts">
            {#if usageFacts}
              <div class="chat-activity__fact">
                <dt>{t('chat.activity.overview.tokens')}</dt>
                <dd use:tooltip={usageFacts.tokensSplit}>
                  {usageFacts.tokens}
                </dd>
              </div>
              {#if usageFacts.cache}
                <div class="chat-activity__fact">
                  <dt>{t('chat.activity.overview.cache')}</dt>
                  <dd>{usageFacts.cache}</dd>
                </div>
              {/if}
              {#if usageFacts.cost}
                <div class="chat-activity__fact">
                  <dt>{t('chat.activity.overview.cost')}</dt>
                  <dd use:tooltip={usageFacts.costSplit}>{usageFacts.cost}</dd>
                </div>
              {/if}
            {/if}
            <!-- Until the first read answers, the panel claims neither
                 changes nor none. -->
            {#if sessionStats !== undefined}
              <div class="chat-activity__fact">
                <dt>{t('chat.activity.overview.changes')}</dt>
                <dd class="chat-activity__stats-value">
                  {#if sessionStats}
                    <!-- The panel sits at the right edge, so its card opens
                         to the left. -->
                    <ChangeStats stats={sessionStats} placement="left" />
                  {:else}
                    <span class="chat-activity__stats-empty"
                      >{t('chat.activity.overview.noChanges')}</span
                    >
                  {/if}
                </dd>
              </div>
            {/if}
            {#if session?.createdAt}
              <div class="chat-activity__fact">
                <dt>{t('chat.activity.overview.started')}</dt>
                <dd>{formatAbsoluteTime(session.createdAt)}</dd>
              </div>
            {/if}
            {#if session?.lastActiveAt}
              <div class="chat-activity__fact">
                <dt>{t('chat.activity.overview.lastActive')}</dt>
                <dd
                  use:tooltip={{
                    text: formatAbsoluteTime(session.lastActiveAt),
                    placement: 'left',
                  }}
                >
                  {formatRelativeTime(session.lastActiveAt)}
                </dd>
              </div>
            {/if}
          </dl>
        </section>

        <div class="chat-activity__tasks">
          {#if tasks.length === 0 && reflectionTasks.length === 0}
            <p class="chat-activity__empty">
              {t('chat.activity.empty')}
            </p>
          {:else}
            {#each [['running', sections.running, sections.running], ['finished', sections.finished, visibleFinished]] as [section, entries, shown] (section)}
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
                    {#each shown as entry (entry.key)}
                      <li>
                        {#if entry.kind === 'reflection'}
                          {@render reflectionRow(entry.row)}
                        {:else}
                          {@render taskRow(entry.task)}
                        {/if}
                      </li>
                    {/each}
                  </ul>
                  {#if section === 'finished' && entries.length > FINISHED_PREVIEW}
                    <Button
                      variant="tertiary"
                      class="chat-activity__more"
                      aria-expanded={showAllFinished}
                      onClick={() => (showAllFinished = !showAllFinished)}
                    >
                      {showAllFinished
                        ? t('chat.activity.showFewer')
                        : t('chat.activity.showAll', {
                            count: entries.length,
                          })}
                    </Button>
                  {/if}
                </section>
              {/if}
            {/each}
          {/if}
        </div>
      </div>
    </aside>
  {/if}
</div>

<ContextMenu menu={taskMenu} onClose={() => (taskMenu = null)} />

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

  /* Open, the panel is a full-height column at the right edge, the
     counterpart of the Session list on the left. It lies over the chat
     while the area is too narrow to give both their room... */
  .chat-activity--open {
    top: 0;
    right: 0;
    bottom: 0;
    display: flex;
    width: min(var(--chat-activity-width), calc(100% - 48px));
    height: auto;
    box-shadow: var(--floating-elevation);
    transform: none;
  }

  /* ...and docks beside it otherwise, so the chat centers in the rest and
     keeps its scrollbar. Beside a docked Session list it needs more room. */
  @container chat-area (min-width: 740px) {
    .chat-activity--open {
      position: relative;
      flex: 0 0 var(--chat-activity-width);
      box-shadow: none;
    }
  }
  @container chat-area (min-width: 740px) and (max-width: 1019px) {
    :global(.chat-view__content-shell:has(> .session-drawer))
      .chat-activity--open {
      position: absolute;
      flex: none;
      box-shadow: var(--floating-elevation);
    }
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
    height: 100%;
    min-height: 0;
    flex-direction: column;
    overflow: hidden;
    border-left: 1px solid var(--border);
    background: var(--surface);
  }
  /* Matches the Session list's header. */
  .chat-activity__header {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    justify-content: space-between;
    gap: 4px;
    padding: 3px;
    border-bottom: 1px solid var(--border);
  }
  :global(.chat-activity__close.btn-tertiary.btn-icon) {
    width: 28px;
    min-width: 28px;
    height: 28px;
    min-height: 28px;
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
    min-width: 0;
    flex: 1;
    margin: 0;
    padding: 5px 8px;
    overflow: hidden;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: var(--fs-label-md);
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .chat-activity__body {
    display: flex;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    gap: 14px;
    overflow-y: auto;
    padding: 10px 8px 12px;
    scrollbar-width: thin;
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
  /* Label and value pairs in even rows. */
  .chat-activity__facts {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 4px 12px;
    margin: 0;
    padding: 4px 8px 0;
    font-size: var(--fs-body-sm);
    font-variant-numeric: tabular-nums;
  }
  .chat-activity__fact {
    display: contents;
  }
  .chat-activity__fact dt {
    align-self: center;
    color: var(--text-lo);
  }
  .chat-activity__fact dd {
    display: flex;
    min-width: 0;
    min-height: 22px;
    align-items: center;
    margin: 0;
    overflow: hidden;
    color: var(--text-hi);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .chat-activity__stats-empty {
    color: var(--text-med);
  }
  :global(.chat-activity__more.btn-tertiary) {
    min-height: 0;
    margin: 4px 0 0;
    padding: 3px 8px;
    border: 0;
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    font-weight: 400;
  }
  :global(.chat-activity__more.btn-tertiary:hover) {
    background: var(--surface-2);
    color: var(--text-hi);
  }
  .chat-activity__empty {
    margin: 0;
    padding: 8px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
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
  .chat-activity__scope {
    min-width: 0;
    overflow: hidden;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-weight: 400;
    text-overflow: ellipsis;
    white-space: nowrap;
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
  .chat-activity__task-time,
  .chat-activity__task-separator {
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
    .chat-activity__disclosure,
  :global(.chat-activity__nested-toggle[aria-expanded='true'])
    .chat-activity__disclosure {
    transform: rotate(90deg);
  }
  /* The count of Sub-Agents below a Sub-Agent, opening them under its row. */
  :global(.chat-activity__nested-toggle.btn-tertiary) {
    min-width: 0;
    min-height: 22px;
    flex: 0 0 auto;
    gap: 3px;
    padding: 0 5px;
    border: 0;
    border-radius: var(--r-sm);
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
    font-weight: 400;
  }
  :global(.chat-activity__nested-toggle.btn-tertiary:hover) {
    background: var(--surface-3);
    color: var(--text-hi);
  }
  .chat-activity__nested {
    margin: 0 0 2px 14px;
    padding: 0 0 0 4px;
    border-left: 1px solid var(--border);
    list-style: none;
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
  :global(.chat-activity__nested-toggle.btn-tertiary:focus-visible),
  :global(.chat-activity__task-link.btn-tertiary:focus-visible),
  :global(.chat-activity__parent-link.btn-tertiary:focus-visible),
  :global(.chat-activity__more.btn-tertiary:focus-visible) {
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
