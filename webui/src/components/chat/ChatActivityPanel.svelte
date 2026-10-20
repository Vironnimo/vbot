<script>
  import { onDestroy, onMount } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  import {
    activitySections,
    changedFilesCard,
    backgroundTasks,
  } from '$lib/chatTimelinePresentation.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import { formatCost, formatTokens } from '$lib/statisticsView.js';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/timeText.js';
  import { tooltip } from '$lib/tooltip.js';

  import Button from '../ui/Button.svelte';
  import ContextMenu from '../ui/ContextMenu.svelte';
  import { contextMenuAnchor } from '../ui/contextMenu.js';
  import ChangeStats from './ChangeStats.svelte';
  import ActivityTaskRow from './ActivityTaskRow.svelte';
  import ActivityReflectionRow from './ActivityReflectionRow.svelte';
  import { taskLabel } from './activityPresentation.js';
  import './activityPanel.css';

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
    // timeline while it is. The Chat header's toggle (`toggleId`) opens and
    // closes it; Escape closes it too.
    open = $bindable(false),
    id = undefined,
    toggleId = undefined,
    // Reports how much work runs, for the toggle's running marker.
    onRunningCountChange = () => {},
  } = $props();

  const cancellingTaskIds = new SvelteSet();
  // Finished reviews whose change list is open, and those asking to confirm
  // their undo; both are view state only.
  const expandedReviews = new SvelteSet();
  const confirmingUndo = new SvelteSet();
  // Command rows whose output view is open, by task id: view state only. An
  // open view reads the output while it shows (CommandOutput.svelte).
  const expandedOutputs = new SvelteSet();
  const expandedSubAgents = new SvelteSet();
  const rowState = {
    cancellingTaskIds,
    expandedOutputs,
    expandedSubAgents,
    expandedReviews,
    confirmingUndo,
  };
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
  $effect(() => {
    onRunningCountChange(runningTaskCount);
  });
  // Finished work stays folded until the user opens it, and then shows its
  // newest entries until the user asks for all of them; view state only.
  const FINISHED_PREVIEW = 5;
  let showFinished = $state(false);
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

  // Closing with focus inside the panel hands focus to the header's toggle,
  // so keyboard users keep their place.
  const closePanel = () => {
    const panel = id ? document.getElementById(id) : null;
    const focusInside = panel?.contains(document.activeElement);
    open = false;
    if (focusInside) {
      document.getElementById(toggleId)?.focus();
    }
  };

  // The cards this browser folded, by card key; every card starts open.
  const COLLAPSED_CARDS_KEY = 'vbot.chat.sessionInfo.collapsedCards';
  let collapsedCards = $state(readCollapsedCards());

  function readCollapsedCards() {
    try {
      const parsed = JSON.parse(
        localStorage.getItem(COLLAPSED_CARDS_KEY) || '{}',
      );
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
        ? parsed
        : {};
    } catch {
      return {};
    }
  }

  const toggleCard = (key) => {
    collapsedCards = { ...collapsedCards, [key]: !collapsedCards[key] };
    try {
      localStorage.setItem(COLLAPSED_CARDS_KEY, JSON.stringify(collapsedCards));
    } catch {
      // localStorage unavailable (private browsing, storage quota)
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

  const parentSessionAction = () =>
    parentSession?.relation === 'reflection'
      ? t('chat.returnToMainSession')
      : parentSession?.relation === 'fork'
        ? t('chat.returnToSourceSession')
        : t('chat.returnToParentSession');

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
      totalTokens: t('chat.activity.usage.tokensTotal', {
        count: formatTokens(input + output, locale),
      }),
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
</script>

<!-- A card of the panel: a header that names it, sums it up and folds it,
     and its details below. A card without details has no fold. -->
{#snippet card(key, title, summary, body)}
  {@const expanded = Boolean(body) && !collapsedCards[key]}
  <section
    class="chat-activity__card"
    data-card={key}
    aria-labelledby={`${panelId}-${key}-title`}
  >
    <h3 id={`${panelId}-${key}-title`} class="chat-activity__card-heading">
      <Button
        variant="tertiary"
        class="chat-activity__card-toggle"
        disabled={!body}
        aria-expanded={body ? expanded : undefined}
        aria-controls={expanded ? `${panelId}-${key}-body` : undefined}
        onClick={() => toggleCard(key)}
      >
        <span class="chat-activity__card-title">{title}</span>
        <span class="chat-activity__card-summary">{@render summary()}</span>
        {#if body}
          <svg
            class="chat-activity__disclosure chat-activity__card-chevron"
            viewBox="0 0 16 16"
            width="12"
            height="12"
            aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg
          >
        {/if}
      </Button>
    </h3>
    {#if expanded}
      <div id={`${panelId}-${key}-body`} class="chat-activity__card-body">
        {@render body()}
      </div>
    {/if}
  </section>
{/snippet}

{#snippet sessionSummary()}
  {#if session?.lastActiveAt}
    {formatRelativeTime(session.lastActiveAt)}
  {/if}
{/snippet}

{#snippet sessionBody()}
  <p class="chat-activity__session-name">
    {session?.displayName ?? t('chat.activity.title')}
  </p>
  {#if session?.createdAt || session?.lastActiveAt}
    <dl class="chat-activity__facts">
      {#if session?.createdAt}
        <div class="chat-activity__fact">
          <dt>{t('chat.activity.overview.started')}</dt>
          <dd>{formatAbsoluteTime(session.createdAt)}</dd>
        </div>
      {/if}
      {#if session?.lastActiveAt}
        <div class="chat-activity__fact">
          <dt>{t('chat.activity.overview.lastActive')}</dt>
          <dd>{formatAbsoluteTime(session.lastActiveAt)}</dd>
        </div>
      {/if}
    </dl>
  {/if}
{/snippet}

{#snippet usageSummary()}
  {usageFacts.cost || usageFacts.totalTokens}
{/snippet}

{#snippet usageBody()}
  <dl class="chat-activity__facts">
    <div class="chat-activity__fact">
      <dt>{t('chat.activity.overview.tokens')}</dt>
      <dd use:tooltip={usageFacts.tokensSplit}>{usageFacts.tokens}</dd>
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
  </dl>
  {#if usage?.subAgentCount > 0}
    <!-- Usage and changes cover the Sub-Agents too. -->
    <p class="chat-activity__scope">
      {usage.subAgentCount === 1
        ? t('chat.activity.overview.withSubAgentsOne')
        : t('chat.activity.overview.withSubAgentsMany', {
            count: usage.subAgentCount,
          })}
    </p>
  {/if}
{/snippet}

<!-- Until the first read answers, the panel claims neither changes nor
     none. -->
{#snippet changesSummary()}
  {#if sessionStats}
    <span class="chat-activity__totals">
      <span
        class="chat-activity__added"
        class:chat-activity__total--zero={sessionStats.added === 0}
        >+{sessionStats.added}</span
      >
      <span
        class="chat-activity__removed"
        class:chat-activity__total--zero={sessionStats.removed === 0}
        >-{sessionStats.removed}</span
      >
    </span>
  {:else if sessionStats === null}
    {t('chat.activity.overview.noChanges')}
  {/if}
{/snippet}

{#snippet changesBody()}
  <ChangeStats stats={sessionStats} list placement="left" />
{/snippet}

{#snippet tasksSummary()}
  {#if sections.running.length > 0}
    <span class="chat-activity__running-summary">
      <span class="chat-activity__working-dot" aria-hidden="true"></span>
      {t('chat.activity.runningCount', { count: sections.running.length })}
    </span>
  {/if}
  {#if sections.finished.length > 0}
    <span
      >{sections.running.length > 0 ? '· ' : ''}{t(
        'chat.activity.finishedCount',
        { count: sections.finished.length },
      )}</span
    >
  {/if}
  {#if sections.running.length === 0 && sections.finished.length === 0}
    {t('chat.activity.empty')}
  {/if}
{/snippet}

{#snippet taskEntries(entries)}
  <ul class="chat-activity__task-list">
    {#each entries as entry (entry.key)}
      <li>
        {#if entry.kind === 'reflection'}
          <ActivityReflectionRow
            row={entry.row}
            {panelId}
            state={rowState}
            {nowMs}
            {onOpenReflection}
            {onLoadReflectionChanges}
            {onUndoReflection}
          />
        {:else}
          <ActivityTaskRow
            task={entry.task}
            {panelId}
            state={rowState}
            {subAgentStatuses}
            {subAgentWork}
            {subAgentTree}
            {onSubAgentWorkWanted}
            {onNavigateToSubAgent}
            {onCancelSubAgent}
            {onStopSubAgentWork}
            {onStopBackgroundCommand}
            {onLoadCommandOutput}
            onOpenTaskMenu={openTaskMenu}
          />
        {/if}
      </li>
    {/each}
  </ul>
{/snippet}

<!-- Running work shows at once; finished work waits folded below it. -->
{#snippet tasksBody()}
  {#if sections.running.length > 0}
    <div class="chat-activity__group chat-activity__group--running">
      {@render taskEntries(sections.running)}
    </div>
  {/if}
  {#if sections.finished.length > 0}
    <div class="chat-activity__group chat-activity__group--finished">
      <Button
        variant="tertiary"
        class="chat-activity__finished-toggle"
        aria-expanded={showFinished}
        aria-controls={showFinished ? `${panelId}-finished` : undefined}
        onClick={() => (showFinished = !showFinished)}
      >
        <svg
          class="chat-activity__disclosure"
          viewBox="0 0 16 16"
          width="10"
          height="10"
          aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg
        >
        {t('chat.activity.finished')}
        <span class="chat-activity__count">{sections.finished.length}</span>
      </Button>
      {#if showFinished}
        <div id={`${panelId}-finished`}>
          {@render taskEntries(visibleFinished)}
          {#if sections.finished.length > FINISHED_PREVIEW}
            <Button
              variant="tertiary"
              class="chat-activity__more"
              aria-expanded={showAllFinished}
              onClick={() => (showAllFinished = !showAllFinished)}
            >
              {showAllFinished
                ? t('chat.activity.showFewer')
                : t('chat.activity.showAll', {
                    count: sections.finished.length,
                  })}
            </Button>
          {/if}
        </div>
      {/if}
    </div>
  {/if}
{/snippet}

{#if open}
  <aside {id} class="chat-activity" aria-label={t('chat.activity.label')}>
    <!-- The way back to the parent stays visible above the cards, also while
         the Session card is folded. -->
    {#if parentSession}
      <Button
        variant="tertiary"
        class="chat-activity__parent-link"
        onClick={() => onNavigateToParentSession(parentSession.target)}
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path d="M6.5 4 2.5 8l4 4" />
          <path d="M3 8h6.25a4.25 4.25 0 0 1 4.25 4.25V13" />
        </svg>
        <span class="chat-activity__parent-text">
          <span class="chat-activity__parent-action">
            {parentSessionAction()}
          </span>
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
        </span>
      </Button>
    {/if}
    {@render card(
      'session',
      t('chat.activity.title'),
      sessionSummary,
      sessionBody,
    )}
    {#if usageFacts}
      {@render card(
        'usage',
        t('chat.activity.usage.title'),
        usageSummary,
        usageBody,
      )}
    {/if}
    {#if sessionStats !== undefined}
      {@render card(
        'changes',
        t('chat.activity.overview.changes'),
        changesSummary,
        changedFilesCard(sessionStats) ? changesBody : null,
      )}
    {/if}
    {@render card(
      'tasks',
      t('chat.activity.tasks.title'),
      tasksSummary,
      sections.running.length + sections.finished.length > 0 ? tasksBody : null,
    )}
  </aside>
{/if}

<ContextMenu menu={taskMenu} onClose={() => (taskMenu = null)} />
