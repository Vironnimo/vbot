<script>
  import {
    backgroundCommandStatusDetails,
    subAgentStatusDetails,
  } from '$lib/chatTimelinePresentation.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Button from '../ui/Button.svelte';
  import { isContextMenuKey } from '../ui/contextMenu.js';
  import CommandOutput from './CommandOutput.svelte';
  import CopyableValueCard from './CopyableValueCard.svelte';
  import ActivityStatus from './ActivityStatus.svelte';
  import { taskLabel } from './activityPresentation.js';

  let {
    task,
    panelId,
    state,
    subAgentStatuses,
    subAgentWork,
    subAgentTree,
    onSubAgentWorkWanted,
    onNavigateToSubAgent,
    onCancelSubAgent,
    onStopSubAgentWork,
    onStopBackgroundCommand,
    onLoadCommandOutput,
    onOpenTaskMenu,
  } = $props();

  // Details behind a status icon: the state in words, since when, how long,
  // and for a command its command id.
  const statusDetails = (task) => {
    const details =
      task.kind === 'command'
        ? backgroundCommandStatusDetails(task.tool, task.rowState, Date.now())
        : subAgentStatusDetails(
            task.tool,
            task.runDotStatus,
            subAgentStatuses,
            Date.now(),
            remainingWork(task),
          );
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

  const cancelTaskLabel = (task) => {
    if (task.kind === 'command') {
      return t('chat.activity.cancelCommandAria', { command: task.label });
    }
    if (task.workRunning) {
      return t('chat.activity.stopSubAgentWorkAria', { agent: task.agentId });
    }
    return t('chat.activity.cancelSubAgentAria', { agent: task.agentId });
  };

  const isTaskCancelling = (task) => state.cancellingTaskIds.has(task.id);
  const handleCancelTask = async (task) => {
    if (isTaskCancelling(task)) {
      return;
    }
    state.cancellingTaskIds.add(task.id);
    try {
      if (task.kind === 'command') {
        await onStopBackgroundCommand({ commandId: task.commandId });
      } else if (task.workRunning) {
        await onStopSubAgentWork({ tool: task.tool });
      } else {
        await onCancelSubAgent({ tool: task.tool });
      }
    } finally {
      state.cancellingTaskIds.delete(task.id);
    }
  };

  const outputId = (task) => `${panelId}-${task.id}-output`;

  const toggleOutput = (task) => {
    if (state.expandedOutputs.has(task.id)) {
      state.expandedOutputs.delete(task.id);
    } else {
      state.expandedOutputs.add(task.id);
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
  const childrenOf = (subAgentId) =>
    (subAgentId && subAgentChildren[subAgentId]) || [];
  const nestedListId = (subAgentId) => `${panelId}-nested-${subAgentId}`;
  const toggleNested = (subAgentId) => {
    if (state.expandedSubAgents.has(subAgentId)) {
      state.expandedSubAgents.delete(subAgentId);
    } else {
      state.expandedSubAgents.add(subAgentId);
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
</script>

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

{#snippet commandContent(task)}
  <span class="chat-activity__task-copy">
    <span
      class="chat-activity__task-title"
      class:chat-activity__task-command={!task.description}
    >
      {task.label}
    </span>
    <span class="chat-activity__task-meta">
      <span
        class="chat-activity__task-state"
        use:tooltip={() => statusDetails(task)}
      >
        <ActivityStatus dotStatus={task.dotStatus} />
        <span class="chat-activity__task-kind"
          >{t('chat.activity.kind.command')}</span
        >
        {#if task.timeLabel}
          <span class="chat-activity__task-time">· {task.timeLabel}</span>
        {/if}
      </span>
    </span>
  </span>
  <CopyableValueCard
    value={task.fullCommand}
    title={task.description}
    mono
    copyLabel={t('chat.copyCommand')}
    copiedLabel={t('chat.commandCopied')}
    whenTruncated={!task.description && task.command === task.fullCommand}
    placement="left"
  />
{/snippet}

<!-- Every row reads as two lines: what the work is about, then a quiet line
     with its state symbol, its kind and how long it ran. -->
{#snippet taskRow(task)}
  {#if task.kind === 'command'}
    <div
      class="chat-activity__task-row chat-activity__task-row--bash"
      aria-label={taskLabel(task)}
    >
      {#if onLoadCommandOutput}
        <Button
          variant="tertiary"
          class="chat-activity__task-link"
          ariaLabel={t('chat.activity.commandOutput.aria', {
            command: task.label,
          })}
          aria-expanded={state.expandedOutputs.has(task.id)}
          aria-controls={state.expandedOutputs.has(task.id)
            ? outputId(task)
            : undefined}
          onClick={() => toggleOutput(task)}
        >
          {@render commandContent(task)}
        </Button>
      {:else}
        <!-- Keep the complete command and Copy reachable by keyboard even
             when this view cannot load output. -->
        <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
        <span
          class="chat-activity__task-copy"
          tabindex={task.fullCommand ? 0 : undefined}
        >
          {@render commandContent(task)}
        </span>
      {/if}
      {@render cancelButton(task)}
    </div>
    {#if onLoadCommandOutput && state.expandedOutputs.has(task.id)}
      <CommandOutput
        id={outputId(task)}
        commandId={task.commandId}
        running={task.dotStatus === 'running'}
        load={onLoadCommandOutput}
        label={t('chat.activity.commandOutput.aria', {
          command: task.label,
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
        oncontextmenu={(event) => onOpenTaskMenu(task, event)}
        onkeydown={(event) => {
          if (isContextMenuKey(event)) onOpenTaskMenu(task, event);
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
            <ActivityStatus dotStatus={task.dotStatus} />
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
    {#if state.expandedSubAgents.has(task.subAgentId)}
      {@render nestedList(task.subAgentId)}
    {/if}
  {/if}
{/snippet}

<!-- How many Sub-Agents a Sub-Agent works with; shows them below its row. -->
{#snippet nestedToggle(subAgentId, name)}
  {@const count = childrenOf(subAgentId).length}
  {#if count > 0}
    {@const expanded = state.expandedSubAgents.has(subAgentId)}
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
                <ActivityStatus dotStatus={treeDotStatus(child.status)} />
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
        {#if state.expandedSubAgents.has(child.id)}
          {@render nestedList(child.id)}
        {/if}
      </li>
    {/each}
  </ul>
{/snippet}

{@render taskRow(task)}
